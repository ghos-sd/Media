import ffmpeg, asyncio, logging
from pathlib import Path
from .config import CONFIG
from .cookies import YOUTUBE_COOKIES_AVAILABLE, YT_COOKIE_PATH
from .utils import is_valid_file

MAX_FILE_SIZE_MB = CONFIG["MAX_FILE_SIZE_MB"]
FORCE_REENCODE_TT = CONFIG["FORCE_REENCODE_TT"]

def build_yt_dlp_cmd(url: str, out_path: Path, as_audio: bool = False):
    """بناء أمر yt-dlp"""
    is_tiktok = "tiktok.com" in url
    if is_tiktok:
        return [
            "yt-dlp", "-f", "mp4*+m4a/best[ext=mp4]/best",
            "--merge-output-format", "mp4", "--no-warnings",
            "--concurrent-fragments", "4", "--retries", "10",
            "--fragment-retries", "10", "--socket-timeout", "30",
            "--user-agent", "Mozilla/5.0", "--add-header", "Referer:https://www.tiktok.com/",
            "-o", str(out_path), url,
        ]
    else:
        fmt = f"b[filesize<{MAX_FILE_SIZE_MB}M]/bv*+ba/best" if not as_audio else "bestaudio[abr<=128k]/bestaudio"
        cmd = [
            "yt-dlp", "-f", fmt, "--no-warnings", "--restrict-filenames",
            "--concurrent-fragments", "4", "--retries", "10", "--fragment-retries", "10",
            "--socket-timeout", "30", "--geo-bypass", "--encoding", "utf-8",
            "--user-agent", "Mozilla/5.0", "--extractor-args", "youtube:player_client=android",
            "-o", str(out_path), url,
        ]
        if YOUTUBE_COOKIES_AVAILABLE:
            cmd += ["--cookies", str(YT_COOKIE_PATH)]
        return cmd

async def reencode_to_mp4(in_path: Path, out_path: Path):
    """إعادة ترميز MP4"""
    try:
        await asyncio.to_thread(
            lambda: ffmpeg.input(str(in_path)).output(
                str(out_path), vcodec="libx264", pix_fmt="yuv420p", preset="veryfast",
                movflags="+faststart", acodec="aac"
            ).run(overwrite_output=True, quiet=True)
        )
        return is_valid_file(out_path)
    except Exception as e:
        logging.exception("FFmpeg error: %s", e)
        return False

async def convert_to_mp3(in_path: Path, out_path: Path):
    """تحويل إلى MP3"""
    try:
        await asyncio.to_thread(
            lambda: ffmpeg.input(str(in_path)).output(
                str(out_path), acodec="libmp3lame", audio_bitrate="128k"
            ).run(overwrite_output=True, quiet=True)
        )
        return is_valid_file(out_path)
    except Exception as e:
        logging.exception("FFmpeg error: %s", e)
        return False
