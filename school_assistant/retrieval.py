"""Local hybrid retrieval over selected WeChat messages, attachments and tasks."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Any, Iterable

import numpy as np

from . import config
from .database import Store, now_iso


logger = logging.getLogger("school_assistant.retrieval")


def _clean_text(value: Any) -> str:
    return re.sub(r"[ \t]+", " ", str(value or "").replace("\x00", "")).strip()


def _format_time(timestamp: int | None) -> str:
    if not timestamp:
        return "时间未知"
    return datetime.fromtimestamp(int(timestamp)).astimezone().isoformat(timespec="minutes")


def split_text(text: str, size: int = config.RAG_CHUNK_CHARS, overlap: int = config.RAG_CHUNK_OVERLAP) -> list[str]:
    """Split extracted files into small, overlapping, human-readable chunks."""
    text = re.sub(r"\r\n?", "\n", _clean_text(text))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + size)
        end = hard_end
        if hard_end < len(text):
            floor = start + int(size * 0.62)
            candidates = [
                text.rfind(marker, floor, hard_end)
                for marker in ("\n\n", "\n", "。", "；", "！", "？")
            ]
            boundary = max(candidates)
            if boundary > floor:
                end = boundary + (2 if text.startswith("\n\n", boundary) else 1)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        # Prefer starting after nearby whitespace/punctuation inside the overlap.
        nearby = max(text.rfind("\n", next_start, end), text.rfind("。", next_start, end))
        start = nearby + 1 if nearby >= next_start else next_start
    return chunks


class RetrievalIndex:
    """Persistent SQLite vectors plus deterministic lexical scoring.

    The complete corpus is cheap to enumerate at this scale.  Only new or
    changed chunks are embedded, so each query sees newly detected messages
    without rebuilding the index.
    """

    def __init__(self, store: Store):
        self.store = store
        self.model_name = config.EMBEDDING_MODEL
        self._model: Any | None = None
        self._model_error: str | None = None
        self._model_error_at = 0.0
        self._lock = threading.RLock()
        self._last_sync_monotonic = 0.0
        self._status: dict[str, Any] = {
            "model": self.model_name,
            "model_loaded": False,
            "chunks": 0,
            "pending_embeddings": 0,
            "last_sync_at": None,
            "last_error": None,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _load_model(self):
        if self._model is not None:
            return self._model
        if self._model_error and time.monotonic() - self._model_error_at < 300:
            raise RuntimeError(self._model_error)
        try:
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            from fastembed import TextEmbedding

            self._model = TextEmbedding(
                model_name=self.model_name,
                cache_dir=str(config.EMBEDDING_CACHE_DIR),
                threads=config.EMBEDDING_THREADS,
            )
            self._status["model_loaded"] = True
            self._model_error = None
            return self._model
        except Exception as exc:
            self._model_error = f"{type(exc).__name__}: {exc}"[:300]
            self._model_error_at = time.monotonic()
            self._status["last_error"] = self._model_error
            raise

    @staticmethod
    def _document(
        *,
        source_kind: str,
        source_id: str | int,
        chunk_index: int,
        group_id: str | None,
        group_name: str | None,
        title: str,
        content: str,
        create_time: int | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        source_key = f"{source_kind}:{source_id}:{chunk_index}"
        return {
            "source_key": source_key,
            "source_kind": source_kind,
            "source_id": str(source_id),
            "chunk_index": int(chunk_index),
            "group_id": group_id,
            "group_name": group_name,
            "title": _clean_text(title)[:300],
            "content": _clean_text(content)[:6000],
            "create_time": int(create_time) if create_time else None,
            "metadata_json": json.dumps(metadata, ensure_ascii=False, default=str),
            "content_hash": hashlib.sha256(_clean_text(content).encode("utf-8")).hexdigest(),
        }

    def _build_documents(self) -> list[dict[str, Any]]:
        corpus = self.store.retrieval_source_rows()
        documents: list[dict[str, Any]] = []

        for message in corpus["messages"]:
            group = _clean_text(message.get("group_name"))
            sender = _clean_text(message.get("sender_name")) or "未知发送者"
            timestamp = int(message.get("create_time") or 0)
            body = _clean_text(message.get("content"))
            file_name = _clean_text(message.get("file_name"))
            file_state = _clean_text(message.get("download_state"))
            extract_state = _clean_text(message.get("file_extract_state"))
            header = f"群聊：{group}\n时间：{_format_time(timestamp)}\n发送者：{sender}"
            parts = [header]
            if body:
                parts.append(f"消息：{body}")
            if file_name:
                parts.append(
                    f"附件：{file_name}\n下载状态：{file_state or '未知'}\n"
                    f"正文提取状态：{extract_state or '未知'}"
                )
                if message.get("file_extract_error"):
                    parts.append(f"附件提示：{_clean_text(message['file_extract_error'])}")
            if body or file_name:
                documents.append(self._document(
                    source_kind="message",
                    source_id=message["id"],
                    chunk_index=0,
                    group_id=message.get("group_id"),
                    group_name=group,
                    title=file_name or "群聊消息",
                    content="\n".join(parts),
                    create_time=timestamp,
                    metadata={
                        "message_id": message["id"],
                        "sender": sender,
                        "message_type": message.get("message_type"),
                        "file_name": file_name or None,
                        "download_state": file_state or None,
                        "extract_state": extract_state or None,
                    },
                ))

            file_text = _clean_text(message.get("file_text"))
            for chunk_index, chunk in enumerate(split_text(file_text)):
                documents.append(self._document(
                    source_kind="attachment",
                    source_id=message["id"],
                    chunk_index=chunk_index,
                    group_id=message.get("group_id"),
                    group_name=group,
                    title=file_name or "聊天附件",
                    content=(
                        f"群聊：{group}\n时间：{_format_time(timestamp)}\n"
                        f"附件：{file_name or '未命名附件'}\n正文片段：\n{chunk}"
                    ),
                    create_time=timestamp,
                    metadata={
                        "message_id": message["id"],
                        "file_name": file_name or None,
                        "chunk": chunk_index,
                        "dedup_key": hashlib.sha256(
                            (file_name + "\n" + chunk).encode("utf-8")
                        ).hexdigest(),
                    },
                ))

        for task in corpus["tasks"]:
            group = _clean_text(task.get("source_group_name"))
            title = _clean_text(task.get("title"))
            content = "\n".join(filter(None, [
                f"事项：{title}",
                f"状态：{_clean_text(task.get('status')) or '未知'}",
                f"来源群：{group}" if group else "",
                f"截止时间：{_clean_text(task.get('due_at'))}" if task.get("due_at") else "截止时间：未明确",
                f"说明：{_clean_text(task.get('description'))}" if task.get("description") else "",
                f"需要执行：{_clean_text(task.get('action_text'))}" if task.get("action_text") else "",
                f"附件状态：{_clean_text(task.get('attachment_state'))}" if task.get("requires_attachment") else "",
            ]))
            documents.append(self._document(
                source_kind="task",
                source_id=task["id"],
                chunk_index=0,
                group_id=task.get("source_group_id"),
                group_name=group,
                title=title,
                content=content,
                create_time=None,
                metadata={
                    "task_id": task["id"],
                    "status": task.get("status"),
                    "due_at": task.get("due_at"),
                    "requires_attachment": bool(task.get("requires_attachment")),
                    "attachment_state": task.get("attachment_state"),
                },
            ))
        return documents

    def sync(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if not force and time.monotonic() - self._last_sync_monotonic < config.RAG_SYNC_SECONDS:
                return self.status()
            documents = self._build_documents()
            by_key = {item["source_key"]: item for item in documents if item["content"]}
            stamp = now_iso()

            with self.store.transaction() as conn:
                existing = {
                    row["source_key"]: dict(row)
                    for row in conn.execute(
                        "SELECT source_key,content_hash,embedding_model,embedding FROM rag_chunks"
                    ).fetchall()
                }
                for key, document in by_key.items():
                    old = existing.get(key)
                    changed = not old or old["content_hash"] != document["content_hash"]
                    stale_model = bool(old and old["embedding_model"] != self.model_name)
                    if not old:
                        conn.execute(
                            """
                            INSERT INTO rag_chunks(
                                source_key,source_kind,source_id,chunk_index,group_id,group_name,
                                title,content,create_time,metadata_json,content_hash,updated_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                document["source_key"], document["source_kind"], document["source_id"],
                                document["chunk_index"], document["group_id"], document["group_name"],
                                document["title"], document["content"], document["create_time"],
                                document["metadata_json"], document["content_hash"], stamp,
                            ),
                        )
                    else:
                        clear = changed or stale_model or old["embedding"] is None
                        conn.execute(
                            """
                            UPDATE rag_chunks SET source_kind=?,source_id=?,chunk_index=?,group_id=?,
                                group_name=?,title=?,content=?,create_time=?,metadata_json=?,
                                content_hash=?,embedding_model=CASE WHEN ? THEN NULL ELSE embedding_model END,
                                embedding_dim=CASE WHEN ? THEN NULL ELSE embedding_dim END,
                                embedding=CASE WHEN ? THEN NULL ELSE embedding END,updated_at=?
                            WHERE source_key=?
                            """,
                            (
                                document["source_kind"], document["source_id"], document["chunk_index"],
                                document["group_id"], document["group_name"], document["title"],
                                document["content"], document["create_time"], document["metadata_json"],
                                document["content_hash"], int(clear), int(clear), int(clear), stamp, key,
                            ),
                        )
                stale_keys = set(existing) - set(by_key)
                for key in stale_keys:
                    conn.execute("DELETE FROM rag_chunks WHERE source_key=?", (key,))

            with self.store.connection() as conn:
                pending = [dict(row) for row in conn.execute(
                    """
                    SELECT id,content FROM rag_chunks
                    WHERE embedding IS NULL OR embedding_model<>?
                    ORDER BY id
                    """,
                    (self.model_name,),
                ).fetchall()]

            self._status.update({"chunks": len(by_key), "pending_embeddings": len(pending)})
            if pending:
                try:
                    model = self._load_model()
                    batch_size = 32
                    for offset in range(0, len(pending), batch_size):
                        batch = pending[offset:offset + batch_size]
                        vectors = list(model.embed([row["content"] for row in batch], batch_size=batch_size))
                        with self.store.transaction() as conn:
                            for row, raw_vector in zip(batch, vectors):
                                vector = np.asarray(raw_vector, dtype=np.float32)
                                norm = float(np.linalg.norm(vector))
                                if norm:
                                    vector /= norm
                                conn.execute(
                                    """
                                    UPDATE rag_chunks SET embedding_model=?,embedding_dim=?,embedding=?,updated_at=?
                                    WHERE id=?
                                    """,
                                    (self.model_name, len(vector), vector.tobytes(), stamp, row["id"]),
                                )
                    self._status["pending_embeddings"] = 0
                    self._status["last_error"] = None
                except Exception as exc:
                    self._status["last_error"] = f"向量模型不可用，已退回关键词检索：{type(exc).__name__}: {exc}"[:300]
                    logger.exception("本地向量索引更新失败，继续使用关键词检索")
            elif force:
                try:
                    self._load_model()
                    self._status["last_error"] = None
                except Exception as exc:
                    self._status["last_error"] = f"向量模型不可用，已退回关键词检索：{type(exc).__name__}: {exc}"[:300]
                    logger.exception("本地向量模型预热失败，继续使用关键词检索")

            self._last_sync_monotonic = time.monotonic()
            self._status["last_sync_at"] = now_iso()
            return self.status()

    @staticmethod
    def _terms(question: str, search_terms: Iterable[str]) -> list[str]:
        values: list[str] = []
        raw_values = [question, *search_terms]
        for value in raw_values:
            clean = re.sub(r"\s+", "", _clean_text(value)).casefold()
            if 1 <= len(clean) <= 80 and clean not in values:
                values.append(clean)
            for segment in re.split(r"[\s,，、;；:：/|]+", _clean_text(value)):
                segment = segment.strip().casefold()
                if 1 <= len(segment) <= 30 and segment not in values:
                    values.append(segment)
        # Add durable letter/number tokens without attempting fragile Chinese word segmentation.
        for token in re.findall(r"[a-z]+\d*|\d+(?:\.\d+)?", question.casefold()):
            if token not in values:
                values.append(token)
        return values[:16]

    @staticmethod
    def _lexical_score(row: dict[str, Any], question: str, terms: list[str]) -> float:
        title = re.sub(r"\s+", "", _clean_text(row.get("title"))).casefold()
        group = re.sub(r"\s+", "", _clean_text(row.get("group_name"))).casefold()
        content = re.sub(r"\s+", "", _clean_text(row.get("content"))).casefold()
        compact_question = re.sub(r"\s+", "", question).casefold()
        score = 0.0
        if len(compact_question) >= 2 and compact_question in content:
            score += 12.0
        for term in terms:
            if not term:
                continue
            if term in {"时间", "地点", "课程", "通知", "事项", "什么", "分别", "时候"}:
                weight = 0.3
            else:
                weight = 0.65 if len(term) == 1 else 1.0
            if term in title:
                score += 6.0 * weight
            if term in group:
                score += 3.5 * weight
            occurrences = content.count(term)
            if occurrences:
                score += min(5.0, 1.8 + math.log2(occurrences + 1)) * weight
        return score

    def _query_vector(self, question: str) -> np.ndarray | None:
        try:
            model = self._load_model()
            vector = np.asarray(next(iter(model.query_embed(question))), dtype=np.float32)
            norm = float(np.linalg.norm(vector))
            return vector / norm if norm else vector
        except Exception as exc:
            logger.warning("查询向量生成失败，使用关键词检索：%s", exc)
            return None

    def search(
        self,
        question: str,
        *,
        search_terms: Iterable[str] = (),
        top_k: int = config.RAG_TOP_K,
    ) -> dict[str, Any]:
        self.sync()
        with self.store.connection() as conn:
            rows = [dict(row) for row in conn.execute(
                """
                SELECT id,source_key,source_kind,source_id,chunk_index,group_id,group_name,
                       title,content,create_time,metadata_json,embedding_model,embedding_dim,embedding
                FROM rag_chunks
                """
            ).fetchall()]

        terms = self._terms(question, search_terms)
        query_vector = self._query_vector(question) if rows else None
        scored: list[dict[str, Any]] = []
        for row in rows:
            lexical = self._lexical_score(row, question, terms)
            dense = 0.0
            if query_vector is not None and row.get("embedding") and row.get("embedding_model") == self.model_name:
                vector = np.frombuffer(row["embedding"], dtype=np.float32)
                if vector.size == query_vector.size:
                    dense = float(np.dot(query_vector, vector))
            lexical_normalized = 1.0 - math.exp(-lexical / 22.0)
            # Exact words dominate dates/names; dense recall fills vocabulary gaps.
            combined = (0.58 * dense) + (0.42 * lexical_normalized)
            if lexical >= 10:
                combined += 0.06
            row.update({"lexical_score": lexical, "vector_score": dense, "score": combined})
            scored.append(row)
        scored.sort(key=lambda item: (item["score"], item["lexical_score"], item.get("create_time") or 0), reverse=True)

        selected: list[dict[str, Any]] = []
        per_source: Counter[tuple[str, str]] = Counter()
        seen_attachment_chunks: set[str] = set()
        for row in scored:
            source = (row["source_kind"], row["source_id"])
            max_per_source = 4 if row["source_kind"] == "attachment" else 1
            if per_source[source] >= max_per_source:
                continue
            if row["source_kind"] == "attachment":
                try:
                    dedup_key = str(json.loads(row.get("metadata_json") or "{}").get("dedup_key") or "")
                except json.JSONDecodeError:
                    dedup_key = ""
                if dedup_key and dedup_key in seen_attachment_chunks:
                    continue
            # With no meaningful lexical or semantic score, the corpus is not evidence.
            if row["lexical_score"] <= 0 and row["vector_score"] < 0.30:
                continue
            selected.append(row)
            per_source[source] += 1
            if row["source_kind"] == "attachment" and dedup_key:
                seen_attachment_chunks.add(dedup_key)
            if len(selected) >= max(1, int(top_k)):
                break

        sources: list[dict[str, Any]] = []
        context_chars = 0
        for index, row in enumerate(selected, start=1):
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except json.JSONDecodeError:
                metadata = {}
            content = row["content"]
            if context_chars + len(content) > 28000:
                content = content[: max(0, 28000 - context_chars)]
            if not content:
                break
            item = {
                "ref": f"S{index}",
                "kind": row["source_kind"],
                "source_id": row["source_id"],
                "group": row.get("group_name"),
                "time": _format_time(row.get("create_time")),
                "title": row.get("title"),
                "content": content,
                "metadata": metadata,
                "score": round(float(row["score"]), 4),
            }
            sources.append(item)
            context_chars += len(content)

        neighbor_messages: list[dict[str, Any]] = []
        seen_neighbor_ids: set[int] = set()
        for row in selected[:6]:
            if row["source_kind"] not in {"message", "attachment"}:
                continue
            for neighbor in self.store.message_neighbors(int(row["source_id"]), radius=2):
                neighbor_id = int(neighbor["id"])
                if neighbor_id in seen_neighbor_ids:
                    continue
                seen_neighbor_ids.add(neighbor_id)
                neighbor["time"] = _format_time(neighbor.get("create_time"))
                neighbor_messages.append(neighbor)
                if len(neighbor_messages) >= 16:
                    break
            if len(neighbor_messages) >= 16:
                break

        return {
            "question": question,
            "terms": terms,
            "sources": sources,
            "neighbor_messages": neighbor_messages,
            "index": self.status(),
        }
