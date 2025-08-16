#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, shlex, asyncio, tempfile, time
from typing import Optional, Tuple, Dict, Any, List

from dotenv import load_dotenv
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------- Config ----------
load_dotenv()
MAX_MB = float(os.getenv("MAX_MB", "70"))
TARGET_BYTES = int(MAX_MB * 1024 * 1024)
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x.isdigit()}
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "10"))
_last_call: Dict[int, float] = {}

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN not set")

# ---------- Helpers ----------
def is_youtube(url: str) -> bool:
    u = url.lower()
    return ("youtube.com" in u) or ("youtu.be" in u)

def is_tiktok(url: str) -> bool:
    u = url.lower()
    return ("tiktok.com" in u) or ("vt.tiktok.com" in u) or ("tt.tiktok.com" in u)

def yt_dlp_base(url: str) -> str:
    base = "yt-dlp --no-call-home --no-warnings"
    if is_tiktok(url):
        base += ' --extractor-args "tiktok:hd=1" --referer https://www.tiktok.com/'
    return base

async def run_cmd(cmd: str) -> Tuple[int, str, str]:
    p = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await p.communicate()
    return p.returncode, out.decode("utf-8", "ignore"), err.decode("utf-8", "ignore")

async def probe_info(url: str) -> Dict[str, Any]:
    code, out, err = await run_cmd(yt_dlp_base(url) + f" -J {shlex.quote(url)}")
    if code != 0:
        raise RuntimeError(f"yt-dlp probe failed: {err.strip() or out.strip()}")
    import json as _json
    data = _json.loads(out)
    if data.get("_type") == "playlist" and data.get("entries"):
        data = data["entries"][0]
    return data

def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-. ]+", "_", name).strip() or "video"

def pick_direct_under_limit(formats: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    cand = []
    for f in formats or []:
        if f.get("ext") != "mp4": continue
        if f.get("acodec") in (None, "none"): continue
        if f.get("vcodec") in (None, "none"): continue
        size = f.get("filesize") or f.get("filesize_approx")
        if size and size <= TARGET_BYTES:
            cand.append((size, f))
    if not cand: return None
    cand.sort(key=lambda x: (x[0], x[1].get("height", 10**9)))
    return cand[0][1]

def compute_av_bitrates(duration: float, target_bytes: int, audio_kbps: int = 96) -> Tuple[int, int]:
    duration = max(duration, 1.0)
    total_kbps = (target_bytes * 8) / duration / 1000.0
    v = int(max(total_kbps - audio_kbps, 280))
    return v, audio_kbps

def scale_for(vkbps: int) -> str:
    if vkbps < 500: return "scale='min(640,iw)':'min(360,ih)':force_original_aspect_ratio=decrease"
    if vkbps < 900: return "scale='min(854,iw)':'min(480,ih)':force_original_aspect_ratio=decrease"
    return "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease"

def compute_audio_kbps(duration: float, target_bytes: int, ceiling: int = 320) -> int:
    duration = max(duration, 1.0)
    total_kbps = (target_bytes * 8) / duration / 1000.0
    return int(max(min(total_kbps - 16, ceiling), 64))

# ---------- Downloaders ----------
async def download_best(url: str, folder: str) -> Tuple[str, Optional[str]]:
    info = await probe_info(url)
    title = sanitize(info.get("title") or "video")
    duration = float(info.get("duration") or 0)
    fmts = info.get("formats") or []

    choice = pick_direct_under_limit(fmts)
    if choice:
        out_tmpl = os.path.join(folder, f"{title}.%(ext)s")
        code, out, err = await run_cmd(yt_dlp_base(url) + f" -f {shlex.quote(choice['format_id'])} -o {shlex.quote(out_tmpl)} {shlex.quote(url)}")
        if code != 0:
            raise RuntimeError(f"yt-dlp direct download failed: {err.strip() or out.strip()}")
        for n in os.listdir(folder):
            if n.endswith(".mp4") and os.path.getsize(os.path.join(folder, n)) <= TARGET_BYTES:
                return os.path.join(folder, n), None

    out_tmpl = os.path.join(folder, f"{title}.%(ext)s")
    code, out, err = await run_cmd(yt_dlp_base(url) + f' -f "bv*+ba/b" -o {shlex.quote(out_tmpl)} --merge-output-format mp4 {shlex.quote(url)}')
    if code != 0:
        raise RuntimeError(f"yt-dlp best download failed: {err.strip() or out.strip()}")

    input_path = None
    for n in os.listdir(folder):
        if n.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
            input_path = os.path.join(folder, n); break
    if not input_path:
        raise RuntimeError("Downloaded file not found")

    if os.path.getsize(input_path) <= TARGET_BYTES:
        return input_path, None

    if duration <= 0: duration = 30.0
    vkbps, akbps = compute_av_bitrates(duration, TARGET_BYTES)
    scale = scale_for(vkbps)
    out_fit = os.path.join(folder, f"{title}.fit.mp4")
    ff = (
        f"ffmpeg -y -i {shlex.quote(input_path)} "
        f"-vf {shlex.quote(scale)} -c:v libx264 -preset veryfast -b:v {vkbps}k -maxrate {vkbps}k -bufsize {2*vkbps}k "
        f"-c:a aac -b:a {akbps}k -movflags +faststart {shlex.quote(out_fit)}"
    )
    code, out, err = await run_cmd(ff)
    if code != 0:
        raise RuntimeError(f"ffmpeg transcode failed: {err.strip() or out.strip()}")

    if os.path.getsize(out_fit) > TARGET_BYTES:
        audio_only = os.path.join(folder, f"{title}.m4a")
        code, out, err = await run_cmd(f"ffmpeg -y -i {shlex.quote(input_path)} -vn -c:a aac -b:a 96k {shlex.quote(audio_only)}")
        if code == 0 and os.path.getsize(audio_only) <= TARGET_BYTES:
            return audio_only, "تحويل لصوت فقط لعدم القدرة على إبقاء الفيديو تحت الحد"
        raise RuntimeError("حتى بعد الضغط الملف أكبر من الحد")
    return out_fit, "تم ضغط الفيديو ليتوافق مع الحد"

async def download_youtube_audio(url: str, folder: str) -> str:
    info = await probe_info(url)
    title = sanitize(info.get("title") or "audio")
    duration = float(info.get("duration") or 0)
    out_tmpl = os.path.join(folder, f"{title}.%(ext)s")

    code, out, err = await run_cmd(yt_dlp_base(url) + f' -f "bestaudio/b" -o {shlex.quote(out_tmpl)} {shlex.quote(url)}')
    if code != 0:
        raise RuntimeError(f"yt-dlp audio download failed: {err.strip() or out.strip()}")

    src = None
    for n in os.listdir(folder):
        if n.lower().endswith((".m4a", ".webm", ".opus", ".mp3", ".mp4", ".mkv", ".mov")):
            src = os.path.join(folder, n); break
    if not src:
        raise RuntimeError("Audio not found")

    if src.lower().endswith(".mp3") and os.path.getsize(src) <= TARGET_BYTES:
        return src

    if duration <= 0: duration = 60.0
    kbps = compute_audio_kbps(duration, TARGET_BYTES, 320)
    out_mp3 = os.path.join(folder, f"{title}.mp3")
    code, out, err = await run_cmd(f"ffmpeg -y -i {shlex.quote(src)} -vn -c:a libmp3lame -b:a {kbps}k {shlex.quote(out_mp3)}")
    if code != 0:
        raise RuntimeError(f"ffmpeg mp3 encode failed: {err.strip() or out.strip()}")

    if os.path.getsize(out_mp3) > TARGET_BYTES:
        kbps2 = max(64, kbps // 2)
        out_mp3b = os.path.join(folder, f"{title}.fit.mp3")
        code, out, err = await run_cmd(f"ffmpeg -y -i {shlex.quote(src)} -vn -c:a libmp3lame -b:a {kbps2}k {shlex.quote(out_mp3b)}")
        if code != 0 or os.path.getsize(out_mp3b) > TARGET_BYTES:
            raise RuntimeError("حتى بعد التخفيض الصوت أكبر من الحد")
        return out_mp3b
    return out_mp3

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_IDS:
        await update.message.reply_text("هذا بوت خاص. اطلب من المالك إضافتك.")
        return
    await update.message.reply_text(
        f"أرسل رابط تيك توك أو يوتيوب.\n"
        f"- تيك توك: محاولة تنزيل بدون واترمارك وتحت {int(MAX_MB)}MB.\n"
        f"- يوتيوب: باختار لك فيديو أو MP3."
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_IDS:
        await update.message.reply_text("هذا بوت خاص.")
        return
    now = time.time()
    if now - _last_call.get(uid, 0) < RATE_LIMIT_SECONDS:
        await update.message.reply_text("في تنزيل شغال.. ثواني.")
        return

    text = (update.message.text or "").strip()
    if not re.search(r"https?://", text):
        await update.message.reply_text("أرسل رابط صحيح.")
        return

    if is_youtube(text):
        context.user_data["pending_url"] = text
        kb = [[
            InlineKeyboardButton("🎬 فيديو", callback_data="yt:video"),
            InlineKeyboardButton("🎧 صوت (MP3)", callback_data="yt:audio"),
        ]]
        await update.message.reply_text("من يوتيوب؟ تختار شنو؟", reply_markup=InlineKeyboardMarkup(kb))
        return

    _last_call[uid] = now
    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)
    await update.message.reply_text("شغال…")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            final_path, note = await download_best(text, tmp)
            size_mb = os.path.getsize(final_path) / (1024 * 1024)
            cap = f"تم ✅ الحجم: {size_mb:.1f}MB"
            if note: cap += f"\n{note}"
            if final_path.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                await update.message.reply_video(InputFile(final_path), caption=cap)
            else:
                await update.message.reply_document(InputFile(final_path), caption=cap)
    except Exception as e:
        await update.message.reply_text(f"خطأ: {e}")
    finally:
        _last_call[uid] = time.time()

async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if uid not in ALLOWED_IDS:
        await q.edit_message_text("هذا بوت خاص.")
        return
    now = time.time()
    if now - _last_call.get(uid, 0) < RATE_LIMIT_SECONDS:
        await q.edit_message_text("في تنزيل شغال.. ثواني.")
        return
    url = context.user_data.get("pending_url")
    if not url:
        await q.edit_message_text("أرسل رابط يوتيوب تاني.")
        return

    _last_call[uid] = now
    await q.edit_message_text("شغال…")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            if q.data == "yt:audio":
                path = await download_youtube_audio(url, tmp)
                size_mb = os.path.getsize(path) / (1024 * 1024)
                await q.message.reply_audio(InputFile(path), caption=f"MP3 ✅ {size_mb:.1f}MB")
            else:
                path, note = await download_best(url, tmp)
                size_mb = os.path.getsize(path) / (1024 * 1024)
                cap = f"تم ✅ {size_mb:.1f}MB"
                if note: cap += f"\n{note}"
                if path.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                    await q.message.reply_video(InputFile(path), caption=cap)
                else:
                    await q.message.reply_document(InputFile(path), caption=cap)
    except Exception as e:
        await q.message.reply_text(f"خطأ: {e}")
    finally:
        _last_call[uid] = time.time()
        context.user_data.pop("pending_url", None)

def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(MessageHandler(filters.Entity("url") & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(on_choice))
    return app

if __name__ == "__main__":
    app = build_app()
    print("Bot is running...")
    app.run_polling(close_loop=False)
