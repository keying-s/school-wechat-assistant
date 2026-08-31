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
BOT_ENV_PATH = BASE_DIR / "config" / ".env.school.bot"


def _load_env() -> None:
    for env_path in (ENV_PATH, BOT_ENV_PATH):
        if not env_path.is_file():
            continue
        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
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

# Dedicated Enterprise WeChat intelligent robot.  These credentials belong to
# the school assistant only and are intentionally separate from the tracker.
WECOM_SCHOOL_BOT_ID = get("WECOM_SCHOOL_BOT_ID")
WECOM_SCHOOL_BOT_SECRET = get("WECOM_SCHOOL_BOT_SECRET")
WECOM_REPLY_MAX_BYTES = max(800, min(2000, int(get("WECOM_REPLY_MAX_BYTES", "1900"))))

# Local retrieval.  FastEmbed runs this ONNX model entirely on this computer;
# neither documents nor vectors are sent to an embedding API.
EMBEDDING_MODEL = get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
EMBEDDING_CACHE_DIR = Path(
    get("EMBEDDING_CACHE_DIR") or str(DATA_DIR / "models")
).resolve()
EMBEDDING_THREADS = max(1, min(16, int(get("EMBEDDING_THREADS", "8"))))
RAG_CHUNK_CHARS = max(300, min(1500, int(get("RAG_CHUNK_CHARS", "800"))))
RAG_CHUNK_OVERLAP = max(50, min(300, int(get("RAG_CHUNK_OVERLAP", "120"))))
RAG_TOP_K = max(5, min(30, int(get("RAG_TOP_K", "10"))))
RAG_SYNC_SECONDS = max(5, int(get("RAG_SYNC_SECONDS", "15")))
QA_HISTORY_TURNS = max(2, min(20, int(get("QA_HISTORY_TURNS", "20"))))
QA_SESSION_TTL_HOURS = max(1, min(720, int(get("QA_SESSION_TTL_HOURS", "720"))))

WECHAT_DATA_ROOT = get("WECHAT_DATA_ROOT")
WCDB_TOOL_PATH = Path(get(
    "WCDB_TOOL_PATH",
    str(BASE_DIR / "vendor" / "wcdb-key-tool" / "wcdb_key_tool_windows.py"),
)).resolve()

TIMEZONE_NAME = "Asia/Shanghai"

for directory in (DATA_DIR, LOG_DIR, EMBEDDING_CACHE_DIR):
    directory.mkdir(parents=True, exist_ok=True)
