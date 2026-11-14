import asyncio
import html
import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =============== CONFIG ===============

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "5293614793"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "Fast1panelbot").lstrip("@")

DB_FILE = Path(os.getenv("DB_FILE", "users_db.json"))

VIDEO_COST = 5
START_CREDITS = 10
REFERRAL_CREDITS = 20

MAX_VIDEO_SIZE = 50 * 1024 * 1024
PROCESS_TIMEOUT = 600
MAX_PARALLEL_JOBS = 2

WHATSAPP_URL = "https://wa.me/8801791110761"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

DB_LOCK = threading.RLock()
USER_LOCKS = {}
USER_LOCKS_GUARD = threading.Lock()
PROCESS_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL_JOBS)


# =============== DATABASE ===============

def default_user():
    return {
        "credits": START_CREDITS,
        "referred_by": None,
        "referred_count": 0,
    }


def load_db():
    with DB_LOCK:
        if not DB_FILE.exists():
            return {}

        try:
            with DB_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)

            return data if isinstance(data, dict) else {}

        except Exception as e:
            logger.error("Database read error: %s", e)
            return {}


def save_db(db):
    with DB_LOCK:
        try:
            DB_FILE.parent.mkdir(parents=True, exist_ok=True)

            fd, temp_name = tempfile.mkstemp(
                prefix=f"{DB_FILE.name}.",
                suffix=".tmp",
                dir=str(DB_FILE.parent),
            )

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(db, f, indent=4, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())

                os.replace(temp_name, DB_FILE)

            finally:
                if os.path.exists(temp_name):
                    os.remove(temp_name)

        except OSError:
            logger.exception("Database save error")


def get_user_data(user_id):
    with DB_LOCK:
        db = load_db()

        if user_id not in db:
            db[user_id] = default_user()
        else:
            for key, value in default_user().items():
                db[user_id].setdefault(key, value)

        save_db(db)
        return dict(db[user_id])


def get_total_users():
    return len(load_db())


def update_user_credit(user_id, amount):
    with DB_LOCK:
        db = load_db()

        if user_id not in db:
            db[user_id] = default_user()

        db[user_id]["credits"] = (
            int(db[user_id].get("credits", 0)) + amount
        )

        save_db(db)

        return db[user_id]["credits"]


def deduct_user_credit(user_id, amount):
    with DB_LOCK:
        db = load_db()

        if user_id not in db:
            db[user_id] = default_user()

        balance = int(db[user_id].get("credits", 0))

        if balance < amount:
            save_db(db)
            return False, balance

        db[user_id]["credits"] = balance - amount

        save_db(db)

        return True, db[user_id]["credits"]


def register_referral(new_user_id, referrer_id):

    if new_user_id == referrer_id:
        return False

    with DB_LOCK:
        db = load_db()

        if new_user_id not in db or referrer_id not in db:
            return False

        new_user = db[new_user_id]

        if new_user.get("referred_by") is not None:
            return False

        new_user["referred_by"] = referrer_id

        referrer = db[referrer_id]

        referrer["credits"] = (
            int(referrer.get("credits", 0))
            + REFERRAL_CREDITS
        )

        referrer["referred_count"] = (
            int(referrer.get("referred_count", 0)) + 1
        )

        save_db(db)

        return True


def get_user_lock(user_id):
    with USER_LOCKS_GUARD:
        if user_id not in USER_LOCKS:
            USER_LOCKS[user_id] = asyncio.Lock()

        return USER_LOCKS[user_id]


# =============== VIDEO PROCESSING ===============

def process_video_advanced(input_path, output_path):

    # আক্রমণাত্মক ভিজ্যুয়াল বাইপাস: স্পিড পরিবর্তন, হিউ শিফট, প্যাডিং বর্ডার, হেভি কালার এবং নয়েজ
    vf_watermark = (
        "setpts=0.95*PTS,"
        "hue=h=12,"
        "pad=iw+16:ih+16:(ow-iw)/2:(oh-ih)/2:black,"
        "eq=brightness=0.06:contrast=1.2:saturation=1.25,"
        "noise=alls=18:allf=t+u,"
        f"drawtext=text='@{BOT_USERNAME}':"
        "x=w-tw-20:y=20:"
        "fontsize=22:"
        "fontcolor=white:"
        "box=1:"
        "boxcolor=black@0.55"
    )

    vf_normal = (
        "setpts=0.95*PTS,"
        "hue=h=12,"
        "pad=iw+16:ih+16:(ow-iw)/2:(oh-ih)/2:black,"
        "eq=brightness=0.06:contrast=1.2:saturation=1.25,"
        "noise=alls=18:allf=t+u"
    )

    af_filter = "atempo=1.053,asetrate=44100*1.02,equalizer=f=1000:t=q:w=1:g=-8"

    def build_cmd(vf):
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", input_path,
            "-map", "0:v:0",
            "-map", "0:a?",
            "-vf", vf,
            "-af", af_filter,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "128k",
            "-map_metadata", "0",
            "-movflags", "+faststart",
            output_path,
        ]

    try:

        result = subprocess.run(
            build_cmd(vf_watermark),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PROCESS_TIMEOUT,
        )

        if (
            result.returncode != 0
            and "drawtext" in result.stderr.lower()
        ):

            logger.warning(
                "Drawtext unavailable. Retrying without watermark."
            )

            if os.path.exists(output_path):
                os.remove(output_path)

            result = subprocess.run(
                build_cmd(vf_normal),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=PROCESS_TIMEOUT,
            )

        if result.returncode != 0:

            error = result.stderr.strip()[-700:]

            return False, (
                f"FFmpeg Error {result.returncode}: {error}"
            )

        if (
            not os.path.exists(output_path)
            or os.path.getsize(output_path) == 0
        ):
            return False, "Output video is empty."

        return True, ""

    except subprocess.TimeoutExpired:

        return False, "ভিডিও প্রসেসিংয়ে ১০ মিনিটের বেশি সময় লেগেছে।"

    except FileNotFoundError:

        return False, "সার্ভারে FFmpeg পাওয়া যায়নি।"

    except Exception as e:

        logger.exception("FFmpeg error")

        return False, str(e)


# =============== KEYBOARDS ===============

def home_keyboard(is_admin=False):

    keyboard = [
        [
            InlineKeyboardButton(
                "🎁 প্রতি রেফারে ২০ ক্রেডিট",
                callback_data="ref_menu",
            )
        ],
        [
            InlineKeyboardButton(
                "💎 ব্যালেন্স চেক",
                callback_data="check_credits",
            ),
            InlineKeyboardButton(
                "⚡ রিচার্জ",
                url=WHATSAPP_URL,
            ),
        ],
        [
            InlineKeyboardButton(
                "🏠 Home",
                callback_data="home",
            )
        ],
    ]

    if is_admin:
        keyboard.insert(
            0,
            [
                InlineKeyboardButton(
                    "👑 Admin Control Panel",
                    callback_data="admin_help",
                )
            ],
        )

    return InlineKeyboardMarkup(keyboard)


# =============== START COMMAND ===============

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if user is None:
        return

    user_id = str(user.id)

    get_user_data(user_id)

    if context.args and context.args[0].startswith("ref_"):

        referrer_id = context.args[0][4:]

        if (
            referrer_id.isdigit()
            and register_referral(user_id, referrer_id)
        ):

            try:
                await context.bot.send_message(
                    chat_id=int(referrer_id),
                    text=(
                        "🎉 <b>দারুণ খবর!</b>\n"
                        "একজন নতুন বন্ধু জয়েন করেছে।\n"
                        "🎁 আপনার অ্যাকাউন্টে "
                        "<b>২০ ফ্রি ক্রেডিট</b> যোগ হয়েছে!"
                    ),
                    parse_mode=ParseMode.HTML,
                )

            except Exception:
                logger.info("Referral notification failed")

    data = get_user_data(user_id)

    text = (
        f"👥 <b>Total Users: {get_total_users()}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🌟 <b>স্বাগতম, {html.escape(user.first_name)}!</b>\n\n"
        "🔥 নিজের অথবা ব্যবহারের অনুমতি থাকা ভিডিও এডিট করুন।\n\n"
        f"💎 আপনার ব্যালেন্স: <b>{data['credits']} ক্রেডিট</b>\n"
        f"📌 প্রতি ভিডিও এডিট: <b>{VIDEO_COST} ক্রেডিট</b>\n\n"
        f"🎁 প্রতি সফল রেফারে {REFERRAL_CREDITS} ক্রেডিট!\n\n"
        "👇 ভিডিও পাঠিয়ে এডিট শুরু করুন।"
    )

    markup = home_keyboard(user.id == ADMIN_ID)

    await update.effective_message.reply_text(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
    )


# =============== VIDEO HANDLER ===============

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    video = message.video or message.document

    if video is None:
        return

    user_id = str(user.id)

    async with get_user_lock(user_id):

        data = get_user_data(user_id)

        if data["credits"] < VIDEO_COST:

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🎁 Refer",
                        callback_data="ref_menu",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⚡ Recharge",
                        url=WHATSAPP_URL,
                    )
                ],
            ])

            await message.reply_text(
                "❌ পর্যাপ্ত ক্রেডিট নেই!\n"
                f"একটি ভিডিও এডিট করতে {VIDEO_COST} ক্রেডিট লাগবে।",
                reply_markup=keyboard,
            )

            return

        if (
            video.file_size is not None
            and video.file_size > MAX_VIDEO_SIZE
        ):

            await message.reply_text(
                "⚠️ ভিডিওটি ৫০ MB-এর বেশি।"
            )

            return

        charged, remaining = deduct_user_credit(
            user_id,
            VIDEO_COST,
        )

        if not charged:
            await message.reply_text("❌ পর্যাপ্ত ক্রেডিট নেই।")
            return

        status = await message.reply_text(
            "📥 ভিডিও ডাউনলোড ও প্রসেসিং শুরু হয়েছে..."
        )

        input_file = (
            f"input_{message.chat_id}_{message.message_id}.mp4"
        )

        output_file = (
            f"output_{message.chat_id}_{message.message_id}.mp4"
        )

        refunded = False
        delivered = False

        try:

            telegram_file = await context.bot.get_file(
                video.file_id,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
            )

            await telegram_file.download_to_drive(
                custom_path=input_file,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
            )

            if os.path.getsize(input_file) > MAX_VIDEO_SIZE:
                raise ValueError("ভিডিওটি ৫০ MB-এর বেশি।")

            await status.edit_text(
                "🎨 ভিডিও প্রসেসিং চলছে...\n"
                "• Speed & hue transformation\n"
                "• Border pad & noise injection\n"
                "• Advanced audio reshaping"
            )

            async with PROCESS_SEMAPHORE:

                success, error = await asyncio.to_thread(
                    process_video_advanced,
                    input_file,
                    output_file,
                )

            if not success:

                update_user_credit(user_id, VIDEO_COST)
                refunded = True

                await status.edit_text(
                    "❌ ভিডিও প্রসেস করা যায়নি।\n"
                    "৫ ক্রেডিট ফেরত দেওয়া হয়েছে।\n\n"
                    f"<code>{html.escape(error)}</code>",
                    parse_mode=ParseMode.HTML,
                )

                return

            await status.edit_text(
                "📤 ভিডিও আপলোড করা হচ্ছে..."
            )

            with open(output_file, "rb") as f:

                await message.reply_video(
                    video=f,
                    caption=(
                        "🎉 <b>ভিডিও প্রসেসিং সম্পন্ন!</b>\n\n"
                        f"💎 কাটা হয়েছে: {VIDEO_COST} ক্রেডিট\n"
                        f"বর্তমান ব্যালেন্স: {remaining} ক্রেডিট"
                    ),
                    parse_mode=ParseMode.HTML,
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                )

            delivered = True

            await status.delete()

        except Exception as e:

            logger.exception("Video processing error")

            if not delivered and not refunded:
                update_user_credit(user_id, VIDEO_COST)
                refunded = True

            try:
                await status.edit_text(
                    "⚠️ ভিডিও প্রসেসিংয়ে সমস্যা হয়েছে।\n"
                    "ক্রেডিট ফেরত দেওয়া হয়েছে।\n\n"
                    f"<code>{html.escape(str(e))[:700]}</code>",
                    parse_mode=ParseMode.HTML,
                )

            except Exception:
                logger.exception("Status update failed")

        finally:

            for path in (input_file, output_file):

                try:
                    if os.path.exists(path):
                        os.remove(path)

                except OSError:
                    logger.warning("File cleanup failed: %s", path)


# =============== ADMIN COMMANDS ===============

async def cmd_addcredit(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        return

    if (
        len(context.args) < 2
        or not context.args[0].isdigit()
    ):

        await update.effective_message.reply_text(
            "ব্যবহার:\n/addcredit [user_id] [amount]"
        )

        return

    try:

        target = context.args[0]
        amount = int(context.args[1])

        if amount <= 0:
            raise ValueError

        balance = update_user_credit(target, amount)

        await update.effective_message.reply_text(
            f"✅ {amount} ক্রেডিট যোগ হয়েছে।\n"
            f"নতুন ব্যালেন্স: {balance}"
        )

    except ValueError:

        await update.effective_message.reply_text(
            "❌ Amount ধনাত্মক পূর্ণসংখ্যা হতে হবে।"
        )


async def cmd_checkuser(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        return

    if (
        not context.args
        or not context.args[0].isdigit()
    ):

        await update.effective_message.reply_text(
            "ব্যবহার:\n/checkuser [user_id]"
        )

        return

    target = context.args[0]
    db = load_db()

    if target not in db:

        await update.effective_message.reply_text(
            "এই ইউজার এখনো বট ব্যবহার করেনি।"
        )

        return

    data = db[target]

    await update.effective_message.reply_text(
        f"🔍 ইউজার ইনফো\n"
        f"ID: {target}\n"
        f"Balance: {data.get('credits', 0)}\n"
        f"Referrals: {data.get('referred_count', 0)}"
    )


# =============== CALLBACKS ===============

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if query is None:
        return

    user_id = str(query.from_user.id)

    if query.data == "home":

        await query.answer()

        data = get_user_data(user_id)

        text = (
            f"👥 Total Users: {get_total_users()}\n\n"
            "🌟 <b>মেইন মেনু</b>\n\n"
            f"💎 ব্যালেন্স: {data['credits']} ক্রেডিট\n\n"
            "ভিডিও পাঠিয়ে এডিট শুরু করুন।"
        )

        await query.message.edit_text(
            text,
            reply_markup=home_keyboard(
                query.from_user.id == ADMIN_ID
            ),
            parse_mode=ParseMode.HTML,
        )

    elif query.data == "ref_menu":

        await query.answer()

        data = get_user_data(user_id)

        ref_link = (
            f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        )

        text = (
            "🔥 <b>রেফারেল মেনু</b>\n\n"
            f"প্রতি সফল রেফারে {REFERRAL_CREDITS} ক্রেডিট পাবেন।\n\n"
            f"মোট রেফার: {data['referred_count']} জন\n\n"
            "আপনার লিংক:\n"
            f"<code>{html.escape(ref_link)}</code>"
        )

        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="home",
                    )
                ]
            ]),
            parse_mode=ParseMode.HTML,
        )

    elif query.data == "check_credits":

        data = get_user_data(user_id)

        await query.answer(
            f"বর্তমান ব্যালেন্স: {data['credits']} ক্রেডিট",
            show_alert=True,
        )

    elif query.data == "admin_help":

        if query.from_user.id != ADMIN_ID:

            await query.answer(
                "শুধু অ্যাডমিনের জন্য!",
                show_alert=True,
            )

            return

        await query.answer()

        await query.message.edit_text(
            "👑 <b>Admin Control Panel</b>\n\n"
            "/addcredit [user_id] [amount]\n\n"
            "/checkuser [user_id]",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="home",
                    )
                ]
            ]),
            parse_mode=ParseMode.HTML,
        )

    else:

        await query.answer()


# =============== ERROR HANDLER ===============

async def on_error(update, context):

    logger.error(
        "Update caused error",
        exc_info=context.error,
    )


# =============== MAIN ===============

def main():

    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN environment variable সেট করা নেই!"
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(60)
        .read_timeout(300)
        .write_timeout(300)
        .build()
    )

    app.add_handler(
        CommandHandler("start", cmd_start)
    )

    app.add_handler(
        CommandHandler("addcredit", cmd_addcredit)
    )

    app.add_handler(
        CommandHandler("checkuser", cmd_checkuser)
    )

    app.add_handler(
        CallbackQueryHandler(on_callback)
    )

    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO,
            handle_video,
        )
    )

    app.add_error_handler(on_error)

    logger.info(
        "Bot @%s is starting...",
        BOT_USERNAME,
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
