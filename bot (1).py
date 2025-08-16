#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Private Telegram Bot: Download TikTok/YouTube (<= MAX_MB MiB)
- يوتيوب: يطلب منك تختار (فيديو / MP3) عند إرسال الرابط
- تيك توك: يحاول تنزيل نسخة بدون واترمارك قدر الإمكان
- ضغط تلقائي عبر ffmpeg لو الحجم تعدّى الحد
- وصول مقيّد عبر ALLOWED_IDS
"""

import os
import re
import shlex
import asyncio
import tempfile
import time
from typing import Optional, Tuple, Dict, Any, List

from dotenv import load_dotenv
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,   # <-- المهم: استيراد الهاندلر
)

# -------- Settings --------
load_dotenv()

# الحد الافتراضي 70MB (تقدر تغيّره من Environment Variables)
MAX_MB = float(os.getenv("MAX_MB", "70"))
TARGET_BYTES = int(MAX_MB * 1024 * 1024)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_IDS = {
    int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN not set. ضع التوكن في Environment Variables.")
if not ALLOWED_IDS:
    print("WARNING: ALLOWED_IDS فاضي. البوت حيرفض الكل لحد تضيف معرفات المستخدمين.")

# منع السبام (ثواني بين كل عملية لنفس المستخدم)
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "10"))
_last_call: Dict[int, float] = {}

# ---- Helpers ----
def is_youtube(url: str) -> bool:
    u = url.lower()
    return ("youtube.com" in u) or ("youtu.be" in u)

def compute_audio_bitrate(duration: float, target_bytes: int, ceiling_kbps: int = 320) -> int:
    """Return audio kbps up to ceiling that fits in target size."""
    duration = max(duration, 1.0)
    total_kbps = (target_bytes * 8) / duration / 1000.0
    # اترك هامش بسيط للحاوية (~16kbps)
    kbps = int(max(min(total_kbps - 16, ceiling_kbps), 64))
    return kbps

def is_tiktok(url: str) -> bool:
    return "tiktok.com" in url.lower() or "vt.tiktok.com" in url.lower() or "tt.tiktok.com" in url.lower()

def yt_dlp_base(url: str) -> str:
    """
    Base yt-dlp command; يفعّل إعدادات تيك توك لنسخة HD وبدون واترمارك قدر الإمكان.
    """
    base = 'yt-dlp --no-call-home --no-warnings'
    if is_tiktok(url):
        base += ' --extractor-args "tiktok:hd=1" --referer https://www.tiktok.com/'
    return base

async def run_cmd(cmd: str) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "ignore"), err.decode("utf-8", "ignore")

async def probe_info(url: str) -> Dict[str, Any]:
    # احصل على الميتاداتا من yt-dlp
    cmd = yt_dlp_base(url) + f' -J {shlex.quote(url)}'
    code, out, err = await run_cmd(cmd)
    if code != 0:
        raise RuntimeError(f"yt-dlp probe failed: {err.strip() or out.strip()}")
    import json as _json
    data = _json.loads(out)
    if data.get("_type") == "playlist" and data.get("entries"):
        data = data["entries"][0]
    return data

def sanitize_filename(name: str) -> str:
    import re as _re
    name = _re.sub(r"[^\w\-. ]+", "_", name, flags=_re.U)
    return name.strip() or "video"

def pick_direct_under_limit(formats: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    # فضّل mp4 (فيديو+صوت) وحجم معروف <= الحد
    candidates = []
    for f in formats or []:
        if not f.get("acodec") or f.get("acodec") == "none":
            continue
        if not f.get("vcodec") or f.get("vcodec") == "none":
            continue
        if f.get("ext", "") != "mp4":
            continue
        size = f.get("filesize") or f.get("filesize_approx")
        if size and size <= TARGET_BYTES:
            candidates.append((size, f))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1].get("height", 1e9)))
    return candidates[0][1]

def compute_bitrates(duration: float, target_bytes: int, audio_kbps: int = 96) -> Tuple[int, int]:
    """Return (video_kbps, audio_kbps) to fit within target_bytes."""
    duration = max(duration, 1.0)
    total_kbps = (target_bytes * 8) / duration / 1000.0
    video_kbps = int(max(total_kbps - audio_kbps, 280))
    return video_kbps, audio_kbps

def scaling_filter_for_bitrate(video_kbps: int) -> str:
    if video_kbps < 500:
        return "scale='min(640,iw)':'min(360,ih)':force_original_aspect_ratio=decrease"
    elif video_kbps < 900:
        return "scale='min(854,iw)':'min(480,ih)':force_original_aspect_ratio=decrease"
    else:
        return "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease"

async def download_best(url: str, folder: str) -> Tuple[str, Optional[str]]:
    """
    نزّل أفضل نسخة تحت الحد إن أمكن؛ وإلا نزّل الأفضل ثم اضغط/أعد الترميز.
    يعيد (final_path, note)
    """
    info = await probe_info(url)
    title = sanitize_filename(info.get("title") or "video")
    duration = float(info.get("duration") or 0)
    formats = info.get("formats") or []

    # 1) جرّب صيغة مباشرة تحت الحد
    chosen = pick_direct_under_limit(formats)
    if chosen:
        fmt_id = chosen["format_id"]
        out_tmpl = os.path.join(folder, f"{title}.%(ext)s")
        cmd = yt_dlp_base(url) + f' -f {shlex.quote(fmt_id)} -o {shlex.quote(out_tmpl)} {shlex.quote(url)}'
        code, out, err = await run_cmd(cmd)
        if code != 0:
            raise RuntimeError(f"yt-dlp direct download failed: {err.strip() or out.strip()}")
        for name in os.listdir(folder):
            if name.endswith(".mp4"):
                path = os.path.join(folder, name)
                if os.path.getsize(path) <= TARGET_BYTES:
                    return path, None
        # لو لسه كبير بالصدفة، هنكمّل لإعادة الترميز

    # 2) نزّل أفضل جودة متاحة ثم افحص الحجم
    out_tmpl = os.path.join(folder, f"{title}.%(ext)s")
    cmd = yt_dlp_base(url) + f' -f "bv*+ba/b" -o {shlex.quote(out_tmpl)} --merge-output-format mp4 {shlex.quote(url)}'
    code, out, err = await run_cmd(cmd)
    if code != 0:
        raise RuntimeError(f"yt-dlp best download failed: {err.strip() or out.strip()}')

    # ابحث عن الملف الناتج
    input_path = None
    for name in os.listdir(folder):
        if name.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
            input_path = os.path.join(folder, name)
            break
    if not input_path:
        raise RuntimeError("Downloaded file not found.")

    if os.path.getsize(input_path) <= TARGET_BYTES:
        return input_path, None

    # 3) إعادة ترميز ببتريت محسوب + تصغير أبعاد مناسب
    if duration <= 0:
        duration = 30.0
    v_kbps, a_kbps = compute_bitrates(duration, TARGET_BYTES)
    scale = scaling_filter_for_bitrate(v_kbps)
    output_path = os.path.join(folder, f"{title}.fit.mp4")
    ffmpeg_cmd = (
        f"ffmpeg -y -i {shlex.quote(input_path)} "
        f"-vf {shlex.quote(scale)} "
        f"-c:v libx264 -preset veryfast -b:v {v_kbps}k -maxrate {v_kbps}k -bufsize {2*v_kbps}k "
        f"-c:a aac -b:a {a_kbps}k -movflags +faststart "
        f"{shlex.quote(output_path)}"
    )
    code, out, err = await run_cmd(ffmpeg_cmd)
    if code != 0:
        raise RuntimeError(f"ffmpeg transcode failed: {err.strip() or out.strip()}")

    if os.path.getsize(output_path) > TARGET_BYTES:
        # آخر محاولة: صوت فقط
        audio_out = os.path.join(folder, f"{title}.m4a")
        cmd_a = f"ffmpeg -y -i {shlex.quote(input_path)} -vn -c:a aac -b:a 96k {shlex.quote(audio_out)}"
        code, out, err = await run_cmd(cmd_a)
        if code == 0 and os.path.getsize(audio_out) <= TARGET_BYTES:
            return audio_out, "تم التحويل لصوت فقط لأن الفيديو ما قدرنا نخليه تحت الحد."
        else:
            raise RuntimeError("حتى بعد الضغط، الملف أكبر من الحد المسموح.")
    return output_path, "تم ضغط الفيديو ليتوافق مع حد الحجم."

async def download_youtube_audio(url: str, folder: str) -> str:
    """نزّل أفضل صوت من يوتيوب وحوّله MP3 بأعلى جودة ممكنة حتى 320kbps ضمن الحد."""
    info = await probe_info(url)
    title = sanitize_filename(info.get("title") or "audio")
    duration = float(info.get("duration") or 0)
    out_tmpl = os.path.join(folder, f"{title}.%(ext)s")

    # نزّل bestaudio
    cmd = yt_dlp_base(url) + f' -f "bestaudio/b" -o {shlex.quote(out_tmpl)} {shlex.quote(url)}'
    code_rc, out, err = await run_cmd(cmd)
    if code_rc != 0:
        raise RuntimeError(f"yt-dlp audio download failed: {err.strip() or out.strip()}")

    input_path = None
    for name in os.listdir(folder):
        if name.lower().endswith((".m4a", ".webm", ".mp3", ".mp4", ".mkv", ".opus", ".mov")):
            input_path = os.path.join(folder, name)
            break
    if not input_path:
        raise RuntimeError("Audio file not found after download.")

    if input_path.lower().endswith(".mp3") and os.path.getsize(input_path) <= TARGET_BYTES:
        return input_path

    if duration <= 0:
        duration = 60.0
    kbps = compute_audio_bitrate(duration, TARGET_BYTES, ceiling_kbps=320)

    mp3_out = os.path.join(folder, f"{title}.mp3")
    ff_cmd = f"ffmpeg -y -i {shlex.quote(input_path)} -vn -c:a libmp3lame -b:a {kbps}k {shlex.quote(mp3_out)}"
    code_rc, out, err = await run_cmd(ff_cmd)
    if code_rc != 0:
        raise RuntimeError(f"ffmpeg mp3 encode failed: {err.strip() or out.strip()}")

    if os.path.getsize(mp3_out) > TARGET_BYTES:
        kbps2 = max(64, kbps // 2)
        mp3_out2 = os.path.join(folder, f"{title}.fit.mp3")
        ff_cmd2 = f"ffmpeg -y -i {shlex.quote(input_path)} -vn -c:a libmp3lame -b:a {kbps2}k {shlex.quote(mp3_out2)}"
        code_rc, out, err = await run_cmd(ff_cmd2)
        if code_rc != 0 or os.path.getsize(mp3_out2) > TARGET_BYTES:
            raise RuntimeError("حتى بعد التخفيض، الصوت أكبر من الحد.")
        return mp3_out2
    return mp3_out

# ---- Handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_IDS:
        await update.message.reply_text("هذا بوت خاص. اطلب من المالك إضافتك للقائمة المسموح بها.")
        return
    await update.message.reply_text(
        f"أهلاً! أرسل رابط تيك توك أو يوتيوب.\n"
        f"- تيك توك: تنزيل تلقائي بدون واترمارك (قدر الإمكان) وتحت {int(MAX_MB)}MB.\n"
        f"- يوتيوب: باختر ليك تنزل *فيديو أو MP3*.\n"
        "الاستخدام شخصي وتعليمي فقط."
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_IDS:
        await update.message.reply_text("هذا بوت خاص. اطلب من المالك إضافتك للقائمة المسموح بها.")
        return
    now = time.time()
    if now - _last_call.get(user_id, 0) < RATE_LIMIT_SECONDS:
        await update.message.reply_text("لحظة بس… في تنزيل شغال. جرّب بعد ثواني.")
        return

    text = (update.message.text or "").strip()
    if not re.search(r"https?://", text):
        await update.message.reply_text("أرسل رابط صحيح من تيك توك أو يوتيوب.")
        return

    # روابط يوتيوب → اعرض الاختيارات
    if is_youtube(text):
        context.user_data["pending_url"] = text
        keyboard = [[
            InlineKeyboardButton("🎬 فيديو", callback_data="yt:video"),
            InlineKeyboardButton("🎧 صوت (MP3)", callback_data="yt:audio"),
        ]]
        await update.message.reply_text("من يوتيوب؟ تختار شنو؟", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # تيك توك (أو غيره) → نزّل مباشرة
    _last_call[user_id] = now
    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)
    await update.message.reply_text("شغال على الرابط… ثواني ✨")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            final_path, note = await download_best(text, tmpdir)
            size_mb = os.path.getsize(final_path) / (1024 * 1024)
            cap = f"تم التحميل ✅ الحجم: {size_mb:.1f}MB"
            if note:
                cap += f"\n{note}"
            if final_path.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                await update.message.reply_video(video=InputFile(final_path), caption=cap)
            else:
                await update.message.reply_document(document=InputFile(final_path), caption=cap)
    except Exception as e:
        await update.message.reply_text(f"حصل خطأ: {str(e)}")
    finally:
        _last_call[user_id] = time.time()

async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in ALLOWED_IDS:
        await query.edit_message_text("هذا بوت خاص.")
        return
    now = time.time()
    if now - _last_call.get(user_id, 0) < RATE_LIMIT_SECONDS:
        await query.edit_message_text("لحظة بس… في تنزيل شغال. جرّب بعد ثواني.")
        return
    url = context.user_data.get("pending_url")
    if not url:
        await query.edit_message_text("ما لقيت الرابط. أرسل رابط يوتيوب تاني.")
        return
    _last_call[user_id] = now
    await query.edit_message_text("تمام.. جاري التحميل ✨")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            if query.data == "yt:audio":
                final_path = await download_youtube_audio(url, tmpdir)
                size_mb = os.path.getsize(final_path) / (1024 * 1024)
                cap = f"تم التحويل إلى MP3 ✅ الحجم: {size_mb:.1f}MB (أعلى جودة ممكنة ضمن الحد)"
                await query.message.reply_audio(audio=InputFile(final_path), caption=cap)
            else:
                final_path, note = await download_best(url, tmpdir)
                size_mb = os.path.getsize(final_path) / (1024 * 1024)
                cap = f"تم التحميل ✅ الحجم: {size_mb:.1f}MB"
                if note:
                    cap += f"\n{note}"
                if final_path.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                    await query.message.reply_video(video=InputFile(final_path), caption=cap)
                else:
                    await query.message.reply_document(document=InputFile(final_path), caption=cap)
    except Exception as e:
        await query.message.reply_text(f"حصل خطأ: {str(e)}")
    finally:
        _last_call[user_id] = time.time()
        context.user_data.pop("pending_url", None)

def build_app() -> Any:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    # نص/روابط
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(MessageHandler(filters.Entity("url") & ~filters.COMMAND, handle_url))
    # اختيارات الأزرار
    app.add_handler(CallbackQueryHandler(on_choice))
    return app

if __name__ == "__main__":
    app = build_app()
    print("Bot is running...")
    app.run_polling(close_loop=False)            continue
        ext = f.get("ext", "")
        if ext != "mp4":
            continue
        size = f.get("filesize") or f.get("filesize_approx")
        if size and size <= TARGET_BYTES:
            candidates.append((size, f))
    if not candidates:
        return None
    # Smallest that fits
    candidates.sort(key=lambda x: (x[0], x[1].get("height", 1e9)))
    return candidates[0][1]

def compute_bitrates(duration: float, target_bytes: int, audio_kbps: int = 96) -> Tuple[int, int]:
    """Return (video_kbps, audio_kbps) to fit within target_bytes."""
    duration = max(duration, 1.0)
    total_kbps = (target_bytes * 8) / duration / 1000.0
    # Reserve for audio
    video_kbps = int(max(total_kbps - audio_kbps, 280))
    return video_kbps, audio_kbps

def scaling_filter_for_bitrate(video_kbps: int) -> str:
    # Simple heuristic: lower bitrate => lower max resolution
    if video_kbps < 500:
        return "scale='min(640,iw)':'min(360,ih)':force_original_aspect_ratio=decrease"
    elif video_kbps < 900:
        return "scale='min(854,iw)':'min(480,ih)':force_original_aspect_ratio=decrease"
    else:
        return "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease"

async def download_best(url: str, folder: str) -> Tuple[str, Optional[str]]:
    """
    Attempt direct download under limit; else download best and transcode.
    Returns (final_path, note) where note may describe re-encoding.
    """
    info = await probe_info(url)
    title = sanitize_filename(info.get("title") or "video")
    duration = float(info.get("duration") or 0)
    formats = info.get("formats") or []

    # 1) Try direct pick
    chosen = pick_direct_under_limit(formats)
    if chosen:
        fmt_id = chosen["format_id"]
        out_tmpl = os.path.join(folder, f"{title}.%(ext)s")
        cmd = yt_dlp_base(url) + f' -f {shlex.quote(fmt_id)} -o {shlex.quote(out_tmpl)} {shlex.quote(url)}'
        code, out, err = await run_cmd(cmd)
        if code != 0:
            raise RuntimeError(f"yt-dlp direct download failed: {err.strip() or out.strip()}")
        # Find produced file (mp4 expected)
        for name in os.listdir(folder):
            if name.endswith(".mp4"):
                path = os.path.join(folder, name)
                if os.path.getsize(path) <= TARGET_BYTES:
                    return path, None
        # Fallback to re-encode if size still exceeds (rare)
        base_in = None
        for name in os.listdir(folder):
            if name.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
                base_in = os.path.join(folder, name)
                break
        if base_in:
            # fallthrough to re-encode
            pass
        else:
            raise RuntimeError("Failed to locate downloaded file.")

    # 2) Download best available (will re-encode)
    out_tmpl = os.path.join(folder, f"{title}.%(ext)s")
    cmd = yt_dlp_base(url) + f' -f "bv*+ba/b" -o {shlex.quote(out_tmpl)} --merge-output-format mp4 {shlex.quote(url)}'
    code, out, err = await run_cmd(cmd)
    if code != 0:
        raise RuntimeError(f"yt-dlp best download failed: {err.strip() or out.strip()}")

    # Locate the downloaded file
    input_path = None
    for name in os.listdir(folder):
        if name.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
            input_path = os.path.join(folder, name)
            break
    if not input_path:
        raise RuntimeError("Downloaded file not found.")

    # If already under limit, return it
    if os.path.getsize(input_path) <= TARGET_BYTES:
        return input_path, None

    # 3) Re-encode with target bitrate & scaling
    if duration <= 0:
        # If duration unknown, just try a conservative encode
        duration = 30.0
    v_kbps, a_kbps = compute_bitrates(duration, TARGET_BYTES)
    scale = scaling_filter_for_bitrate(v_kbps)
    output_path = os.path.join(folder, f"{title}.fit.mp4")
    ffmpeg_cmd = (
        f"ffmpeg -y -i {shlex.quote(input_path)} "
        f"-vf {shlex.quote(scale)} "
        f"-c:v libx264 -preset veryfast -b:v {v_kbps}k -maxrate {v_kbps}k -bufsize {2*v_kbps}k "
        f"-c:a aac -b:a {a_kbps}k -movflags +faststart "
        f"{shlex.quote(output_path)}"
    )
    code, out, err = await run_cmd(ffmpeg_cmd)
    if code != 0:
        raise RuntimeError(f"ffmpeg transcode failed: {err.strip() or out.strip()}")

    if os.path.getsize(output_path) > TARGET_BYTES:
        # As a last resort, try audio-only extract if video cannot fit
        audio_out = os.path.join(folder, f"{title}.m4a")
        cmd_a = f"ffmpeg -y -i {shlex.quote(input_path)} -vn -c:a aac -b:a 96k {shlex.quote(audio_out)}"
        code, out, err = await run_cmd(cmd_a)
        if code == 0 and os.path.getsize(audio_out) <= TARGET_BYTES:
            return audio_out, "تم التحويل لصوت فقط لأن الفيديو ما قدرنا نخليه تحت الحد."
        else:
            raise RuntimeError("حتى بعد الضغط، الملف أكبر من الحد المسموح.")
    return output_path, "تم ضغط الفيديو ليتوافق مع حد الحجم."


async def download_youtube_audio(url: str, folder: str) -> str:
    """Download bestaudio and convert to MP3 with highest possible quality within TARGET_BYTES (up to 320kbps)."""
    info = await probe_info(url)
    title = sanitize_filename(info.get("title") or "audio")
    duration = float(info.get("duration") or 0)
    out_tmpl = os.path.join(folder, f"{title}.%(ext)s")

    # Try to directly get bestaudio
    cmd = yt_dlp_base(url) + f' -f "bestaudio/b" -o {shlex.quote(out_tmpl)} {shlex.quote(url)}'
    code_rc, out, err = await run_cmd(cmd)
    if code_rc != 0:
        raise RuntimeError(f"yt-dlp audio download failed: {err.strip() or out.strip()}")

    # locate audio/video file
    input_path = None
    for name in os.listdir(folder):
        if name.lower().endswith((".m4a", ".webm", ".mp3", ".mp4", ".mkv", ".opus", ".mov")):
            input_path = os.path.join(folder, name)
            break
    if not input_path:
        raise RuntimeError("Audio file not found after download.")

    # If already MP3 and under limit, return as-is
    if input_path.lower().endswith(".mp3") and os.path.getsize(input_path) <= TARGET_BYTES:
        return input_path

    # Compute target bitrate
    if duration <= 0:
        duration = 60.0  # conservative
    kbps = compute_audio_bitrate(duration, TARGET_BYTES, ceiling_kbps=320)

    mp3_out = os.path.join(folder, f"{title}.mp3")
    ff_cmd = f"ffmpeg -y -i {shlex.quote(input_path)} -vn -c:a libmp3lame -b:a {kbps}k {shlex.quote(mp3_out)}"
    code_rc, out, err = await run_cmd(ff_cmd)
    if code_rc != 0:
        raise RuntimeError(f"ffmpeg mp3 encode failed: {err.strip() or out.strip()}")

    if os.path.getsize(mp3_out) > TARGET_BYTES:
        # As last resort, reduce bitrate further
        kbps2 = max(64, kbps // 2)
        mp3_out2 = os.path.join(folder, f"{title}.fit.mp3")
        ff_cmd2 = f"ffmpeg -y -i {shlex.quote(input_path)} -vn -c:a libmp3lame -b:a {kbps2}k {shlex.quote(mp3_out2)}"
        code_rc, out, err = await run_cmd(ff_cmd2)
        if code_rc != 0 or os.path.getsize(mp3_out2) > TARGET_BYTES:
            raise RuntimeError("حتى بعد التخفيض، الصوت أكبر من الحد.")
        return mp3_out2
    return mp3_out

# ---- Handlers ----

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_IDS:
        await update.message.reply_text("هذا بوت خاص. اطلب من المالك إضافتك للقائمة المسموح بها.")
        return
    await update.message.reply_text(
        f"أهلاً! أرسل رابط تيك توك أو يوتيوب.
"
        f"- تيك توك: تنزيل تلقائي بدون واترمارك (قدر الإمكان) وتحت {int(MAX_MB)}MB.
"
        f"- يوتيوب: باختر ليك تنزل *فيديو أو MP3*.
"
        "الاستخدام شخصي وتعليمي فقط."
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_IDS:
        await update.message.reply_text("هذا بوت خاص. اطلب من المالك إضافتك للقائمة المسموح بها.")
        return
    now = time.time()
    if now - _last_call.get(user_id, 0) < RATE_LIMIT_SECONDS:
        await update.message.reply_text("لحظة بس… في تنزيل شغال. جرّب بعد ثواني.")
        return
    text = (update.message.text or "").strip()
    if not re.search(r"https?://", text):
        await update.message.reply_text("أرسل رابط صحيح من تيك توك أو يوتيوب.")
        return

    # YouTube => ask choice
    if is_youtube(text):
        context.user_data["pending_url"] = text
        keyboard = [
            [InlineKeyboardButton("🎬 فيديو", callback_data="yt:video"),
             InlineKeyboardButton("🎧 صوت (MP3)", callback_data="yt:audio")]
        ]
        await update.message.reply_text("من يوتيوب؟ تختار شنو؟", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # TikTok or others => proceed standard
    _last_call[user_id] = now
    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)
    await update.message.reply_text("شغال على الرابط… ثواني ✨")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            final_path, note = await download_best(text, tmpdir)
            size_mb = os.path.getsize(final_path) / (1024*1024)
            cap = f"تم التحميل ✅ الحجم: {size_mb:.1f}MB"
            if note:
                cap += f"
{note}"
            if final_path.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                await update.message.reply_video(video=InputFile(final_path), caption=cap)
            else:
                await update.message.reply_document(document=InputFile(final_path), caption=cap)
    except Exception as e:
        await update.message.reply_text(f"حصل خطأ: {str(e)}")
    finally:
        _last_call[user_id] = time.time()

async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in ALLOWED_IDS:
        await query.edit_message_text("هذا بوت خاص.")
        return
    now = time.time()
    if now - _last_call.get(user_id, 0) < RATE_LIMIT_SECONDS:
        await query.edit_message_text("لحظة بس… في تنزيل شغال. جرّب بعد ثواني.")
        return
    url = context.user_data.get("pending_url")
    if not url:
        await query.edit_message_text("ما لقيت الرابط. أرسل رابط يوتيوب تاني.")
        return
    _last_call[user_id] = now

    choice = query.data
    await query.edit_message_text("تمام.. جاري التحميل ✨")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            if choice == "yt:audio":
                final_path = await download_youtube_audio(url, tmpdir)
                size_mb = os.path.getsize(final_path) / (1024*1024)
                cap = f"تم التحويل إلى MP3 ✅ الحجم: {size_mb:.1f}MB (أعلى جودة ممكنة ضمن الحد)"
                await query.message.reply_audio(audio=InputFile(final_path), caption=cap)
            else:
                final_path, note = await download_best(url, tmpdir)
                size_mb = os.path.getsize(final_path) / (1024*1024)
                cap = f"تم التحميل ✅ الحجم: {size_mb:.1f}MB"
                if note:
                    cap += f"
{note}"
                if final_path.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                    await query.message.reply_video(video=InputFile(final_path), caption=cap)
                else:
                    await query.message.reply_document(document=InputFile(final_path), caption=cap)
    except Exception as e:
        await query.message.reply_text(f"حصل خطأ: {str(e)}")
    finally:
        _last_call[user_id] = time.time()
        context.user_data.pop("pending_url", None)

def build_app() -> Any:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(MessageHandler(filters.Entity("url") & ~filters.COMMAND, handle_url))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(telegram.ext.CallbackQueryHandler(on_choice))
    return app
if __name__ == "__main__":
    app = build_app()
    print("Bot is running...")
    app.run_polling(close_loop=False)
