import base64, logging
from pathlib import Path
from .config import CONFIG

YT_COOKIE_PATH = Path(CONFIG["YT_COOKIE_PATH"])
YT_COOKIES_B64 = CONFIG["YT_COOKIES_B64"]

def write_youtube_cookies_file() -> bool:
    """فك الترميز وكتابة كوكيز YouTube إلى ملف"""
    if not YT_COOKIES_B64:
        logging.warning("YT_COOKIES_B64 is empty. YouTube may require login.")
        return False
    try:
        data = base64.b64decode(YT_COOKIES_B64)
        YT_COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
        YT_COOKIE_PATH.write_bytes(data)
        ok = YT_COOKIE_PATH.is_file() and YT_COOKIE_PATH.stat().st_size > 200
        logging.info("Cookies file written to %s (size=%d)", YT_COOKIE_PATH, YT_COOKIE_PATH.stat().st_size if ok else 0)
        return ok
    except Exception as e:
        logging.exception("Failed to write cookies file: %s", e)
        return False

YOUTUBE_COOKIES_AVAILABLE: bool = write_youtube_cookies_file()
