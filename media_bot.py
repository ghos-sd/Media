# media_bot.py
# Telegram bot to download YouTube/TikTok media with yt-dlp + ffmpeg
# Author: you :)

import os
import re
import sys
import json
import base64
import asyncio
import logging
import hashlib
import subprocess
from functools import partial
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import List, Set, Optional, Dict

import ffmpeg  # ffmpeg-python
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ========= Logging =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ========= Env & Config =========
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_IDS_RAW: str = os.getenv("ALLOWED_IDS", "").strip()

CONFIG_FILE = Path("config.json")
DEFAULT_CONFIG: Dict[str, object] = {
    "MAX_FILE_SIZE_MB": 70,                # Telegram limit (<= 70MB)
    "MIN_VALID_FILE_SIZE_BYTES": 200 * 1024,
    "FORCE_REENCODE_TT": True,             # Re-encode TikTok to h264/aac for compatibility
    "YT_COOKIE_PATH": "/app/cookies_youtube.txt",
    "YT_COOKIES_B64": "",
    "TIMEOUT_SECONDS": 120                 # Telegram send timeouts
}

def load_config() -> Dict[str, object]:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.is_file():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            logging.warning("Could not read config.json, using defaults.")
    # Override by env if present
    for k, v in list(cfg.items()):
        ev = os.getenv(k)
        if ev is None:
            continue
        if isinstance(v, bool):
            cfg[k] = ev.lower() in ("1", "true", "yes", "on")
        elif isinstance(v, int):
            try:
                cfg[k] = int(ev)
            except ValueError:
                logging.warning("Invalid int for %s: %s", k, ev)
        else:
            cfg[k] = ev
    return cfg

CONFIG = load_config()
MAX_FILE_SIZE_MB: int = int(CONFIG["MAX_FILE_SIZE_MB"])
MIN_VALID_FILE_SIZE_BYTES: int = int(CONFIG["MIN_VALID_FILE_SIZE_BYTES"])
FORCE_REENCODE_TT: bool = bool(CONFIG["FORCE_REENCODE_TT"])
YT_COOKIE_PATH: Path = Path(str(CONFIG["YT_COOKIE_PATH"]))
YT_COOKIES_B64: str = str(CONFIG["YT_COOKIES_B64"])
TIMEOUT_SECONDS: int = int(CONFIG["TIMEOUT_SECONDS"])

def parse_allowed_ids(s: str) -> Set[int]:
    if not s:
        return set()
    return {int(p) for p in re.split(r"[,\s]+", s) if p.strip().isdigit()}

ALLOWED_IDS: Set[int] = parse_allowed_ids(ALLOWED_IDS_RAW)

# ========= YouTube cookies =========
def write_youtube_cookies_file() -> bool:
    if not YT_COOKIES_B64:
        logging.warning("YT_COOKIES_B64 empty; YouTube may require login.")
        return False
    try:
        data = base64.b64decode(YT_COOKIES_B64)
        YT_COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
        YT_COOKIE_PATH.write_bytes(data)
        ok = YT_COOKIE_PATH.exists() and YT_COOKIE_PATH.stat().st_size > 200
        logging.info("Wrote cookies to %s (size=%d)", YT_COOKIE_PATH, YT_COOKIE_PATH.stat().st_size if ok else 0)
        return ok
    except Exception as e:
        logging.exception("Failed writing cookies: %s", e)
        return False

YOUTUBE_COOKIES_AVAILABLE: bool = write_youtube_cookies_file()

# ========= Helpers =========
async def run_blocking_cmd(cmd: List[str]) -> Optional[str]:
    """
    Run a blocking subprocess in a thread so we don't block the asyncio loop.
    Returns stdout text on success, None on failure.
    """
    logging.info("RUN: %s", " ".join(cmd))
    loop = asyncio.get_event_loop()
    try:
        call = partial(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
            errors="ignore",
        )
        proc = await loop.run_in_executor(None, call)
        logging.info("OK: %s", (proc.stdout or "")[-1200:])
        return proc.stdout
    except subprocess.CalledProcessError as e:
        logging.warning("FAIL rc=%s: %s", e.returncode, (e.stdout or "")[-1200:])
        return None
    except Exception as e:
        logging.exception("run_blocking_cmd error: %s", e)
        return None

def is_valid_file(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > MIN_VALID_FILE_SIZE_BYTES
    except Exception:
        return False

# ========= yt-dlp commands =========
def build_yt_dlp_cmd(url: str, out_path: Path, as_audio: bool = False) -> List[str]:
    is_tiktok = ("tiktok.com" in url) or ("vt.tiktok.com" in url)
    if is_tiktok:
        return [
            "yt-dlp",
            "-f", "mp4*+m4a/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--no-warnings",
            "--concurrent-fragments", "4",
            "--retries", "10",
            "--fragment-retries", "10",
            "--socket-timeout", "30",
            "--user-agent", "Mozilla/5.0",
            "--add-header", "Referer:https://www.tiktok.com/",
            "-o", str(out_path),
            url,
        ]
    # YouTube / others
    fmt = f"b[filesize<{MAX_FILE_SIZE_MB}M]/bv*+ba/best" if not as_audio else "bestaudio[abr<=128k]/bestaudio"
    cmd = [
        "yt-dlp",
        "-f", fmt,
        "--no-warnings",
        "--restrict-filenames",
        "--concurrent-fragments", "4",
        "--retries", "10",
        "--fragment-retries", "10",
        "--socket-timeout", "30",
        "--geo-bypass",
        "--encoding", "utf-8",
        "--user-agent", "Mozilla/5.0",
        "--extractor-args", "youtube:player_client=android",
        "-o", str(out_path),
        url,
    ]
    if YOUTUBE_COOKIES_AVAILABLE:
        cmd += ["--cookies", str(YT_COOKIE_PATH)]
    return cmd

async def reencode_to_mp4(in_path: Path, out_path: Path) -> bool:
    try:
        await asyncio.to_thread(
            lambda: ffmpeg
            .input(str(in_path))
            .output(str(out_path),
                    vcodec="libx264",
                    pix_fmt="yuv420p",
                    preset="veryfast",
                    movflags="+faststart",
                    acodec="aac")
            .run(overwrite_output=True, quiet=True)
        )
        return is_valid_file(out_path)
    except ffmpeg.Error as e:
        try:
            err = e.stderr.decode("utf-8", errors="ignore")
        except Exception:
            err = str(e)
        logging.error("ffmpeg re-encode failed: %s", err)
        return False
    except Exception as e:
        logging.exception("ffmpeg error: %s", e)
        return False

async def convert_to_mp3(in_path: Path, out_path: Path) -> bool:
    try:
        await asyncio.to_thread(
            lambda: ffmpeg
            .input(str(in_path))
            .output(str(out_path), acodec="libmp3lame", audio_bitrate="128k")
            .run(overwrite_output=True, quiet=True)
        )
        return is_valid_file(out_path)
    except ffmpeg.Error as e:
        try:
            err = e.stderr.decode("utf-8", errors="ignore")
        except Exception:
            err = str(e)
        logging.error("ffmpeg mp3 failed: %s", err)
        return False
    except Exception as e:
        logging.exception("ffmpeg mp3 error: %s", e)
        return False

# ========= Telegram =========
URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)

def is_authorized(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_IDS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"أرسل رابط يوتيوب / تيك توك.\n"
        f"- اكتب mp3 مع الرابط لو عايز صوت فقط.\n"
        f"- الحد الأقصى: {MAX_FILE_SIZE_MB}MB."
    )

async def handle_media_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("غير مسموح.")
        return

    text = (update.effective_message.text or "").strip()
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("أرسل رابط صالح.")
        return

    url = m.group(1)
    is_tiktok = ("tiktok.com" in url) or ("vt.tiktok.com" in url)
    as_audio = "mp3" in text.lower()

    with TemporaryDirectory(prefix="tg_dl_", dir="/tmp") as tmp:
        tmpdir = Path(tmp)
        base = f"media_{hashlib.md5(url.encode()).hexdigest()[:10]}_{update.effective_message.id}"
        out_tmpl = tmpdir / (base + (".mp4" if is_tiktok else ".%(ext)s"))

        await update.effective_chat.send_action(ChatAction.TYPING)
        status = await update.message.reply_text("⏳ جاري التنزيل...")

        try:
            cmd = build_yt_dlp_cmd(url, out_tmpl, as_audio)
            result = await run_blocking_cmd(cmd)
            if result is None:
                await status.edit_text("فشل التنزيل. ربما الرابط خاص أو غير متاح. جرّب رابط آخر.")
                return

            files = sorted(tmpdir.glob(f"{base}*"), key=lambda p: p.stat().st_mtime, reverse=True)
            final_file: Optional[Path] = next((p for p in files if is_valid_file(p)), None)
            if not final_file:
                await status.edit_text("فشل التنزيل. الملف الناتج غير صالح.")
                return

            processed = final_file
            if is_tiktok and FORCE_REENCODE_TT:
                fixed = tmpdir / f"{base}_fixed.mp4"
                if await reencode_to_mp4(final_file, fixed):
                    processed = fixed

            if (not is_tiktok) and as_audio and processed.suffix.lower() not in (".mp3", ".m4a"):
                mp3 = tmpdir / f"{base}.mp3"
                if await convert_to_mp3(processed, mp3):
                    processed = mp3

            try:
                size_mb = processed.stat().st_size / (1024 * 1024)
                caption = f"تم ✅ الحجم: {size_mb:.1f}MB"

                if processed.suffix.lower() in (".mp3", ".m4a", ".aac", ".ogg"):
                    await update.message.reply_audio(
                        audio=processed.open("rb"),
                        caption=caption,
                        filename=processed.name,
                        read_timeout=TIMEOUT_SECONDS,
                        write_timeout=TIMEOUT_SECONDS,
                    )
                else:
                    await update.message.reply_video(
                        video=processed.open("rb"),
                        caption=caption,
                        filename=processed.name,
                        read_timeout=TIMEOUT_SECONDS,
                        write_timeout=TIMEOUT_SECONDS,
                    )
                await status.delete()
            except Exception:
                logging.exception("Telegram send failed")
                await status.edit_text("حدث خطأ أثناء إرسال الملف.")

        except Exception as e:
            logging.exception("Unexpected error: %s", e)
            await status.edit_text("حصل خطأ غير متوقع. جرّب لاحقًا.")

# ========= Main =========
def main() -> None:
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN is missing!")
        raise SystemExit("BOT_TOKEN is missing!")

    # Ensure yt-dlp present
    try:
        v = subprocess.check_output(["yt-dlp", "--version"], text=True).strip()
        logging.info("yt-dlp version: %s", v)
    except (subprocess.CalledProcessError, FileNotFoundError):
        logging.error("yt-dlp is not installed or not in PATH")
        sys.exit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_media_request))

    logging.info("Bot is running…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()    try:
        data = base64.b64decode(YT_COOKIES_B64)
        YT_COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
        YT_COOKIE_PATH.write_bytes(data)
        ok = YT_COOKIE_PATH.is_file() and YT_COOKIE_PATH.stat().st_size > 200
        logging.info(
            "Cookies written to %s (size=%d)",
            YT_COOKIE_PATH,
            YT_COOKIE_PATH.stat().st_size if ok else 0,
        )
        return ok
    except Exception:
        logging.exception("Failed to write YouTube cookies.")
        return False


YOUTUBE_COOKIES_AVAILABLE: bool = write_youtube_cookies_file()

# =========================
# Helpers
# =========================
async def run_blocking_cmd(cmd: List[str]) -> Optional[str]:
    """Run a blocking shell command in a thread; return stdout or None."""
    logging.info("RUN: %s", " ".join(cmd))
    loop = asyncio.get_event_loop()
    try:
        proc: subprocess.CompletedProcess[str] = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                encoding="utf-8",
                errors="ignore",
            ),
        )
        logging.info("CMD OK: %s", proc.stdout[-1200:])
        return proc.stdout
    except subprocess.CalledProcessError as e:
        logging.warning("CMD FAIL (%d): %s", e.returncode, (e.stdout or "")[-1200:])
        return None
    except Exception:
        logging.exception("Command execution error")
        return None


def is_valid_file(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > MIN_VALID_FILE_SIZE_BYTES
    except Exception:
        return False


def build_yt_dlp_cmd(url: str, out_path: Path, as_audio: bool) -> List[str]:
    """Build yt-dlp command according to source."""
    is_tiktok = "tiktok.com" in url or "vt.tiktok.com" in url

    if is_tiktok:
        cmd = [
            "yt-dlp",
            "-f", "mp4*+m4a/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--no-warnings",
            "--concurrent-fragments", "4",
            "--retries", "10",
            "--fragment-retries", "10",
            "--socket-timeout", "30",
            "--user-agent", "Mozilla/5.0",
            "--add-header", "Referer:https://www.tiktok.com/",
            "-o", str(out_path),
            url,
        ]
    else:
        fmt = (
            f"b[filesize<{MAX_FILE_SIZE_MB}M]/bv*+ba/best"
            if not as_audio
            else "bestaudio[abr<=128k]/bestaudio"
        )
        cmd = [
            "yt-dlp",
            "-f", fmt,
            "--no-warnings",
            "--restrict-filenames",
            "--concurrent-fragments", "4",
            "--retries", "10",
            "--fragment-retries", "10",
            "--socket-timeout", "30",
            "--geo-bypass",
            "--encoding", "utf-8",
            "--user-agent", "Mozilla/5.0",
            "--extractor-args", "youtube:player_client=android",
            "-o", str(out_path),
            url,
        ]
        if YOUTUBE_COOKIES_AVAILABLE:
            cmd += ["--cookies", str(YT_COOKIE_PATH)]

    return cmd


async def reencode_to_mp4(in_path: Path, out_path: Path) -> bool:
    """Re-encode to H.264 MP4 for better Telegram compatibility."""
    try:
        await asyncio.to_thread(
            lambda: ffmpeg
            .input(str(in_path))
            .output(
                str(out_path),
                vcodec="libx264",
                pix_fmt="yuv420p",
                preset="veryfast",
                movflags="+faststart",
                acodec="aac",
            )
            .run(overwrite_output=True, quiet=True)
        )
        return is_valid_file(out_path)
    except ffmpeg.Error as e:
        logging.error("ffmpeg re-encode failed: %s", e.stderr.decode("utf-8", "ignore"))
        return False
    except Exception:
        logging.exception("ffmpeg re-encode error")
        return False


async def convert_to_mp3(in_path: Path, out_path: Path) -> bool:
    """Extract/convert audio to MP3."""
    try:
        await asyncio.to_thread(
            lambda: ffmpeg
            .input(str(in_path))
            .output(str(out_path), acodec="libmp3lame", audio_bitrate="128k")
            .run(overwrite_output=True, quiet=True)
        )
        return is_valid_file(out_path)
    except ffmpeg.Error as e:
        logging.error("ffmpeg mp3 failed: %s", e.stderr.decode("utf-8", "ignore"))
        return False
    except Exception:
        logging.exception("ffmpeg mp3 error")
        return False

# =========================
# Telegram handlers
# =========================
URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)


def is_authorized(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"أرسل رابط يوتيوب/تيك توك.\n"
        f"- اكتب mp3 مع الرابط لو عايز صوت فقط.\n"
        f"- الحد الأقصى: {MAX_FILE_SIZE_MB}MB."
    )


async def handle_media_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("غير مسموح.")
        return

    text = (update.effective_message.text or "").strip()
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("أرسل رابط صالح.")
        return

    url = m.group(1)
    is_tiktok = "tiktok.com" in url or "vt.tiktok.com" in url
    as_audio = "mp3" in text.lower()

    with TemporaryDirectory(prefix="tg_dl_", dir="/tmp") as tmpdir:
        tmp = Path(tmpdir)
        base = f"m_{hashlib.md5(url.encode()).hexdigest()[:10]}_{update.effective_message.id}"
        out_tmpl = tmp / (base + (".%(ext)s" if not is_tiktok else ".mp4"))

        await update.effective_chat.send_action(
            ChatAction.TYPING if as_audio else ChatAction.UPLOAD_VIDEO
        )
        status = await update.message.reply_text("⏳ جاري التنزيل...")

        try:
            cmd = build_yt_dlp_cmd(url, out_tmpl, as_audio)
            result = await run_blocking_cmd(cmd)
            if result is None:
                await status.edit_text(
                    "فشل التنزيل. قد يكون الرابط خاصًا أو غير متاح. جرّب رابطًا آخر."
                )
                return

            files = sorted(
                tmp.glob(base + "*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            final: Optional[Path] = next((p for p in files if is_valid_file(p)), None)
            if not final:
                await status.edit_text("فشل التنزيل. الملف الناتج غير صالح.")
                return

            processed = final
            if is_tiktok and FORCE_REENCODE_TT:
                fixed = tmp / f"{base}_fixed.mp4"
                if await reencode_to_mp4(final, fixed):
                    processed = fixed

            if (not is_tiktok) and as_audio and processed.suffix.lower() not in (".mp3", ".m4a"):
                mp3 = tmp / f"{base}.mp3"
                if await convert_to_mp3(processed, mp3):
                    processed = mp3

            try:
                size_mb = processed.stat().st_size / (1024 * 1024)
                caption = f"تم ✅ الحجم: {size_mb:.1f}MB"

                if processed.suffix.lower() in (".mp3", ".m4a", ".aac", ".ogg"):
                    await update.message.reply_audio(
                        audio=processed.open("rb"),
                        caption=caption,
                        filename=processed.name,
                        read_timeout=TIMEOUT_SECONDS,
                        write_timeout=TIMEOUT_SECONDS,
                    )
                else:
                    await update.message.reply_video(
                        video=processed.open("rb"),
                        caption=caption,
                        filename=processed.name,
                        read_timeout=TIMEOUT_SECONDS,
                        write_timeout=TIMEOUT_SECONDS,
                    )

                await status.delete()
            except Exception:
                logging.exception("Telegram send failed.")
                await status.edit_text("حدث خطأ أثناء إرسال الملف.")

        except Exception:
            logging.exception("Unexpected error in handler.")
            await status.edit_text("حصل خطأ غير متوقع.")


def main() -> None:
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN is missing!")
        raise SystemExit("BOT_TOKEN is missing!")

    # Verify yt-dlp availability
    try:
        ver = subprocess.check_output(["yt-dlp", "--version"], text=True).strip()
        logging.info("yt-dlp version: %s", ver)
    except Exception:
        logging.error("yt-dlp is not installed or not in PATH")
        sys.exit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_media_request))

    logging.info("Bot is running…")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()            lambda: subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8", errors="ignore")
        
        logging.info("Command succeeded. Output: %s", proc.stdout[-1200:])
        return proc.stdout
    except subprocess.CalledProcessError as e:
        logging.warning("Command failed with code %d. Output: %s", e.returncode, e.stdout[-1200:])
        return None
    except Exception as e:
        logging.exception("Run cmd error: %s", e)
        return None

def is_valid_file(p: Path) -> bool:
    """Checks if a Path object points to a valid file with a minimum size."""
    try:
        return p.is_file() and p.stat().st_size > MIN_VALID_FILE_SIZE_BYTES
    except Exception:
        return False

# ====== DOWNLOAD & CONVERSION COMMANDS ======
def build_yt_dlp_cmd(url: str, out_path: Path, as_audio: bool = False) -> List[str]:
    """Constructs the yt-dlp command for a given URL."""
    is_tiktok = "tiktok.com" in url
    if is_tiktok:
        cmd = [
            "yt-dlp", "-f", "mp4*+m4a/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--no-warnings", "--concurrent-fragments", "4",
            "--retries", "10", "--fragment-retries", "10",
            "--socket-timeout", "30", "--user-agent", "Mozilla/5.0",
            "--add-header", "Referer:https://www.tiktok.com/",
            "-o", str(out_path), url,
        ]
    else:
        # YouTube / Generic
        fmt = f"b[filesize<{MAX_FILE_SIZE_MB}M]/bv*+ba/best" if not as_audio else "bestaudio[abr<=128k]/bestaudio"
        cmd = [
            "yt-dlp", "-f", fmt,
            "--no-warnings", "--restrict-filenames",
            "--concurrent-fragments", "4",
            "--retries", "10", "--fragment-retries", "10",
            "--socket-timeout", "30", "--geo-bypass", "--encoding", "utf-8",
            "--user-agent", "Mozilla/5.0",
            "--extractor-args", "youtube:player_client=android",
            "-o", str(out_path), url,
        ]
        if YOUTUBE_COOKIES_AVAILABLE:
            cmd += ["--cookies", str(YT_COOKIE_PATH)]
    return cmd

async def reencode_to_mp4(in_path: Path, out_path: Path) -> bool:
    """Re-encodes a video file to MP4 using ffmpeg-python."""
    try:
        await asyncio.to_thread(
            lambda: ffmpeg
            .input(str(in_path))
            .output(str(out_path), vcodec="libx264", pix_fmt="yuv420p", preset="veryfast", movflags="+faststart", acodec="aac")
            .run(overwrite_output=True, quiet=True)
        )
        return is_valid_file(out_path)
    except ffmpeg.Error as e:
        logging.error("FFmpeg re-encode failed: %s", e.stderr.decode('utf8', errors='ignore'))
        return False
    except Exception as e:
        logging.exception("FFmpeg re-encode error: %s", e)
        return False

async def convert_to_mp3(in_path: Path, out_path: Path) -> bool:
    """Converts a video or audio file to MP3 using ffmpeg-python."""
    try:
        await asyncio.to_thread(
            lambda: ffmpeg
            .input(str(in_path))
            .output(str(out_path), acodec="libmp3lame", audio_bitrate="128k")
            .run(overwrite_output=True, quiet=True)
        )
        return is_valid_file(out_path)
    except ffmpeg.Error as e:
        logging.error("FFmpeg MP3 conversion failed: %s", e.stderr.decode('utf8', errors='ignore'))
        return False
    except Exception as e:
        logging.exception("FFmpeg MP3 conversion error: %s", e)
        return False

# ====== TELEGRAM HANDLERS ======
URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)

def is_authorized(update: Update) -> bool:
    """Checks if the user is authorized to use the bot."""
    if not ALLOWED_IDS:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_IDS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    await update.message.reply_text(
        f"أرسل رابط يوتيوب/تيك توك.\n- أكتب mp3 مع الرابط لو عايز صوت فقط.\n- الحد الأقصى: {MAX_FILE_SIZE_MB}MB."
    )

async def handle_media_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler for processing user-submitted links."""
    if not is_authorized(update):
        await update.message.reply_text("غير مسموح.")
        return

    text = (update.effective_message.text or "").strip()
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("أرسل رابط صالح.")
        return

    url = m.group(1)
    is_tiktok = "tiktok.com" in url or "vt.tiktok.com" in url
    as_audio = "mp3" in text.lower()

    # Use TemporaryDirectory for automatic cleanup
    with TemporaryDirectory(prefix="tg_dl_", dir="/tmp") as tmp:
        temp_dir = Path(tmp)
        # Use a hash to shorten and unique-ify the filename
        base_filename = f"media_{hashlib.md5(url.encode()).hexdigest()[:10]}_{update.effective_message.id}"
        
        # Use a clear output path template
        output_template = temp_dir / (base_filename + (".%(ext)s" if not is_tiktok else ".mp4"))
        
        await update.effective_chat.send_action(ChatAction.TYPING)
        status_message = await update.message.reply_text("⏳ جاري التنزيل...")

        try:
            cmd = build_yt_dlp_cmd(url, output_template, as_audio)
            
            # Using asyncio.to_thread for blocking subprocess
            result_output = await run_blocking_cmd(cmd)

            if result_output is None:
                await status_message.edit_text(
                    "فشل التنزيل. قد يكون الرابط خاص أو غير متوفر.\n"
                    "حاول مجددًا أو جرب رابطًا آخر."
                )
                return

            downloaded_files = sorted(temp_dir.glob(f"{base_filename}*"), 
                                      key=lambda p: p.stat().st_mtime, 
                                      reverse=True)
            final_file: Optional[Path] = next((f for f in downloaded_files if is_valid_file(f)), None)

            if not final_file:
                await status_message.edit_text("فشل التنزيل. الملف الناتج غير صالح.")
                return

            # Post-processing and conversion logic
            processed_file = final_file
            
            if is_tiktok and FORCE_REENCODE_TT:
                fixed_path = temp_dir / f"{base_filename}_fixed.mp4"
                if await reencode_to_mp4(final_file, fixed_path):
                    processed_file = fixed_path

            if not is_tiktok and as_audio and processed_file.suffix.lower() not in (".mp3", ".m4a"):
                mp3_path = temp_dir / f"{base_filename}.mp3"
                if await convert_to_mp3(processed_file, mp3_path):
                    processed_file = mp3_path
            
            # Final Telegram upload
            try:
                size_mb = processed_file.stat().st_size / (1024 * 1024)
                caption = f"تم ✅ الحجم: {size_mb:.1f}MB"
                
                if processed_file.suffix.lower() in (".mp3", ".m4a", ".aac", ".ogg"):
                    await update.message.reply_audio(
                        audio=processed_file.open("rb"),
                        caption=caption,
                        filename=processed_file.name,
                        read_timeout=TIMEOUT_SECONDS,
                        write_timeout=TIMEOUT_SECONDS
                    )
                else:
                    await update.message.reply_video(
                        video=processed_file.open("rb"),
                        caption=caption,
                        filename=processed_file.name,
                        read_timeout=TIMEOUT_SECONDS,
                        write_timeout=TIMEOUT_SECONDS
                    )
                    
                await status_message.delete()
                
            except Exception:
                logging.exception("Failed to send file to Telegram.")
                await status_message.edit_text("حدث خطأ أثناء إرسال الملف.")
                
        except Exception as e:
            logging.exception("An unexpected error occurred: %s", e)
            await status_message.edit_text("حصل خطأ غير متوقع.")
        # The temporary directory and its contents are automatically cleaned up here

def main():
    """Main function to start the bot."""
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable is missing!")
        raise SystemExit("BOT_TOKEN is missing!")

    try:
        # Check yt-dlp version and availability
        v = subprocess.check_output(["yt-dlp", "--version"], text=True).strip()
        logging.info("yt-dlp version: %s", v)
    except (subprocess.CalledProcessError, FileNotFoundError):
        logging.error("yt-dlp is not installed or not in PATH")
        sys.exit(1)

    try:
        # Check ffmpeg version and availability
        v = subprocess.check_output(["ffmpeg", "-version"], text=True).strip()
        logging.info("ffmpeg version: %s", v.splitlines()[0])
    except (subprocess.CalledProcessError, FileNotFoundError):
        logging.error("ffmpeg is not installed or not in PATH")
        sys.exit(1)
        
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_media_request))

    logging.info("Bot is running...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

            lambda: subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8", errors="ignore")
        )
        logging.info("Command succeeded. Output: %s", proc.stdout[-1200:])
        return proc.stdout
    except subprocess.CalledProcessError as e:
        logging.warning("Command failed with code %d. Output: %s", e.returncode, e.stdout[-1200:])
        return None
    except Exception as e:
        logging.exception("Run cmd error: %s", e)
        return None

def is_valid_file(p: Path) -> bool:
    """Checks if a Path object points to a valid file with a minimum size."""
    try:
        return p.is_file() and p.stat().st_size > MIN_VALID_FILE_SIZE_BYTES
    except Exception:
        return False

# ====== DOWNLOAD & CONVERSION COMMANDS ======
def build_yt_dlp_cmd(url: str, out_path: Path, as_audio: bool = False) -> List[str]:
    """Constructs the yt-dlp command for a given URL."""
    is_tiktok = "tiktok.com" in url
    if is_tiktok:
        cmd = [
            "yt-dlp", "-f", "mp4*+m4a/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--no-warnings", "--concurrent-fragments", "4",
            "--retries", "10", "--fragment-retries", "10",
            "--socket-timeout", "30", "--user-agent", "Mozilla/5.0",
            "--add-header", "Referer:https://www.tiktok.com/",
            "-o", str(out_path), url,
        ]
    else:
        # YouTube / Generic
        fmt = f"b[filesize<{MAX_FILE_SIZE_MB}M]/bv*+ba/best" if not as_audio else "bestaudio[abr<=128k]/bestaudio"
        cmd = [
            "yt-dlp", "-f", fmt,
            "--no-warnings", "--restrict-filenames",
            "--concurrent-fragments", "4",
            "--retries", "10", "--fragment-retries", "10",
            "--socket-timeout", "30", "--geo-bypass", "--encoding", "utf-8",
            "--user-agent", "Mozilla/5.0",
            "--extractor-args", "youtube:player_client=android",
            "-o", str(out_path), url,
        ]
        if YOUTUBE_COOKIES_AVAILABLE:
            cmd += ["--cookies", str(YT_COOKIE_PATH)]
    return cmd

async def reencode_to_mp4(in_path: Path, out_path: Path) -> bool:
    """Re-encodes a video file to MP4 using ffmpeg-python."""
    try:
        await asyncio.to_thread(
            lambda: ffmpeg
            .input(str(in_path))
            .output(str(out_path), vcodec="libx264", pix_fmt="yuv420p", preset="veryfast", movflags="+faststart", acodec="aac")
            .run(overwrite_output=True, quiet=True)
        )
        return is_valid_file(out_path)
    except ffmpeg.Error as e:
        logging.error("FFmpeg re-encode failed: %s", e.stderr.decode('utf8', errors='ignore'))
        return False
    except Exception as e:
        logging.exception("FFmpeg re-encode error: %s", e)
        return False

async def convert_to_mp3(in_path: Path, out_path: Path) -> bool:
    """Converts a video or audio file to MP3 using ffmpeg-python."""
    try:
        await asyncio.to_thread(
            lambda: ffmpeg
            .input(str(in_path))
            .output(str(out_path), acodec="libmp3lame", audio_bitrate="128k")
            .run(overwrite_output=True, quiet=True)
        )
        return is_valid_file(out_path)
    except ffmpeg.Error as e:
        logging.error("FFmpeg MP3 conversion failed: %s", e.stderr.decode('utf8', errors='ignore'))
        return False
    except Exception as e:
        logging.exception("FFmpeg MP3 conversion error: %s", e)
        return False

# ====== TELEGRAM HANDLERS ======
URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)

def is_authorized(update: Update) -> bool:
    """Checks if the user is authorized to use the bot."""
    if not ALLOWED_IDS:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_IDS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    await update.message.reply_text(
        f"أرسل رابط يوتيوب/تيك توك.\n- أكتب mp3 مع الرابط لو عايز صوت فقط.\n- الحد الأقصى: {MAX_FILE_SIZE_MB}MB."
    )

async def handle_media_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler for processing user-submitted links."""
    if not is_authorized(update):
        await update.message.reply_text("غير مسموح.")
        return

    text = (update.effective_message.text or "").strip()
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("أرسل رابط صالح.")
        return

    url = m.group(1)
    is_tiktok = "tiktok.com" in url or "vt.tiktok.com" in url
    as_audio = "mp3" in text.lower()

    # Use TemporaryDirectory for automatic cleanup
    with TemporaryDirectory(prefix="tg_dl_", dir="/tmp") as tmp:
        temp_dir = Path(tmp)
        # Use a hash to shorten and unique-ify the filename
        base_filename = f"media_{hashlib.md5(url.encode()).hexdigest()[:10]}_{update.effective_message.id}"
        
        # Use a clear output path template
        output_template = temp_dir / (base_filename + (".%(ext)s" if not is_tiktok else ".mp4"))
        
        await update.effective_chat.send_action(ChatAction.TYPING)
        status_message = await update.message.reply_text("⏳ جاري التنزيل...")

        try:
            cmd = build_yt_dlp_cmd(url, output_template, as_audio)
            
            # Using asyncio.to_thread for blocking subprocess
            result_output = await run_blocking_cmd(cmd)

            if result_output is None:
                await status_message.edit_text(
                    "فشل التنزيل. قد يكون الرابط خاص أو غير متوفر.\n"
                    "حاول مجددًا أو جرب رابطًا آخر."
                )
                return

            downloaded_files = sorted(temp_dir.glob(f"{base_filename}*"), 
                                      key=lambda p: p.stat().st_mtime, 
                                      reverse=True)
            final_file: Optional[Path] = next((f for f in downloaded_files if is_valid_file(f)), None)

            if not final_file:
                await status_message.edit_text("فشل التنزيل. الملف الناتج غير صالح.")
                return

            # Post-processing and conversion logic
            processed_file = final_file
            
            if is_tiktok and FORCE_REENCODE_TT:
                fixed_path = temp_dir / f"{base_filename}_fixed.mp4"
                if await reencode_to_mp4(final_file, fixed_path):
                    processed_file = fixed_path

            if not is_tiktok and as_audio and processed_file.suffix.lower() not in (".mp3", ".m4a"):
                mp3_path = temp_dir / f"{base_filename}.mp3"
                if await convert_to_mp3(processed_file, mp3_path):
                    processed_file = mp3_path
            
            # Final Telegram upload
            try:
                size_mb = processed_file.stat().st_size / (1024 * 1024)
                caption = f"تم ✅ الحجم: {size_mb:.1f}MB"
                
                if processed_file.suffix.lower() in (".mp3", ".m4a", ".aac", ".ogg"):
                    await update.message.reply_audio(
                        audio=processed_file.open("rb"),
                        caption=caption,
                        filename=processed_file.name,
                        read_timeout=TIMEOUT_SECONDS,
                        write_timeout=TIMEOUT_SECONDS
                    )
                else:
                    await update.message.reply_video(
                        video=processed_file.open("rb"),
                        caption=caption,
                        filename=processed_file.name,
                        read_timeout=TIMEOUT_SECONDS,
                        write_timeout=TIMEOUT_SECONDS
                    )
                    
                await status_message.delete()
                
            except Exception:
                logging.exception("Failed to send file to Telegram.")
                await status_message.edit_text("حدث خطأ أثناء إرسال الملف.")
                
        except Exception as e:
            logging.exception("An unexpected error occurred: %s", e)
            await status_message.edit_text("حصل خطأ غير متوقع.")
        # The temporary directory and its contents are automatically cleaned up here

def main():
    """Main function to start the bot."""
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable is missing!")
        raise SystemExit("BOT_TOKEN is missing!")

    try:
        # Check yt-dlp version and availability
        v = subprocess.check_output(["yt-dlp", "--version"], text=True).strip()
        logging.info("yt-dlp version: %s", v)
    except (subprocess.CalledProcessError, FileNotFoundError):
        logging.error("yt-dlp is not installed or not in PATH")
        sys.exit(1)

    try:
        # Check ffmpeg version and availability
        v = subprocess.check_output(["ffmpeg", "-version"], text=True).strip()
        logging.info("ffmpeg version: %s", v.splitlines()[0])
    except (subprocess.CalledProcessError, FileNotFoundError):
        logging.error("ffmpeg is not installed or not in PATH")
        sys.exit(1)
        
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_media_request))

    logging.info("Bot is running...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
            lambda: subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8", errors="ignore")
        )
        logging.info("Command succeeded. Output: %s", proc.stdout[-1200:])
        return proc.stdout
    except subprocess.CalledProcessError as e:
        logging.warning("Command failed with code %d. Output: %s", e.returncode, e.stdout[-1200:])
        return None
    except Exception as e:
        logging.exception("Run cmd error: %s", e)
        return None

def is_valid_file(p: Path) -> bool:
    """Checks if a Path object points to a valid file with a minimum size."""
    try:
        return p.is_file() and p.stat().st_size > MIN_VALID_FILE_SIZE_BYTES
    except Exception:
        return False

# ====== DOWNLOAD & CONVERSION COMMANDS ======
def build_yt_dlp_cmd(url: str, out_path: Path, as_audio: bool = False) -> List[str]:
    """Constructs the yt-dlp command for a given URL."""
    is_tiktok = "tiktok.com" in url
    if is_tiktok:
        cmd = [
            "yt-dlp", "-f", "mp4*+m4a/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--no-warnings", "--concurrent-fragments", "4",
            "--retries", "10", "--fragment-retries", "10",
            "--socket-timeout", "30", "--user-agent", "Mozilla/5.0",
            "--add-header", "Referer:https://www.tiktok.com/",
            "-o", str(out_path), url,
        ]
    else:
        # YouTube / Generic
        fmt = f"b[filesize<{MAX_FILE_SIZE_MB}M]/bv*+ba/best" if not as_audio else "bestaudio[abr<=128k]/bestaudio"
        cmd = [
            "yt-dlp", "-f", fmt,
            "--no-warnings", "--restrict-filenames",
            "--concurrent-fragments", "4",
            "--retries", "10", "--fragment-retries", "10",
            "--socket-timeout", "30", "--geo-bypass", "--encoding", "utf-8",
            "--user-agent", "Mozilla/5.0",
            "--extractor-args", "youtube:player_client=android",
            "-o", str(out_path), url,
        ]
        if YOUTUBE_COOKIES_AVAILABLE:
            cmd += ["--cookies", str(YT_COOKIE_PATH)]
    return cmd

async def reencode_to_mp4(in_path: Path, out_path: Path) -> bool:
    """Re-encodes a video file to MP4 using ffmpeg-python."""
    try:
        await asyncio.to_thread(
            lambda: ffmpeg
            .input(str(in_path))
            .output(str(out_path), vcodec="libx264", pix_fmt="yuv420p", preset="veryfast", movflags="+faststart", acodec="aac")
            .run(overwrite_output=True, quiet=True)
        )
        return is_valid_file(out_path)
    except ffmpeg.Error as e:
        logging.error("FFmpeg re-encode failed: %s", e.stderr.decode('utf8', errors='ignore'))
        return False
    except Exception as e:
        logging.exception("FFmpeg re-encode error: %s", e)
        return False

async def convert_to_mp3(in_path: Path, out_path: Path) -> bool:
    """Converts a video or audio file to MP3 using ffmpeg-python."""
    try:
        await asyncio.to_thread(
            lambda: ffmpeg
            .input(str(in_path))
            .output(str(out_path), acodec="libmp3lame", audio_bitrate="128k")
            .run(overwrite_output=True, quiet=True)
        )
        return is_valid_file(out_path)
    except ffmpeg.Error as e:
        logging.error("FFmpeg MP3 conversion failed: %s", e.stderr.decode('utf8', errors='ignore'))
        return False
    except Exception as e:
        logging.exception("FFmpeg MP3 conversion error: %s", e)
        return False

# ====== TELEGRAM HANDLERS ======
URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)

def is_authorized(update: Update) -> bool:
    """Checks if the user is authorized to use the bot."""
    if not ALLOWED_IDS:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_IDS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    await update.message.reply_text(
        f"أرسل رابط يوتيوب/تيك توك.\n- أكتب mp3 مع الرابط لو عايز صوت فقط.\n- الحد الأقصى: {MAX_FILE_SIZE_MB}MB."
    )

async def handle_media_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler for processing user-submitted links."""
    if not is_authorized(update):
        await update.message.reply_text("غير مسموح.")
        return

    text = (update.effective_message.text or "").strip()
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("أرسل رابط صالح.")
        return

    url = m.group(1)
    is_tiktok = "tiktok.com" in url or "vt.tiktok.com" in url
    as_audio = "mp3" in text.lower()

    # Use TemporaryDirectory for automatic cleanup
    with TemporaryDirectory(prefix="tg_dl_", dir="/tmp") as tmp:
        temp_dir = Path(tmp)
        # Use a hash to shorten and unique-ify the filename
        base_filename = f"media_{hashlib.md5(url.encode()).hexdigest()[:10]}_{update.effective_message.id}"
        
        # Use a clear output path template
        output_template = temp_dir / (base_filename + (".%(ext)s" if not is_tiktok else ".mp4"))
        
        await update.effective_chat.send_action(ChatAction.TYPING)
        status_message = await update.message.reply_text("⏳ جاري التنزيل...")

        try:
            cmd = build_yt_dlp_cmd(url, output_template, as_audio)
            
            # Using asyncio.to_thread for blocking subprocess
            result_output = await run_blocking_cmd(cmd)

            if result_output is None:
                await status_message.edit_text(
                    "فشل التنزيل. قد يكون الرابط خاص أو غير متوفر.\n"
                    "حاول مجددًا أو جرب رابطًا آخر."
                )
                return

            downloaded_files = sorted(temp_dir.glob(f"{base_filename}*"), 
                                      key=lambda p: p.stat().st_mtime, 
                                      reverse=True)
            final_file: Optional[Path] = next((f for f in downloaded_files if is_valid_file(f)), None)

            if not final_file:
                await status_message.edit_text("فشل التنزيل. الملف الناتج غير صالح.")
                return

            # Post-processing and conversion logic
            processed_file = final_file
            
            if is_tiktok and FORCE_REENCODE_TT:
                fixed_path = temp_dir / f"{base_filename}_fixed.mp4"
                if await reencode_to_mp4(final_file, fixed_path):
                    processed_file = fixed_path

            if not is_tiktok and as_audio and processed_file.suffix.lower() not in (".mp3", ".m4a"):
                mp3_path = temp_dir / f"{base_filename}.mp3"
                if await convert_to_mp3(processed_file, mp3_path):
                    processed_file = mp3_path
            
            # Final Telegram upload
            try:
                size_mb = processed_file.stat().st_size / (1024 * 1024)
                caption = f"تم ✅ الحجم: {size_mb:.1f}MB"
                
                if processed_file.suffix.lower() in (".mp3", ".m4a", ".aac", ".ogg"):
                    await update.message.reply_audio(
                        audio=processed_file.open("rb"),
                        caption=caption,
                        filename=processed_file.name,
                        read_timeout=TIMEOUT_SECONDS,
                        write_timeout=TIMEOUT_SECONDS
                    )
                else:
                    await update.message.reply_video(
                        video=processed_file.open("rb"),
                        caption=caption,
                        filename=processed_file.name,
                        read_timeout=TIMEOUT_SECONDS,
                        write_timeout=TIMEOUT_SECONDS
                    )
                    
                await status_message.delete()
                
            except Exception:
                logging.exception("Failed to send file to Telegram.")
                await status_message.edit_text("حدث خطأ أثناء إرسال الملف.")
                
        except Exception as e:
            logging.exception("An unexpected error occurred: %s", e)
            await status_message.edit_text("حصل خطأ غير متوقع.")
        # The temporary directory and its contents are automatically cleaned up here

def main():
    """Main function to start the bot."""
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable is missing!")
        raise SystemExit("BOT_TOKEN is missing!")

    try:
        # Check yt-dlp version and availability
        v = subprocess.check_output(["yt-dlp", "--version"], text=True).strip()
        logging.info("yt-dlp version: %s", v)
    except (subprocess.CalledProcessError, FileNotFoundError):
        logging.error("yt-dlp is not installed or not in PATH")
        sys.exit(1)
        
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_media_request))

    logging.info("Bot is running...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

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
