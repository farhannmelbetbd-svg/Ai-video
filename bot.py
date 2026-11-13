import asyncio
import logging
import os
import subprocess
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
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


def process_video_advanced(input_path: str, output_path: str) -> bool:
    try:
        cmd = [
            "ffmpeg",
            "-i", input_path,
            "-vf", "hflip,eq=brightness=0.02:contrast=1.03,scale=trunc(iw/2)*2:trunc(ih/2)*2,setpts=0.95*PTS",
            "-af", "atempo=1.05",
            "-c:v", "libx264",
            "-crf", "23",
            "-preset", "fast",
            "-c:a", "aac",
            output_path,
            "-y"
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            logger.error(f"FFmpeg error: {result.stderr}")
            return False
        return True
    except Exception as e:
        logger.exception(f"Advanced video processing failed: {e}")
        return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "👋 <b>স্বাগতম! (Advanced Video Editor Bot)</b>\n\n"
        "আমাকে একটি ভিডিও পাঠান। আমি সেটিতে মিরর, কালার কারেকশন, স্পিড ও অডিও মডিফিকেশন করে একেবারে ইউনিক (কপিরাইট ফ্রি উপযোগী) করে আপনার সামনে ফিরিয়ে দেবো!",
        parse_mode=ParseMode.HTML,
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    video = message.video

    if not video:
        return

    status_msg = await message.reply_text("📥 ভিডিও ডাউনলোড করা হচ্ছে, একটু অপেক্ষা করুন...")

    input_file = f"input_{message.chat_id}_{message.message_id}.mp4"
    output_file = f"output_{message.chat_id}_{message.message_id}.mp4"

    try:
        file = await context.bot.get_file(video.file_id)
        await file.download_to_drive(input_file)

        await status_msg.edit_text(
            "⚙️ <b>ভিডিও প্রসেসিং চলছে...</b>\n"
            "• মিরর (Horizontal Flip) করা হচ্ছে\n"
            "• কালার গ্রেডিং ও ব্রাইটনেস অ্যাডজাস্ট করা হচ্ছে\n"
            "• স্পিড ও অডিও মডিফাই করা হচ্ছে...",
            parse_mode=ParseMode.HTML
        )

        success = await asyncio.to_thread(process_video_advanced, input_file, output_file)

        if not success or not os.path.exists(output_file):
            await status_msg.edit_text("❌ ভিডিও প্রসেস করতে গিয়ে সমস্যা হয়েছে। আবার চেষ্টা করুন।")
            return

        await status_msg.edit_text("📤 এডিট সম্পন্ন! ভিডিও আপলোড করা হচ্ছে...")

        with open(output_file, "rb") as f:
            await message.reply_video(
                video=f,
                caption="✅ <b>আপনার ভিডিওটি সফলভাবে এডিট করা হয়েছে!</b>\n"
                        "<i>(Horizontal Flip, Color Adjust, Speed & Audio Modified)</i>",
                parse_mode=ParseMode.HTML
            )
        
        await status_msg.delete()

    except Exception as exc:
        logger.exception("Error handling video processing")
        await status_msg.edit_text(f"⚠️ একটি ত্রুটি ঘটেছে: {exc}")
    
    finally:
        if os.path.exists(input_file):
            os.remove(input_file)
        if os.path.exists(output_file):
            os.remove(output_file)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error", update, exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN এনভায়রনমেন্ট ভেরিয়েবল সেট করা নেই!")
    
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_error_handler(on_error)

    logger.info("🚀 Advanced Video Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
