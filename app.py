"""Run the local school schedule website and background Weixin reader."""

from __future__ import annotations

import logging
import signal
import threading
from logging.handlers import RotatingFileHandler

from school_assistant import config
from school_assistant.database import Store
from school_assistant.deepseek_client import DeepSeekClient
from school_assistant.pipeline import Pipeline
from school_assistant.qa_service import SchoolQAService
from school_assistant.retrieval import RetrievalIndex
from school_assistant.server import AppContext, create_server
from school_assistant.wecom_bot import SchoolWeComBot


def configure_logging() -> None:
    handler = RotatingFileHandler(
        config.LOG_DIR / "school_assistant.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main() -> None:
    configure_logging()
    store = Store()
    ai = DeepSeekClient()
    pipeline = Pipeline(store, ai=ai)
    retrieval = RetrievalIndex(store)
    qa = SchoolQAService(store, ai, retrieval)
    pipeline.on_data_changed = qa.request_index_refresh
    bot = SchoolWeComBot(qa)
    context = AppContext(store, pipeline, ai, qa, bot)
    server = create_server(context)

    def shutdown(*_args):
        pipeline.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    pipeline.start()
    qa.warmup_async()
    bot.start()
    logging.getLogger("school_assistant").info(
        "学校事务服务启动：http://%s:%s", config.HOST, config.PORT
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        pipeline.stop()
        server.server_close()


if __name__ == "__main__":
    main()
