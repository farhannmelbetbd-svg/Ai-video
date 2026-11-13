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


def process_video_advanced(input_path: str, output_path: str) -> tuple[bool, str]:
    """
    ভিডিও প্রসেস করবে এবং সফল না হলে সুনির্দিষ্ট কারণ (Error stderr) রিটার্ন করবে।
    """
    try:
        cmd = [
            "ffmpeg",
            "-i", input_path,
            "-vf", "hflip,eq=brightness=0.02:contrast=1.03,scale=trunc(iw/2)*2:trunc(ih/2)*2,setpts=0.95*PTS",
            "-af", "atempo=1.05",
            "-c:v", "libx264",
            "-crf", "28",
            "-preset", "ultrafast",
            "-c:a", "aac",
            output_path,
            "-y"
        ]
        logger.info(f"Running command: {' '.join(cmd)}")
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        
        if result.returncode != 0:
            err_msg = result.stderr.strip()
            short_err = err_msg[-300:] if len(err_msg) > 300 else err_msg
            logger.error(f"FFmpeg failed: {short_err}")
            return False, f"FFmpeg Error Code {result.returncode}:\n{short_err}"
            
        return True, ""
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg process timed out!")
        return False, "Timed out: ভিডিওটি প্রসেস করতে নির্ধারিত সময়ের বেশি লেগেছে।"
    except Exception as e:
        logger.exception(f"Advanced video processing failed: {e}")
        return False, str(e)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏠 Home / Main Menu", callback_data="home")]
        ]
    )
    text = (
        "👋 <b>স্বাগতম! (Advanced Video Editor Bot)</b>\n\n"
        "আমাকে একটি ভিডিও পাঠান। আমি সেটিতে মিরর, কালার কারেকশন, স্পিড ও অডিও মডিফিকেশন করে একেবারে ইউনিক (কপিরাইট ফ্রি উপযোগী) করে আপনার সামনে ফিরিয়ে দেবো!"
    )
    
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    video = message.video

    if not video:
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ Back", callback_data="home"), InlineKeyboardButton("🏠 Home", callback_data="home")]
        ]
    )
    status_msg = await message.reply_text("📥 ভিডিও ডাউনলোড করা হচ্ছে, একটু অপেক্ষা করুন...", reply_markup=keyboard)

    input_file = f"input_{message.chat_id}_{message.message_id}.mp4"
    output_file = f"output_{message.chat_id}_{message.message_id}.mp4"

    try:
        file = await context.bot.get_file(video.file_id, read_timeout=60, write_timeout=60, connect_timeout=60)
        await file.download_to_drive(input_file)

        await status_msg.edit_text(
            "⚙️ <b>ভিডিও প্রসেসিং চলছে...</b>\n"
            "• মিরর (Horizontal Flip) করা হচ্ছে\n"
            "• কালার গ্রেডিং ও ব্রাইটনেস অ্যাডজাস্ট করা হচ্ছে\n"
            "• স্পিড ও অডিও মডিফাই করা হচ্ছে...",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )

        success, error_detail = await asyncio.to_thread(process_video_advanced, input_file, output_file)

        if not success or not os.path.exists(output_file):
            await status_msg.edit_text(
                f"❌ <b>ভিডিও প্রসেস করতে সমস্যা হয়েছে!</b>\n\n"
                f"<b>কারণ:</b>\n<code>{error_detail}</code>",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            return

        await status_msg.edit_text("📤 এডিট সম্পন্ন! ভিডিও আপলোড করা হচ্ছে...", reply_markup=keyboard)

        with open(output_file, "rb") as f:
            await message.reply_video(
                video=f,
                caption="✅ <b>আপনার ভিডিওটি সফলভাবে এডিট করা হয়েছে!</b>\n"
                        "<i>(Horizontal Flip, Color Adjust, Speed & Audio Modified)</i>",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
        
        await status_msg.delete()

    except Exception as exc:
        logger.exception("Error handling video processing")
        await status_msg.edit_text(
            f"⚠️ <b>একটি ত্রুটি ঘটেছে:</b>\n<code>{exc}</code>",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
    
    finally:
        if os.path.exists(input_file):
            os.remove(input_file)
        if os.path.exists(output_file):
            os.remove(output_file)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    if query.data == "home":
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🏠 Home / Main Menu", callback_data="home")]
            ]
        )
        await query.message.edit_text(
            "🏠 <b>মেইন মেনু</b>\n\n"
            "যেকোনো সময় কপিরাইট ফ্রি করার জন্য একটি ভিডিও পাঠান!",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )


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
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_error_handler(on_error)

    logger.info("🚀 Advanced Video Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
