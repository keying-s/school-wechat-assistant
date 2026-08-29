"""Configuration for the school assistant.

Secrets live in ``config/.env.school``.  The loader never logs values and
does not overwrite variables already provided by the process environment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / "config" / ".env.school"


def _load_env() -> None:
    if not ENV_PATH.is_file():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env()


def get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = Path(get("SCHOOL_DB_PATH", str(DATA_DIR / "school_assistant.sqlite3"))).resolve()

HOST = get("SCHOOL_HOST", "127.0.0.1")
PORT = int(get("SCHOOL_PORT", "8765"))
POLL_SECONDS = max(2, int(get("WECHAT_POLL_SECONDS", "5")))
GROUP_REFRESH_SECONDS = max(30, int(get("GROUP_REFRESH_SECONDS", "60")))
DEFAULT_LOOKBACK_DAYS = max(1, int(get("DEFAULT_LOOKBACK_DAYS", "7")))
MAX_INITIAL_MESSAGES = max(100, int(get("MAX_INITIAL_MESSAGES", "800")))

DEEPSEEK_API_KEY = get("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_BASE_URL = get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_TIMEOUT = max(15, int(get("DEEPSEEK_TIMEOUT", "60")))

WECHAT_DATA_ROOT = get("WECHAT_DATA_ROOT")
WCDB_TOOL_PATH = Path(get(
    "WCDB_TOOL_PATH",
    str(BASE_DIR / "vendor" / "wcdb-key-tool" / "wcdb_key_tool_windows.py"),
)).resolve()

TIMEZONE_NAME = "Asia/Shanghai"

for directory in (DATA_DIR, LOG_DIR):
    directory.mkdir(parents=True, exist_ok=True)
