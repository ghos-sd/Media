import re, hashlib, logging
from pathlib import Path
from tempfile import TemporaryDirectory
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from .downloader import build_yt_dlp_cmd, reencode_to_mp4, convert_to_mp3
from .utils import run_blocking_cmd, is_valid_file
from .config import CONFIG

URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)
ALLOWED_IDS = {int(x) for x in (CONFIG.get("ALLOWED_IDS", "").split()) if x.isdigit()}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"أرسل رابط يوتيوب/تيك توك.\n- أكتب mp3 مع الرابط لو عايز صوت فقط.\n- الحد الأقصى: {CONFIG['MAX_FILE_SIZE_MB']}MB.")

def is_authorized(update: Update):
    if not ALLOWED_IDS:
        return True
    return update.effective_user and update.effective_user.id in ALLOWED_IDS

async def handle_media_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("🚫 غير مسموح.")
        return

    text = (update.effective_message.text or "").strip()
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("أرسل رابط صالح.")
        return

    url = m.group(1)
    is_tiktok = "tiktok.com" in url
    as_audio = "mp3" in text.lower()

    with TemporaryDirectory(prefix="tg_dl_", dir="/tmp") as tmp:
        temp_dir = Path(tmp)
        base_filename = f"media_{hashlib.md5(url.encode()).hexdigest()[:10]}_{update.effective_message.id}"
        output_template = temp_dir / (base_filename + (".%(ext)s" if not is_tiktok else ".mp4"))

        await update.effective_chat.send_action(ChatAction.TYPING)
        status_message = await update.message.reply_text("⏳ جاري التنزيل...")

        try:
            cmd = build_yt_dlp_cmd(url, output_template, as_audio)
            result_output = await run_blocking_cmd(cmd)

            if not result_output:
                await status_message.edit_text("❌ فشل التنزيل. قد يكون الرابط خاص أو غير متوفر.")
                return

            downloaded_files = sorted(temp_dir.glob(f"{base_filename}*"), key=lambda p: p.stat().st_mtime, reverse=True)
            final_file = next((f for f in downloaded_files if is_valid_file(f)), None)

            if not final_file:
                await status_message.edit_text("❌ الملف الناتج غير صالح.")
                return

            processed_file = final_file
            if is_tiktok and CONFIG["FORCE_REENCODE_TT"]:
                fixed_path = temp_dir / f"{base_filename}_fixed.mp4"
                if await reencode_to_mp4(final_file, fixed_path):
                    processed_file = fixed_path

            if not is_tiktok and as_audio and processed_file.suffix.lower() not in (".mp3", ".m4a"):
                mp3_path = temp_dir / f"{base_filename}.mp3"
                if await convert_to_mp3(processed_file, mp3_path):
                    processed_file = mp3_path

            try:
                size_mb = processed_file.stat().st_size / (1024 * 1024)
                caption = f"تم ✅ الحجم: {size_mb:.1f}MB"

                if processed_file.suffix.lower() in (".mp3", ".m4a", ".aac", ".ogg"):
                    await update.message.reply_audio(audio=processed_file.open("rb"), caption=caption, filename=processed_file.name)
                else:
                    await update.message.reply_video(video=processed_file.open("rb"), caption=caption, filename=processed_file.name)

                await status_message.delete()

            except Exception as e:
                logging.exception("Telegram send error: %s", e)
                await status_message.edit_text("⚠️ حدث خطأ أثناء إرسال الملف.")

        except Exception as e:
            logging.exception("Unexpected error: %s", e)
            await status_message.edit_text("⚠️ حصل خطأ غير متوقع.")
