import os, json
from pathlib import Path
from typing import Dict

CONFIG_FILE = Path("config.json")
DEFAULT_CONFIG = {
    "MAX_FILE_SIZE_MB": 70,
    "MIN_VALID_FILE_SIZE_BYTES": 200 * 1024,
    "FORCE_REENCODE_TT": True,
    "YT_COOKIE_PATH": "/app/cookies_youtube.txt",
    "YT_COOKIES_B64": "",
    "TIMEOUT_SECONDS": 120
}

def load_config() -> Dict:
    """تحميل الإعدادات من config.json مع دعم الـ Env Vars"""
    config = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.is_file():
        with open(CONFIG_FILE, "r") as f:
            config.update(json.load(f))

    # Override with Env Vars
    for key in config:
        env_val = os.getenv(key)
        if env_val is not None:
            if isinstance(config[key], int):
                config[key] = int(env_val)
            elif isinstance(config[key], bool):
                config[key] = env_val.lower() in ('true', '1')
            else:
                config[key] = env_val
    return config

CONFIG = load_config()
