"""Dedicated Enterprise WeChat long-connection bot for school Q&A only."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import traceback
from typing import Any

from . import config
from .database import now_iso
from .qa_service import SchoolQAService


logger = logging.getLogger("school_assistant.wecom")


def _clip_utf8(text: str, max_bytes: int = config.WECOM_REPLY_MAX_BYTES) -> str:
    payload = str(text).encode("utf-8")
    if len(payload) <= max_bytes:
        return str(text)
    suffix = "\n…（回答较长，已截断）"
    budget = max(1, max_bytes - len(suffix.encode("utf-8")))
    return payload[:budget].decode("utf-8", "ignore") + suffix


class SchoolWeComBot:
    """One BotID, one connection, and no cross-domain routing."""

    def __init__(self, qa: SchoolQAService):
        self.qa = qa
        self._thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "configured": bool(config.WECOM_SCHOOL_BOT_ID and config.WECOM_SCHOOL_BOT_SECRET),
            "connected": False,
            "started_at": None,
            "last_message_at": None,
            "last_reply_at": None,
            "last_error": None,
        }

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def _set_status(self, **updates: Any) -> None:
        with self._status_lock:
            self._status.update(updates)

    def start(self) -> bool:
        if not self._status["configured"]:
            logger.info("未配置学校机器人 BotID/Secret，企业微信入口未启动")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self.run, name="school-wecom-bot", daemon=True)
        self._thread.start()
        return True

    async def _connect_once(self) -> None:
        from wecom_aibot_sdk import WSClient, WSClientOptions, generate_req_id

        client = WSClient(WSClientOptions(
            bot_id=config.WECOM_SCHOOL_BOT_ID,
            secret=config.WECOM_SCHOOL_BOT_SECRET,
        ))

        async def on_text(frame) -> None:
            try:
                body = frame.body or {}
                content = str(body.get("text", {}).get("content", "")).strip()
                sender = str(body.get("from", {}).get("userid") or "anonymous")
                if not content:
                    return
                self._set_status(last_message_at=now_iso())
                logger.info("收到学校事务提问：sender=%s chars=%s", sender, len(content))
                stream_id = generate_req_id("school")
                try:
                    await client.reply_stream(
                        frame,
                        stream_id,
                        "正在检索本地群聊、附件和待办，请稍候…",
                        finish=False,
                    )
                except Exception as exc:
                    logger.warning("学校机器人占位回复失败：%s", exc)
                try:
                    result = await asyncio.to_thread(self.qa.ask, sender, content)
                    reply = result["answer"]
                except Exception:
                    logger.error("学校事务问答失败：\n%s", traceback.format_exc())
                    reply = "抱歉，学校资料检索暂时失败。请稍后再试，或打开本机日程网站检查服务状态。"
                await client.reply_stream(frame, stream_id, _clip_utf8(reply), finish=True)
                self._set_status(last_reply_at=now_iso(), last_error=None)
            except Exception as exc:
                self._set_status(last_error=f"{type(exc).__name__}: {exc}"[:300])
                logger.error("学校机器人回复发送失败：\n%s", traceback.format_exc())

        client.on("message.text", on_text)
        await client.connect_async()
        self._set_status(connected=True, started_at=self._status.get("started_at") or now_iso(), last_error=None)
        logger.info("学校事务智能机器人长连接已建立")
        while getattr(client, "is_connected", True):
            await asyncio.sleep(1)
        self._set_status(connected=False)

    def run(self) -> None:
        self._set_status(started_at=now_iso())
        while True:
            try:
                asyncio.run(self._connect_once())
            except ImportError as exc:
                self._set_status(last_error=f"缺少企业微信 SDK：{exc}")
                logger.error("缺少 wecom-aibot-sdk-python，学校机器人未启动")
                return
            except Exception as exc:
                self._set_status(
                    connected=False,
                    last_error=f"{type(exc).__name__}: {exc}"[:300],
                )
                logger.error("学校机器人长连接异常，5 秒后重连：\n%s", traceback.format_exc())
            time.sleep(5)
