import os
import json
import uuid
import sqlite3
import logging
import asyncio
import tempfile
import queue
from pathlib import Path
from decimal import Decimal, InvalidOperation

import requests
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import AudioFileClip, ImageClip, concatenate_videoclips

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIGURATION
# Fill these values later, or set them as environment vars.
# =========================================================
# Set these two values in Railway -> Service -> Variables.
# Do not paste secret keys directly into this file.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing. Add it in Railway Variables.")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing. Add it in Railway Variables.")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "YOUR_TELEGRAM_USERNAME").lstrip("@")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "5293614793"))

BKASH_NUMBER = os.getenv("BKASH_NUMBER", "01911198221")
NAGAD_NUMBER = os.getenv("NAGAD_NUMBER", "01911198221")

DATABASE_FILE = "users.db"
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

WELCOME_CREDITS = 10
REFERRAL_CREDITS = 20
MAX_MINUTES = 10
MAX_PROMPT_LENGTH = 1500
MAX_PARALLEL_VIDEOS = 2
VIDEO_WIDTH, VIDEO_HEIGHT = 1280, 720

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai_video_bot")


# =========================================================
# DATABASE
# =========================================================
def db_connect():
    conn = sqlite3.connect(DATABASE_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                name TEXT,
                credits INTEGER NOT NULL DEFAULT 0,
                referred_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referred_user_id INTEGER PRIMARY KEY,
                referrer_user_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def get_or_create_user(user_id, username, first_name, referrer_id=None):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

        if row:
            conn.execute(
                "UPDATE users SET username = ?, name = ? WHERE user_id = ?",
                (username or "unknown", first_name or "unknown", user_id),
            )
            updated = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(updated), False

        valid_referrer = None
        if referrer_id and int(referrer_id) != int(user_id):
            exists = conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (int(referrer_id),)
            ).fetchone()
            if exists:
                valid_referrer = int(referrer_id)

        conn.execute(
            """INSERT INTO users (user_id, username, name, credits, referred_by)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, username or "unknown", first_name or "unknown",
             WELCOME_CREDITS, valid_referrer),
        )

        if valid_referrer:
            # Primary key prevents a referral from being rewarded twice.
            inserted = conn.execute(
                """INSERT OR IGNORE INTO referrals
                   (referred_user_id, referrer_user_id) VALUES (?, ?)""",
                (user_id, valid_referrer),
            ).rowcount
            if inserted:
                conn.execute(
                    "UPDATE users SET credits = credits + ? WHERE user_id = ?",
                    (REFERRAL_CREDITS, valid_referrer),
                )

        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row), True


def get_user(user_id):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def change_credits(user_id, amount):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        new_balance = int(row["credits"]) + int(amount)
        if new_balance < 0:
            return None
        conn.execute(
            "UPDATE users SET credits = ? WHERE user_id = ?",
            (new_balance, user_id),
        )
        return new_balance


def reserve_credits(user_id, amount):
    """Atomically deduct credits; return new balance or None."""
    with db_connect() as conn:
        cur = conn.execute(
            """UPDATE users SET credits = credits - ?
               WHERE user_id = ? AND credits >= ?""",
            (amount, user_id, amount),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row["credits"])


def list_users(limit=20):
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT user_id, username, name, credits FROM users "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        return count, [dict(r) for r in rows]


# =========================================================
# HELPERS / KEYBOARDS
# =========================================================
def is_admin_user(user):
    if not user:
        return False
    username_match = bool(
        ADMIN_USERNAME and ADMIN_USERNAME != "YOUR_TELEGRAM_USERNAME"
        and (user.username or "").lower() == ADMIN_USERNAME.lower()
    )
    id_match = ADMIN_CHAT_ID > 0 and user.id == ADMIN_CHAT_ID
    return id_match or username_match


def home_keyboard(is_admin=False):
    rows = [
        [InlineKeyboardButton("🎬 নতুন ভিডিও তৈরি করুন", callback_data="make_video")],
        [
            InlineKeyboardButton("💎 আমার ক্রেডিট", callback_data="my_credit"),
            InlineKeyboardButton("🎁 রেফারেল", callback_data="referral_menu"),
        ],
        [
            InlineKeyboardButton("💳 ক্রেডিট টপআপ", callback_data="buy_credit"),
            InlineKeyboardButton("📞 সাপোর্ট", callback_data="contact_admin"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("⚡ Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def back_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀️ পেছনে", callback_data="back"),
            InlineKeyboardButton("🏠 হোম", callback_data="home"),
        ]
    ])


def voice_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗣️ স্বাভাবিক ভয়েস", callback_data="voice_normal"),
            InlineKeyboardButton("🎙️ ধীর ভয়েস", callback_data="voice_deep"),
        ],
        [InlineKeyboardButton("🏠 হোম", callback_data="home")],
    ])


def minute_keyboard():
    rows = []
    for a, b in ((1, 2), (4, 5), (6, 10)):
        rows.append([
            InlineKeyboardButton(f"⏱️ {a} মিনিট ({a} ক্রেডিট)", callback_data=f"min_{a}"),
            InlineKeyboardButton(f"⏱️ {b} মিনিট ({b} ক্রেডিট)", callback_data=f"min_{b}"),
        ])
    rows.append([InlineKeyboardButton("🏠 হোম", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 ইউজার তালিকা", callback_data="admin_users")],
        [InlineKeyboardButton("🏠 হোম", callback_data="home")],
    ])


async def show_home(target, context, user):
    user_data = get_user(user.id)
    if not user_data:
        user_data, _ = get_or_create_user(
            user.id, user.username, user.first_name
        )
    text = (
        "🏠 *AI VIDEO STUDIO*\n\n"
        f"👤 ইউজার: {user.first_name or 'User'}\n"
        f"💎 ব্যালেন্স: *{user_data['credits']} ক্রেডিট*\n\n"
        "নিচের মেনু থেকে একটি অপশন বেছে নিন।"
    )
    await target.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=home_keyboard(is_admin_user(user)),
    )


# =========================================================
# IMAGE / STORY / VIDEO GENERATION
# =========================================================
def find_bengali_font():
    candidates = [
        os.getenv("BENGALI_FONT", ""),
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansBengaliUI-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "NotoSansBengali-Regular.ttf",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            try:
                return ImageFont.truetype(candidate, 34)
            except Exception:
                pass
    return ImageFont.load_default()


def make_image(text, filename, part_no):
    image = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (15, 23, 42))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (55, 55, VIDEO_WIDTH - 55, VIDEO_HEIGHT - 55),
        radius=28, outline=(99, 102, 241), width=5,
    )
    draw.rounded_rectangle(
        (78, 78, VIDEO_WIDTH - 78, VIDEO_HEIGHT - 78),
        radius=20, fill=(30, 41, 59),
    )

    title_font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34
    ) if os.path.exists("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf") else ImageFont.load_default()
    body_font = find_bengali_font()

    draw.text((115, 125), "AI VIDEO STUDIO", font=title_font, fill=(165, 180, 252))
    draw.text((115, 190), f"PART {part_no:02d}", font=title_font, fill=(251, 191, 36))

    # Wrap text by character count to keep it inside the frame.
    clean_text = " ".join((text or "").split())
    lines = [clean_text[i:i + 42] for i in range(0, min(len(clean_text), 210), 42)]
    y = 285
    for line in lines[:5]:
        try:
            draw.text((115, y), line, font=body_font, fill=(241, 245, 249))
        except Exception:
            # Some minimal server images do not have a Bengali font installed.
            draw.text((115, y), "বাংলা ফন্ট ইনস্টল করলে লেখা দেখা যাবে", fill=(241, 245, 249))
            break
        y += 62

    draw.text((115, 620), "Generated with AI", font=title_font, fill=(148, 163, 184))
    image.save(filename, quality=92)
    return filename


def generate_story_parts(topic, minutes):
    if not GROQ_API_KEY or GROQ_API_KEY.startswith("PASTE_"):
        raise RuntimeError("Groq API key সেট করা হয়নি।")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    parts = []
    session = requests.Session()

    for index in range(1, minutes + 1):
        prompt = (
            f"'{topic}' বিষয় নিয়ে বাংলায় একটি ধারাবাহিক গল্প লিখুন। "
            f"এটি মোট {minutes} মিনিটের ভিডিওর {index} নম্বর অংশ। "
            "প্রতি অংশে প্রায় 100-140টি বাংলা শব্দ লিখুন। "
            "আগের অংশের ধারাবাহিকতা বজায় রাখুন। শুধু গল্পের অংশ দিন।"
        )
        payload = {
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 500,
        }
        response = session.post(
            GROQ_URL, headers=headers, json=payload, timeout=(15, 90)
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Groq API error ({response.status_code}): {response.text[:250]}"
            )
        result = response.json()
        content = result["choices"][0]["message"]["content"].strip()
        if not content:
            raise RuntimeError("Groq থেকে খালি গল্প পাওয়া গেছে।")
        parts.append(content)

    session.close()
    return parts


def render_video_sync(topic, minutes, voice_style, workdir, progress_queue):
    """Blocking work runs in a worker thread, not on the Telegram event loop."""
    clips = []
    audio_objects = []
    final_clip = None
    output_path = os.path.join(workdir, "video.mp4")

    try:
        progress_queue.put_nowait(("story", 0, minutes))
        stories = generate_story_parts(topic, minutes)

        for index, story in enumerate(stories, start=1):
            progress_queue.put_nowait(("render", index, minutes))
            audio_path = os.path.join(workdir, f"voice_{index}.mp3")
            image_path = os.path.join(workdir, f"frame_{index}.jpg")

            gTTS(
                text=story[:4500],
                lang="bn",
                slow=(voice_style == "deep"),
            ).save(audio_path)

            make_image(story[:210], image_path, index)

            audio = AudioFileClip(audio_path)
            audio_objects.append(audio)
            clip = ImageClip(image_path).set_duration(audio.duration).set_audio(audio)
            clips.append(clip)

        progress_queue.put_nowait(("merge", minutes, minutes))
        final_clip = concatenate_videoclips(clips, method="compose")
        final_clip.write_videofile(
            output_path,
            fps=24,
            codec="libx264",
            audio_codec="aac",
            preset="ultrafast",
            threads=2,
            logger=None,
        )
        return output_path
    finally:
        if final_clip:
            try:
                final_clip.close()
            except Exception:
                pass
        for clip in clips:
            try:
                clip.close()
            except Exception:
                pass
        for audio in audio_objects:
            try:
                audio.close()
            except Exception:
                pass


# =========================================================
# TELEGRAM HANDLERS
# =========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referrer_id = None
    if context.args and context.args[0].isdigit():
        referrer_id = int(context.args[0])

    user_data, is_new = get_or_create_user(
        user.id, user.username, user.first_name, referrer_id
    )
    message = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "✨ *AI VIDEO STUDIO* ✨\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"স্বাগতম *{user.first_name or 'User'}*!\n\n"
        "আপনার লেখা বিষয় থেকে AI গল্প, বাংলা ভয়েস এবং ভিডিও তৈরি করুন।\n\n"
        "👤 ইউজার: @" + (user.username or "N/A") + "\n"
        f"🆔 ID: `{user.id}`\n"
        f"💎 ব্যালেন্স: *{user_data['credits']} ক্রেডিট*\n"
    )
    if is_new:
        message += f"\n🎁 নতুন ইউজার বোনাস: *{WELCOME_CREDITS} ক্রেডিট*"
    message += "\n\nনিচের মেনু থেকে একটি অপশন বেছে নিন:"
    await update.message.reply_text(
        message, parse_mode=ParseMode.MARKDOWN,
        reply_markup=home_keyboard(is_admin_user(user)),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data

    user_data = get_user(user.id)
    if not user_data:
        user_data, _ = get_or_create_user(user.id, user.username, user.first_name)

    if data in ("home", "back"):
        context.user_data.clear()
        await show_home(query, context, user)
        return

    if data == "make_video":
        if user_data["credits"] < 1:
            await query.edit_message_text(
                "❌ আপনার পর্যাপ্ত ক্রেডিট নেই। রেফারেল করুন অথবা টপআপ নিন।",
                reply_markup=back_keyboard(),
            )
            return
        await query.edit_message_text(
            "🎙️ *ভয়েস স্টাইল নির্বাচন করুন:*\n\n"
            "স্বাভাবিক ভয়েস অথবা ধীর গতির ভয়েস বেছে নিন।",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=voice_keyboard(),
        )
        return

    if data in ("voice_normal", "voice_deep"):
        context.user_data["voice_style"] = "normal" if data == "voice_normal" else "deep"
        await query.edit_message_text(
            f"🎬 *ভিডিওর দৈর্ঘ্য নির্বাচন করুন:*\n\n"
            f"💎 আপনার ব্যালেন্স: *{user_data['credits']} ক্রেডিট*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=minute_keyboard(),
        )
        return

    if data.startswith("min_"):
        try:
            minutes = int(data.split("_", 1)[1])
        except ValueError:
            return
        if minutes < 1 or minutes > MAX_MINUTES:
            await query.edit_message_text("এই ভিডিও দৈর্ঘ্যটি গ্রহণযোগ্য নয়।", reply_markup=back_keyboard())
            return
        if user_data["credits"] < minutes:
            await query.edit_message_text(
                f"❌ {minutes} মিনিটের ভিডিওর জন্য {minutes} ক্রেডিট প্রয়োজন। "
                f"আপনার আছে {user_data['credits']}।",
                reply_markup=back_keyboard(),
            )
            return
        context.user_data["minute"] = minutes
        context.user_data["awaiting_prompt"] = True
        await query.edit_message_text(
            f"✅ নির্বাচিত: *{minutes} মিনিট* ({minutes} ক্রেডিট)\n\n"
            "✍️ এখন আপনার ভিডিওর বিষয় বা গল্পের প্রম্পট লিখুন।",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_keyboard(),
        )
        return

    if data == "my_credit":
        await query.edit_message_text(
            "💎 *আপনার অ্যাকাউন্ট*\n\n"
            f"👤 {user.first_name or 'User'}\n"
            f"🆔 `{user.id}`\n"
            f"💰 ব্যালেন্স: *{user_data['credits']} ক্রেডিট*\n\n"
            "ভিডিও রেট: প্রতি মিনিটে ১ ক্রেডিট।",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_keyboard(),
        )
        return

    if data == "referral_menu":
        bot = await context.bot.get_me()
        link = f"https://t.me/{bot.username}?start={user.id}"
        await query.edit_message_text(
            "🎁 *রেফারেল বোনাস*\n\n"
            f"বন্ধুকে আপনার লিংক দিয়ে বট চালু করালে আপনি {REFERRAL_CREDITS} ক্রেডিট পাবেন "
            "যখন সে প্রথমবার যোগ দেবে।\n\n"
            f"🔗 আপনার লিংক:\n`{link}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_keyboard(),
        )
        return

    if data == "buy_credit":
        await query.edit_message_text(
            "💳 *ক্রেডিট টপআপ*\n\n"
            f"বিকাশ (Personal): `{BKASH_NUMBER}`\n"
            f"নগদ (Personal): `{NAGAD_NUMBER}`\n\n"
            "Send Money করার পর TrxID ও আপনার Telegram ID দিয়ে অ্যাডমিনকে জানান।\n"
            f"Admin: @{ADMIN_USERNAME}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_keyboard(),
        )
        return

    if data == "contact_admin":
        await query.edit_message_text(
            "📞 *কাস্টমার সাপোর্ট*\n\n"
            f"Admin: @{ADMIN_USERNAME}\n"
            f"আপনার ID: `{user.id}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_keyboard(),
        )
        return

    if data == "admin_panel":
        if not is_admin_user(user):
            await query.edit_message_text("⛔ এই অপশনটি শুধু অ্যাডমিনের জন্য।", reply_markup=back_keyboard())
            return
        await query.edit_message_text(
            "⚡ *Admin Dashboard*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_keyboard(),
        )
        return

    if data == "admin_users":
        if not is_admin_user(user):
            return
        total, rows = list_users(20)
        lines = [f"👥 *মোট ইউজার: {total}*", ""]
        for item in rows:
            lines.append(
                f"• {item['name'] or 'Unknown'} | `{item['user_id']}` | "
                f"{item['credits']} credits"
            )
        lines += ["", "ক্রেডিট দিতে কমান্ড:", "`/addcredit USER_ID AMOUNT`"]
        await query.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_keyboard(),
        )


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.user_data.get("awaiting_prompt"):
        return

    topic = (update.message.text or "").strip()
    if not topic:
        await update.message.reply_text("একটি বিষয় লিখুন।")
        return
    if len(topic) > MAX_PROMPT_LENGTH:
        await update.message.reply_text(
            f"প্রম্পট সর্বোচ্চ {MAX_PROMPT_LENGTH} অক্ষরের হতে পারবে। আবার লিখুন।"
        )
        return

    minutes = int(context.user_data.get("minute", 0))
    voice_style = context.user_data.get("voice_style", "normal")
    if minutes < 1 or minutes > MAX_MINUTES:
        context.user_data.clear()
        await update.message.reply_text("সেশন শেষ হয়েছে। /start দিয়ে আবার শুরু করুন।")
        return

    # Limit concurrent render jobs to protect small hosting plans.
    semaphore = context.application.bot_data["video_semaphore"]
    if semaphore.locked():
        await update.message.reply_text(
            "⏳ এখন ভিডিও তৈরির কাজ চলছে। একটু পরে আবার চেষ্টা করুন।",
            reply_markup=back_keyboard(),
        )
        return

    new_balance = reserve_credits(user.id, minutes)
    if new_balance is None:
        await update.message.reply_text(
            "❌ পর্যাপ্ত ক্রেডিট নেই। /start দিয়ে ব্যালেন্স দেখুন।",
            reply_markup=back_keyboard(),
        )
        return

    context.user_data["awaiting_prompt"] = False
    status = await update.message.reply_text(
        "🚀 আপনার ভিডিও তৈরির কাজ শুরু হয়েছে।\n"
        f"⏱️ দৈর্ঘ্য: {minutes} মিনিট\n"
        f"💎 কাটা হয়েছে: {minutes} ক্রেডিট\n"
        f"অবশিষ্ট: {new_balance} ক্রেডিট\n\n"
        "প্রসেসিং শেষ হলে ভিডিও পাঠানো হবে।"
    )

    workdir = tempfile.mkdtemp(prefix=f"video_{user.id}_")
    progress_queue = queue.Queue()

    try:
        async with semaphore:
            task = asyncio.create_task(
                asyncio.to_thread(
                    render_video_sync, topic, minutes, voice_style,
                    workdir, progress_queue
                )
            )

            # Poll progress without blocking the event loop.
            last_status = ""
            while not task.done():
                try:
                    stage, current, total = await asyncio.to_thread(
                        progress_queue.get, True, 2
                    )
                    if stage == "story":
                        text = "✍️ AI গল্প তৈরি করছে..."
                    elif stage == "render":
                        text = f"🎨 পার্ট {current}/{total}: ভয়েস ও ছবি তৈরি হচ্ছে..."
                    else:
                        text = "🎞️ ভিডিও একত্র করা হচ্ছে..."
                    if text != last_status:
                        try:
                            await status.edit_text(text)
                        except Exception:
                            pass
                        last_status = text
                except queue.Empty:
                    continue

            video_path = await task

        await status.edit_text("✅ ভিডিও তৈরি হয়েছে। Telegram-এ পাঠানো হচ্ছে...")
        with open(video_path, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=(
                    "🎉 *আপনার ভিডিও তৈরি সম্পন্ন!*\n\n"
                    f"🎬 বিষয়: {topic[:300]}\n"
                    f"💎 অবশিষ্ট ক্রেডিট: {get_user(user.id)['credits']}"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        await status.delete()

    except Exception as exc:
        logger.exception("Video generation failed for user %s", user.id)
        refunded = change_credits(user.id, minutes)
        refund_text = (
            f"{minutes} ক্রেডিট ফেরত দেওয়া হয়েছে।"
            if refunded is not None else "ক্রেডিট ফেরত দিতে সমস্যা হয়েছে; অ্যাডমিনের সঙ্গে যোগাযোগ করুন।"
        )
        await status.edit_text(
            "❌ ভিডিও তৈরি করা যায়নি।\n"
            f"{refund_text}\n\n"
            f"কারণ: {str(exc)[:350]}"
        )
    finally:
        context.user_data.pop("minute", None)
        context.user_data.pop("voice_style", None)
        context.user_data.pop("awaiting_prompt", None)
        # Remove only this job's private temporary directory.
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


async def addcredit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user):
        await update.message.reply_text("⛔ এই কমান্ড শুধু অ্যাডমিনের জন্য।")
        return
    if len(context.args) != 2:
        await update.message.reply_text(
            "ব্যবহার: /addcredit USER_ID AMOUNT\nউদাহরণ: /addcredit 123456789 10"
        )
        return
    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
        if amount <= 0:
            raise ValueError("amount must be positive")
    except ValueError:
        await update.message.reply_text("❌ USER_ID ও AMOUNT সঠিক সংখ্যা হতে হবে; AMOUNT ০-এর বেশি দিন।")
        return

    balance = change_credits(target_id, amount)
    if balance is None:
        await update.message.reply_text("❌ ইউজার পাওয়া যায়নি বা ক্রেডিট আপডেট হয়নি।")
        return
    await update.message.reply_text(
        f"✅ {target_id}-কে {amount} ক্রেডিট দেওয়া হয়েছে।\n"
        f"বর্তমান ব্যালেন্স: {balance}"
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "চলমান নির্বাচন বাতিল করা হয়েছে। /start দিয়ে আবার শুরু করুন।",
        reply_markup=home_keyboard(is_admin_user(update.effective_user)),
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled Telegram handler error", exc_info=context.error)


def main():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("PASTE_"):
        raise RuntimeError("প্রথমে TELEGRAM_TOKEN বসান।")
    if ADMIN_CHAT_ID <= 0 and ADMIN_USERNAME == "YOUR_TELEGRAM_USERNAME":
        logger.warning("Admin ID/username এখনো সেট করা হয়নি; admin commands কাজ করবে না।")

    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.bot_data["video_semaphore"] = asyncio.Semaphore(MAX_PARALLEL_VIDEOS)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addcredit", addcredit_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, video_handler))
    app.add_error_handler(error_handler)

    logger.info("AI Video Studio bot is starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
