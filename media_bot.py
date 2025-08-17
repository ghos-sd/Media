import os
import logging
import subprocess
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# إعداد اللوج
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# المتغيرات من Environment
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_IDS = os.getenv("ALLOWED_IDS", "")
MAX_MB = int(os.getenv("MAX_MB", "70"))

# تحويل IDs لقائمة
ALLOWED_IDS = [int(x) for x in ALLOWED_IDS.split(",") if x.strip().isdigit()]

# دالة التحقق
def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_IDS

# أمر /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("❌ غير مسموح لك باستخدام هذا البوت.")
        return
    await update.message.reply_text("✅ أهلاً! أرسل لي رابط تيك توك أو يوتيوب.")

# التعامل مع الروابط
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ غير مسموح لك باستخدام هذا البوت.")
        return

    url = update.message.text.strip()
    await update.message.reply_text("⏳ جاري التحميل...")

    try:
        # تحميل الفيديو باستخدام yt-dlp
        cmd = [
            "yt-dlp",
            "-f", "mp4",
            "--no-warnings",
            "--output", "download.%(ext)s",
            url
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="ignore"
        )

        if result.returncode != 0:
            logger.error(f"yt-dlp error: {result.stderr}")
            await update.message.reply_text("❌ فشل التحميل. الرابط غير صالح أو حدث خطأ.")
            return

        # البحث عن الملف الناتج
        for fname in os.listdir("."):
            if fname.startswith("download"):
                size_mb = os.path.getsize(fname) / (1024 * 1024)
                if size_mb > MAX_MB:
                    await update.message.reply_text(f"⚠️ الملف أكبر من {MAX_MB}MB.")
                    os.remove(fname)
                    return

                await update.message.reply_video(video=open(fname, "rb"))
                os.remove(fname)
                return

        await update.message.reply_text("❌ لم يتم العثور على الملف بعد التحميل.")

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await update.message.reply_text("⚠️ حصل خطأ غير متوقع.")


def main():
    try:
        application = Application.builder().token(BOT_TOKEN).build()

        # Handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        # تشغيل البوت
        application.run_polling()
    except Exception as e:
        logger.error(f"Bot crashed: {e}")


if __name__ == "__main__":
    main()
