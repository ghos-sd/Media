import asyncio, subprocess, logging
from pathlib import Path
from .config import CONFIG

MIN_VALID_FILE_SIZE_BYTES = CONFIG["MIN_VALID_FILE_SIZE_BYTES"]

async def run_blocking_cmd(cmd):
    """تشغيل أوامر blocking في thread منفصل"""
    logging.info("Running command: %s", " ".join(cmd))
    loop = asyncio.get_event_loop()
    try:
        proc = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8", errors="ignore")
        )
        logging.info("Command succeeded. Output: %s", proc.stdout[-800:])
        return proc.stdout
    except subprocess.CalledProcessError as e:
        logging.warning("Command failed with code %d. Output: %s", e.returncode, e.stdout[-800:])
        return None
    except Exception as e:
        logging.exception("Run cmd error: %s", e)
        return None

def is_valid_file(p: Path) -> bool:
    """يتأكد من صلاحية الملف"""
    try:
        return p.is_file() and p.stat().st_size > MIN_VALID_FILE_SIZE_BYTES
    except Exception:
        return False
