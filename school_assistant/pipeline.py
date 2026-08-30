"""Background synchronization and AI extraction pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from . import config
from .database import Store, now_iso
from .deepseek_client import DeepSeekClient
from .file_text import extract_local_file
from .wechat_reader import WeChatReader


logger = logging.getLogger("school_assistant.pipeline")


class Pipeline:
    def __init__(self, store: Store, reader: WeChatReader | None = None, ai: DeepSeekClient | None = None):
        self.store = store
        self.reader = reader or WeChatReader()
        self.ai = ai or DeepSeekClient()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_signature: tuple[int, ...] | None = None
        self._last_group_refresh = 0.0
        self._ai_retry_after = 0.0
        self._dedup_retry_after = 0.0
        self._last_file_reconcile = 0.0
        self.on_data_changed: Callable[[], None] | None = None
        self._state_lock = threading.Lock()
        self._state: dict[str, Any] = {
            "running": False,
            "wechat_ready": False,
            "deepseek_configured": self.ai.configured(),
            "last_sync_at": None,
            "last_error": None,
            "verified_databases": 0,
            "discovered_groups": 0,
            "last_new_messages": 0,
            "last_new_tasks": 0,
            "last_merged_tasks": 0,
            "last_consolidated_tasks": 0,
            "last_extracted_files": 0,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="school-pipeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def request_sync(self) -> None:
        self._wake.set()

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    def _set_state(self, **values: Any) -> None:
        with self._state_lock:
            self._state.update(values)

    def _prepare_reader(self) -> None:
        if self.reader.ready():
            return
        result = self.reader.initialize()
        self._set_state(
            wechat_ready=True,
            verified_databases=result["verified"],
            last_error=None,
        )
        logger.info("微信只读数据库已连接，验证 %s/%s 个库", result["verified"], result["total"])

    def _refresh_groups(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_group_refresh < config.GROUP_REFRESH_SECONDS:
            return
        groups = self.reader.list_groups()
        self.store.upsert_groups(groups)
        self._last_group_refresh = now
        self._set_state(discovered_groups=len(groups))

    def _sync_messages(self, force: bool = False) -> int:
        signature = self.reader.change_signature()
        if not force and signature == self._last_signature:
            return 0
        total = 0
        backlog = False
        for group in self.store.selected_groups():
            rows = self.reader.fetch_group_messages(
                group["id"], group["cursor_time"], group["cursor_local_id"]
            )
            if rows:
                total += len(self.store.insert_messages(group["id"], rows))
                backlog = backlog or len(rows) >= config.MAX_INITIAL_MESSAGES
        self._last_signature = None if backlog else signature
        if backlog:
            self._wake.set()
        self._set_state(last_new_messages=total)
        return total

    def _reconcile_files(self) -> int:
        if time.monotonic() - self._last_file_reconcile < 30:
            return 0
        self._last_file_reconcile = time.monotonic()
        found = 0
        for message in self.store.missing_file_messages():
            path = self.reader.resolve_file(message["file_name"], message.get("file_size") or 0)
            if path:
                self.store.mark_file_available(message["id"], path)
                found += 1
        return found

    def _extract_files(self) -> int:
        extracted = 0
        for message in self.store.pending_file_extractions():
            result = extract_local_file(message["local_path"])
            self.store.finish_file_extraction(
                message["id"], result["state"], result["text"], result["error"]
            )
            extracted += 1
        self._set_state(last_extracted_files=extracted)
        return extracted

    @staticmethod
    def _normalize_due(task: dict[str, Any]) -> tuple[str | None, str | None, bool]:
        raw = str(task.get("due_at") or "").strip()
        all_day = bool(task.get("all_day"))
        if not raw:
            return None, None, all_day
        try:
            if len(raw) == 10:
                due = datetime.fromisoformat(raw).astimezone()
                all_day = True
                due_for_reminder = due.replace(hour=18, minute=0)
            else:
                due_for_reminder = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone()
            lead = max(0, min(10080, int(task.get("reminder_lead_minutes", 1440))))
            reminder = due_for_reminder - timedelta(minutes=lead)
            if reminder < datetime.now().astimezone():
                reminder = datetime.now().astimezone()
            return raw, reminder.isoformat(timespec="seconds"), all_day
        except (ValueError, TypeError):
            return None, None, all_day

    def _process_ai(self) -> int:
        if not self.ai.configured() or time.monotonic() < self._ai_retry_after:
            return 0
        batch = self.store.pending_messages(25)
        if not batch:
            return 0
        group_id = batch[0]["group_id"]
        batch = [message for message in batch if message["group_id"] == group_id]
        ids = [message["id"] for message in batch]
        try:
            candidates = self.store.dedup_candidates(group_id)
            candidate_ids = {int(task["task_id"]) for task in candidates}
            extracted = self.ai.extract_tasks(batch, candidates)
            valid_ids = {int(message["id"]): message for message in batch}
            created = 0
            merged = 0
            for task in extracted:
                related = [int(value) for value in task.get("related_message_ids", []) if str(value).isdigit()]
                related = [value for value in related if value in valid_ids] or ids
                evidence = [valid_ids[value] for value in related]
                due_at, reminder_at, all_day = self._normalize_due(task)
                attachments = [message for message in evidence if message.get("message_type") == "file"]
                requires = bool(task.get("requires_attachment"))
                if requires:
                    if any(message.get("file_text") for message in attachments):
                        attachment_state = "available"
                    elif any(message.get("local_path") for message in attachments):
                        attachment_state = "unreadable"
                    else:
                        attachment_state = "missing"
                else:
                    attachment_state = "available" if any(message.get("local_path") for message in attachments) else "none"
                record = {
                    "title": str(task.get("title", "")).strip()[:180],
                    "description": str(task.get("description", "")).strip()[:4000],
                    "action_text": str(task.get("action_text", "")).strip()[:1000],
                    "due_at": due_at,
                    "all_day": all_day,
                    "priority": task.get("priority") if task.get("priority") in {"urgent", "high", "normal", "low"} else "normal",
                    "source_group_id": group_id,
                    "source_group_name": batch[0]["group_name"],
                    "requires_attachment": requires,
                    "attachment_state": attachment_state,
                    "reminder_at": reminder_at,
                    "confidence": max(0.0, min(1.0, float(task.get("confidence", 0.5)))),
                }
                duplicate_id = task.get("duplicate_task_id")
                try:
                    duplicate_id = int(duplicate_id) if duplicate_id is not None else None
                except (TypeError, ValueError):
                    duplicate_id = None
                if duplicate_id in candidate_ids and self.store.merge_ai_duplicate(
                    duplicate_id, record, related
                ):
                    merged += 1
                else:
                    self.store.upsert_task(record, related)
                    created += 1
            self.store.mark_messages(ids, "processed")
            self._set_state(last_new_tasks=created, last_merged_tasks=merged, last_error=None)
            return created
        except Exception as exc:
            error = f"AI 提取失败：{type(exc).__name__}: {exc}"[:300]
            self.store.mark_messages(ids, "pending", error)
            self._ai_retry_after = time.monotonic() + 60
            self._set_state(last_error=error)
            logger.exception("DeepSeek 事项提取失败")
            return 0

    @staticmethod
    def _dedup_signature(candidates: list[dict[str, Any]]) -> str:
        payload = [
            {
                "id": task["task_id"],
                "title": task.get("title"),
                "due": task.get("due_at"),
                "status": task.get("status"),
                "updated": task.get("updated_at"),
                "evidence": task.get("evidence_count"),
            }
            for task in candidates
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _cleanup_existing_duplicates(self) -> int:
        if not self.ai.configured() or time.monotonic() < self._dedup_retry_after:
            return 0
        candidates = self.store.dedup_candidates(None, 100)
        if len(candidates) < 2:
            return 0
        signature = self._dedup_signature(candidates)
        if self.store.get_setting("semantic_dedup_signature") == signature:
            return 0
        valid_ids = {int(task["task_id"]) for task in candidates}
        try:
            groups = self.ai.find_duplicate_task_groups(candidates)
            used: set[int] = set()
            consolidated = 0
            for group in groups:
                try:
                    keep_id = int(group.get("keep_task_id"))
                except (TypeError, ValueError):
                    continue
                if keep_id not in valid_ids or keep_id in used:
                    continue
                merge_ids: list[int] = []
                for raw_id in group.get("merge_task_ids", []):
                    try:
                        value = int(raw_id)
                    except (TypeError, ValueError):
                        continue
                    if value in valid_ids and value != keep_id and value not in used:
                        merge_ids.append(value)
                if not merge_ids:
                    continue
                count = self.store.consolidate_duplicate_tasks(keep_id, merge_ids)
                if count:
                    used.add(keep_id)
                    used.update(merge_ids)
                    consolidated += count
            refreshed = self.store.dedup_candidates(None, 100)
            self.store.set_setting("semantic_dedup_signature", self._dedup_signature(refreshed))
            self._set_state(last_consolidated_tasks=consolidated, last_error=None)
            if consolidated:
                logger.info("AI 存量去重完成，合并 %s 条重复事项", consolidated)
            return consolidated
        except Exception as exc:
            self._dedup_retry_after = time.monotonic() + 60
            error = f"AI 存量去重失败：{type(exc).__name__}: {exc}"[:300]
            self._set_state(last_error=error)
            logger.exception("DeepSeek 存量事项去重失败")
            return 0

    def _run(self) -> None:
        self._set_state(running=True)
        force = True
        while not self._stop.is_set():
            try:
                self._prepare_reader()
                self._refresh_groups(force=force)
                new_messages = self._sync_messages(force=force)
                reconciled_files = self._reconcile_files()
                extracted_files = self._extract_files()
                new_tasks = self._process_ai()
                consolidated_tasks = self._cleanup_existing_duplicates()
                if (
                    self.on_data_changed
                    and any((new_messages, reconciled_files, extracted_files, new_tasks, consolidated_tasks))
                ):
                    try:
                        self.on_data_changed()
                    except Exception:
                        logger.exception("请求增量检索索引更新失败")
                current_error = self.status().get("last_error") or ""
                self._set_state(
                    wechat_ready=True,
                    last_sync_at=now_iso(),
                    last_new_messages=new_messages,
                    last_new_tasks=new_tasks,
                    last_error=current_error if current_error.startswith("AI") else None,
                )
                force = False
            except Exception as exc:
                self._set_state(
                    wechat_ready=False,
                    last_error=f"{type(exc).__name__}: {exc}"[:300],
                )
                logger.exception("微信只读同步失败")
                force = True
            self._wake.wait(config.POLL_SECONDS)
            if self._wake.is_set():
                force = True
                self._wake.clear()
        self._set_state(running=False)
