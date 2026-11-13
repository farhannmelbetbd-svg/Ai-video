import asyncio
import json
import logging
import os
import subprocess
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 5293614793
DB_FILE = "users_db.json"

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_db(db):
    try:
        with open(DB_FILE, "w") as f:
            json.dump(db, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving database: {e}")

def get_total_users() -> int:
    db = load_db()
    return len(db)

def get_user_data(user_id: str):
    db = load_db()
    if user_id not in db:
        # নতুন ইউজার: ১০ ক্রেডিট বোনাস এবং রেফার্ড বাই নান
        db[user_id] = {"credits": 10, "referred_by": None, "referred_count": 0}
        save_db(db)
    return db[user_id]

def update_user_credit(user_id: str, amount: int):
    db = load_db()
    if user_id not in db:
        db[user_id] = {"credits": 10, "referred_by": None, "referred_count": 0}
    db[user_id]["credits"] += amount
    save_db(db)

def process_video_advanced(input_path: str, output_path: str) -> tuple[bool, str]:
    try:
        cmd = [
            "ffmpeg",
            "-i", input_path,
            "-vf", "hflip,eq=brightness=0.03:contrast=1.05:saturation=1.1,scale=trunc(iw*0.98/2)*2:trunc(ih*0.98/2)*2,setpts=0.93*PTS",
            "-af", "atempo=1.06,asetrate=44100*1.03",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-c:a", "aac",
            output_path,
            "-y"
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        
        if result.returncode != 0:
            err_msg = result.stderr.strip()
            short_err = err_msg[-300:] if len(err_msg) > 300 else err_msg
            return False, f"FFmpeg Error Code {result.returncode}:\n{short_err}"
            
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Timed out: ভিডিওটি প্রসেস করতে নির্ধারিত সময়ের বেশি লেগেছে।"
    except Exception as e:
        return False, str(e)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id_str = str(user.id)
    
    u_data = get_user_data(user_id_str)
    
    # রেফারেল হ্যান্ডলিং
    args = context.args
    if args and args[0].startswith("ref_"):
        referrer_id = args[0].split("_")[1]
        if referrer_id != user_id_str and u_data["referred_by"] is None:
            db = load_db()
            if referrer_id in db:
                u_data["referred_by"] = referrer_id
                # রেফারারকে ২০ ক্রেডিট বোনাস দেওয়া হলো
                db[referrer_id]["credits"] += 20
                db[referrer_id]["referred_count"] = db[referrer_id].get("referred_count", 0) + 1
                save_db(db)
                
                try:
                    await context.bot.send_message(
                        chat_id=int(referrer_id),
                        text=f"🎉 <b>দারুণ খবর!</b> আপনার রেফারেল লিংক থেকে একজন নতুন বন্ধু জয়েন করেছে।\n🎁 আপনার অ্যাকাউন্টে সফলভাবে যুক্ত হয়েছে <b>২০ ফ্রি ক্রেডিট!</b> 💎✨",
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

    credits = get_user_data(user_id_str)["credits"]
    total_users = get_total_users()
    
    bot_username = "Fast1panelbot"
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id_str}"
    
    keyboard = [
        [InlineKeyboardButton("🎁 প্রতি রেফারে ২০ ক্রেডিট আয় করুন!", callback_data="ref_menu")],
        [InlineKeyboardButton("💎 ব্যালেন্স চেক", callback_data="check_credits"), InlineKeyboardButton("⚡ রিচার্জ (WhatsApp)", url="https://wa.me/8801791110761")],
        [InlineKeyboardButton("🏠 মেইন মেনু (Home)", callback_data="home")]
    ]
    
    if user.id == ADMIN_ID:
        keyboard.insert(0, [InlineKeyboardButton("👑 Admin Control Panel (Auto)", callback_data="admin_help")])

    markup = InlineKeyboardMarkup(keyboard)
    
    text = (
        f"👥 <b>Total Family Members: {total_users} Users</b> 🌐\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🌟 <b>স্বাগতম, {user.first_name}!</b> ✨\n\n"
        f"🔥 যেকোনো ভিডিওকে মুহূর্তেই ইউনিক ও কপিরাইট ফ্রি করে নিন কোনো ঝামেলা ছাড়াই!\n\n"
        f"🎁 <b>আপনার বর্তমান ব্যালেন্স:</b> <b>{credits} ক্রেডিট</b> 💎\n"
        f"📌 <i>প্রতি ভিডিও এডিটে খরচ: ৫ ক্রেডিট</i>\n\n"
        f"💡 <i>বন্ধুদের রেফার করলেই পাচ্ছেন প্রতি রেফারে **২০ ক্রেডিট ফ্রি!** নিচে রেফারেল মেনু থেকে লিংক নিন।</i>\n\n"
        f"👇 <b>শুরু করতে চ্যাটে একটি ভিডিও পাঠিয়ে দিন!</b>"
    )
    
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    user_id_str = str(user.id)
    video = message.video

    if not video:
        return

    current_credits = get_user_data(user_id_str)["credits"]
    if current_credits < 5:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 ফ্রি ক্রেডিট পেতে রেফার করুন", callback_data="ref_menu")],
            [InlineKeyboardButton("⚡ এখনই রিচার্জ করুন (WhatsApp)", url="https://wa.me/8801791110761")],
            [InlineKeyboardButton("🏠 Home", callback_data="home")]
        ])
        await message.reply_text(
            "❌ <b>অপর্যাপ্ত ক্রেডিট ব্যালেন্স!</b> ⚠️\n\n"
            "এই ভিডিওটি এডিট করতে কমপক্ষে <b>৫ ক্রেডিট</b> প্রয়োজন। ফ্রি ক্রেডিট পেতে বন্ধুদের রেফার করুন অথবা WhatsApp-এ যোগাযোগ করে রিচার্জ করুন।",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="home"), InlineKeyboardButton("🏠 Home", callback_data="home")]
    ])
    status_msg = await message.reply_text("📥 <b>ভিডিও প্রসেসিং শুরু হয়েছে...</b> একটু অপেক্ষা করুন ⏳", reply_markup=keyboard, parse_mode=ParseMode.HTML)

    update_user_credit(user_id_str, -5)
    remaining = get_user_data(user_id_str)["credits"]

    input_file = f"input_{message.chat_id}_{message.message_id}.mp4"
    output_file = f"output_{message.chat_id}_{message.message_id}.mp4"

    try:
        file = await context.bot.get_file(video.file_id, read_timeout=60, write_timeout=60, connect_timeout=60)
        await file.download_to_drive(input_file)

        await status_msg.edit_text(
            "🎨 <b>ভিডিও ইউনিক করা হচ্ছে...</b> 🛠️\n"
            "• হরাইজন্টাল ফ্লিপ ও কালার গ্রেডিং 🔄\n"
            "• স্পিড ও অডিও পিচ মডিফিকেশন 🎵",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )

        success, error_detail = await asyncio.to_thread(process_video_advanced, input_file, output_file)

        if not success or not os.path.exists(output_file):
            update_user_credit(user_id_str, 5)
            await status_msg.edit_text(
                f"❌ <b>ভিডিও প্রসেস করতে সমস্যা হয়েছে!</b> আপনার ৫ ক্রেডিট রিফান্ড করা হয়েছে। 🔄\n\n"
                f"<b>ত্রুটির বিবরণ:</b>\n<code>{error_detail}</code>",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            return

        await status_msg.edit_text("📤 <b>এডিট সম্পন্ন! ভিডিও আপলোড করা হচ্ছে...</b> 🚀", reply_markup=keyboard, parse_mode=ParseMode.HTML)

        with open(output_file, "rb") as f:
            await message.reply_video(
                video=f,
                caption=f"🎉 <b>আপনার ভিডিওটি সফলভাবে কপিরাইট ফ্রি করা হয়েছে!</b> ✅\n\n"
                        f"💎 <i>৫ ক্রেডিট কাটা হয়েছে। বর্তমান ব্যালেন্স:</i> <b>{remaining} ক্রেডিট</b>",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
        
        await status_msg.delete()

    except Exception as exc:
        update_user_credit(user_id_str, 5)
        logger.exception("Error handling video processing")
        await status_msg.edit_text(
            f"⚠️ <b>একটি ত্রুটি ঘটেছে (ক্রেডিট রিফান্ড করা হয়েছে):</b>\n<code>{exc}</code>",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
    
    finally:
        if os.path.exists(input_file):
            os.remove(input_file)
        if os.path.exists(output_file):
            os.remove(output_file)


async def cmd_addcredit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        args = context.args
        if len(args) < 2:
            await update.effective_message.reply_text("⚠️ ব্যবহারবিধি: <code>/addcredit [user_id] [amount]</code>", parse_mode=ParseMode.HTML)
            return
        target_user = args[0]
        amount = int(args[1])
        update_user_credit(target_user, amount)
        new_balance = get_user_data(target_user)["credits"]
        await update.effective_message.reply_text(f"👑 <b>[ADMIN] সফল!</b> ইউজার <code>{target_user}</code>-কে <b>{amount} ক্রেডিট</b> দেওয়া হয়েছে। নতুন ব্যালেন্স: <b>{new_balance}</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ ত্রুটি: {e}")


async def cmd_checkuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        args = context.args
        if not args:
            await update.effective_message.reply_text("⚠️ ব্যবহারবিধি: <code>/checkuser [user_id]</code>", parse_mode=ParseMode.HTML)
            return
        target_user = args[0]
        u_data = get_user_data(target_user)
        await update.effective_message.reply_text(f"🔍 <b>ইউজার ইনফো:</b>\nID: <code>{target_user}</code>\n💎 ব্যালেন্স: <b>{u_data['credits']}</b>\n👥 রেফার করেছে: <b>{u_data.get('referred_count', 0)} জন</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ ত্রুটি: {e}")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id_str = str(query.from_user.id)
    
    if query.data == "home":
        u_data = get_user_data(user_id_str)
        total_users = get_total_users()
        keyboard = [
            [InlineKeyboardButton("🎁 প্রতি রেফারে ২০ ক্রেডিট আয় করুন!", callback_data="ref_menu")],
            [InlineKeyboardButton("💎 ব্যালেন্স চেক", callback_data="check_credits"), InlineKeyboardButton("⚡ রিচার্জ (WhatsApp)", url="https://wa.me/8801791110761")],
            [InlineKeyboardButton("🏠 মেইন মেনু (Home)", callback_data="home")]
        ]
        if query.from_user.id == ADMIN_ID:
            keyboard.insert(0, [InlineKeyboardButton("👑 Admin Control Panel (Auto)", callback_data="admin_help")])
            
        markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            f"👥 <b>Total Family Members: {total_users} Users</b> 🌐\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🌟 <b>মেইন মেনু</b> ✨\n\n"
            f"🎁 আপনার অ্যাকাউন্ট ব্যালেন্স: <b>{u_data['credits']} ক্রেডিট</b> 💎\n"
            f"কপিরাইট ফ্রি করার জন্য যেকোনো সময় একটি ভিডিও পাঠান!",
            reply_markup=markup,
            parse_mode=ParseMode.HTML
        )
        
    elif query.data == "ref_menu":
        bot_username = "Fast1panelbot"
        ref_link = f"https://t.me/{bot_username}?start=ref_{user_id_str}"
        u_data = get_user_data(user_id_str)
        ref_count = u_data.get("referred_count", 0)
        
        ref_text = (
            f"🔥 <b>রেফার করে ফ্রি ক্রেডিট আয় করুন!</b> 🎁✨\n\n"
            f"আপনার বন্ধু বা অন্য কাউকে এই বট রেফার করলেই আপনি পেয়ে যাবেন সুসংবাদস্বরূপ **২০ ক্রেডিট একবারে ফ্রি!** 💎\n\n"
            f"📊 <b>আপনার মোট রেফার:</b> <code>{ref_count} জন</code>\n"
            f"🔗 <b>আপনার পার্সোনাল রেফারেল লিংক:</b>\n<code>{ref_link}</code>\n\n"
            f"<i>লিংকটি কপি করে আপনার বন্ধুদের পাঠান এবং আনলিমিটেড ফ্রি ক্রেডিট উপভোগ করুন! 🚀</i>"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]])
        await query.message.edit_text(ref_text, reply_markup=markup, parse_mode=ParseMode.HTML)

    elif query.data == "check_credits":
        credits = get_user_data(user_id_str)["credits"]
        await query.answer(f"✨ আপনার বর্তমান ব্যালেন্স: {credits} ক্রেডিট 💎", show_alert=True)
        
    elif query.data == "admin_help":
        if query.from_user.id != ADMIN_ID:
            return
        help_text = (
            "👑 <b>অ্যাডমিন অটো কন্ট্রোল প্যানেল:</b> 🛠️\n\n"
            "১. ক্রেডিট যোগ করতে:\n<code>/addcredit [user_id] [amount]</code>\n\n"
            "২. ইউজার চেক করতে:\n<code>/checkuser [user_id]</code>"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]])
        await query.message.edit_text(help_text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update caused error", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN এনভায়রনমেন্ট ভেরিয়েবল সেট করা নেই!")
    
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(60)
        .read_timeout(60)
        .write_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("addcredit", cmd_addcredit))
    app.add_handler(CommandHandler("checkuser", cmd_checkuser))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_error_handler(on_error)

    logger.info("🚀 Ultimate Bot with @Fast1panelbot Referral System is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ ==---
__main__()
    
