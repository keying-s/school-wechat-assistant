"""Conversation-aware school Q&A orchestration."""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from .database import Store
from .deepseek_client import DeepSeekClient
from .retrieval import RetrievalIndex


logger = logging.getLogger("school_assistant.qa")


class SchoolQAService:
    def __init__(self, store: Store, ai: DeepSeekClient, retrieval: RetrievalIndex | None = None):
        self.store = store
        self.ai = ai
        self.retrieval = retrieval or RetrievalIndex(store)
        self._locks_guard = threading.Lock()
        self._user_locks: dict[str, threading.Lock] = {}
        self._refresh_guard = threading.Lock()
        self._refresh_requested = threading.Event()
        self._refresh_thread: threading.Thread | None = None

    def _user_lock(self, user_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._user_locks.setdefault(user_id, threading.Lock())

    def request_index_refresh(self) -> threading.Thread:
        """Coalesce change events and refresh vectors outside the sync pipeline."""
        self._refresh_requested.set()

        def run() -> None:
            while True:
                self._refresh_requested.clear()
                try:
                    self.retrieval.sync(force=True)
                    logger.info("本地学校资料索引已更新")
                except Exception:
                    logger.exception("本地资料索引更新失败；提问时会重试")
                with self._refresh_guard:
                    if not self._refresh_requested.is_set():
                        self._refresh_thread = None
                        return

        with self._refresh_guard:
            if self._refresh_thread and self._refresh_thread.is_alive():
                return self._refresh_thread
            self._refresh_thread = threading.Thread(
                target=run, name="school-rag-refresh", daemon=True
            )
            self._refresh_thread.start()
            return self._refresh_thread

    def warmup_async(self) -> threading.Thread:
        """Build and load the local index without delaying service startup."""
        return self.request_index_refresh()

    @staticmethod
    def _fallback_terms(question: str) -> list[str]:
        terms = []
        compact = re.sub(r"[，。！？、：；,.!?;:\s]+", " ", question).strip()
        for token in compact.split():
            if token and token not in terms:
                terms.append(token[:80])
        return terms[:8]

    def ask(self, user_id: str, question: str) -> dict[str, Any]:
        safe_user_id = str(user_id or "anonymous")[:180]
        question = str(question or "").strip()
        if not question:
            raise ValueError("question 不能为空")

        if question.casefold() in {"清除上下文", "重置上下文", "清空对话", "/reset", "reset"}:
            self.store.clear_qa_history(safe_user_id)
            return {
                "answer": "已清除本次学校事务问答的对话上下文；本地群消息、附件和待办资料没有删除。",
                "standalone_question": question,
                "sources": [],
                "cleared": True,
            }

        with self._user_lock(safe_user_id):
            history = self.store.qa_history(safe_user_id)
            try:
                plan = self.ai.plan_query(question, history)
            except Exception as exc:
                logger.warning("查询改写失败，直接检索原问题：%s", exc)
                plan = {
                    "standalone_question": question,
                    "search_terms": self._fallback_terms(question),
                    "time_scope": None,
                }

            standalone = str(plan.get("standalone_question") or question).strip()
            search_terms = [str(term) for term in plan.get("search_terms", []) if str(term).strip()]
            if plan.get("time_scope"):
                search_terms.append(str(plan["time_scope"]))
            retrieval = self.retrieval.search(
                standalone,
                search_terms=search_terms,
                time_scope=plan.get("time_scope"),
            )
            answer = self.ai.answer_with_sources(
                question,
                standalone,
                history,
                retrieval,
            )
            self.store.append_qa_exchange(safe_user_id, question, answer)
            return {
                "answer": answer,
                "standalone_question": standalone,
                "search_terms": search_terms,
                "sources": [
                    {
                        "ref": item.get("ref"),
                        "kind": item.get("kind"),
                        "group": item.get("group"),
                        "time": item.get("time"),
                        "title": item.get("title"),
                    }
                    for item in retrieval.get("sources", [])
                ],
                "retrieval": retrieval.get("index", {}),
            }
