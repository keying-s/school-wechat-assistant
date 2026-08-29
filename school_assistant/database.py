"""SQLite persistence for messages, tasks, groups, and reminder delivery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import config


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str = config.DB_PATH):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def connection(self):
        """Yield a read connection and always release its Windows file handles."""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0,
                    lookback_days INTEGER NOT NULL DEFAULT 7,
                    cursor_time INTEGER NOT NULL DEFAULT 0,
                    cursor_local_id INTEGER NOT NULL DEFAULT 0,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_sync_at TEXT
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                    local_id INTEGER NOT NULL,
                    server_id TEXT,
                    sender_id TEXT,
                    sender_name TEXT,
                    create_time INTEGER NOT NULL,
                    message_type TEXT NOT NULL,
                    app_subtype INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL DEFAULT '',
                    file_name TEXT,
                    file_size INTEGER,
                    file_md5 TEXT,
                    local_path TEXT,
                    download_state TEXT NOT NULL DEFAULT 'none',
                    file_text TEXT NOT NULL DEFAULT '',
                    file_extract_state TEXT NOT NULL DEFAULT 'none',
                    file_extract_error TEXT,
                    ai_state TEXT NOT NULL DEFAULT 'pending',
                    ai_error TEXT,
                    inserted_at TEXT NOT NULL,
                    UNIQUE(group_id, local_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_ai ON messages(ai_state, create_time);
                CREATE INDEX IF NOT EXISTS idx_messages_group_time ON messages(group_id, create_time);

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    action_text TEXT NOT NULL DEFAULT '',
                    due_at TEXT,
                    all_day INTEGER NOT NULL DEFAULT 0,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    status TEXT NOT NULL DEFAULT 'open',
                    source_group_id TEXT REFERENCES groups(id) ON DELETE SET NULL,
                    source_group_name TEXT,
                    requires_attachment INTEGER NOT NULL DEFAULT 0,
                    attachment_state TEXT NOT NULL DEFAULT 'none',
                    reminder_at TEXT,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    merged_into_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, due_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_reminder ON tasks(status, reminder_at);

                CREATE TABLE IF NOT EXISTS task_messages (
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    PRIMARY KEY(task_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    claimed_at TEXT,
                    delivered_at TEXT,
                    delivery_error TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, kind, scheduled_at)
                );
                CREATE INDEX IF NOT EXISTS idx_notifications_due
                    ON notifications(delivered_at, scheduled_at, claimed_at);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            message_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            migrations = {
                "file_text": "ALTER TABLE messages ADD COLUMN file_text TEXT NOT NULL DEFAULT ''",
                "file_extract_state": "ALTER TABLE messages ADD COLUMN file_extract_state TEXT NOT NULL DEFAULT 'none'",
                "file_extract_error": "ALTER TABLE messages ADD COLUMN file_extract_error TEXT",
            }
            for column, statement in migrations.items():
                if column not in message_columns:
                    conn.execute(statement)
            task_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "merged_into_task_id" not in task_columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN merged_into_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL"
                )
            conn.execute(
                """
                UPDATE messages SET file_extract_state=CASE
                    WHEN message_type='file' AND download_state='available' AND local_path IS NOT NULL THEN 'pending'
                    WHEN message_type='file' AND download_state='missing' THEN 'waiting'
                    ELSE file_extract_state END
                WHERE file_extract_state='none'
                """
            )
            conn.commit()

    def upsert_groups(self, groups: Iterable[dict[str, Any]]) -> int:
        stamp = now_iso()
        count = 0
        with self.transaction() as conn:
            for group in groups:
                conn.execute(
                    """
                    INSERT INTO groups(id,name,lookback_days,discovered_at,updated_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,updated_at=excluded.updated_at
                    """,
                    (group["id"], group["name"], config.DEFAULT_LOOKBACK_DAYS, stamp, stamp),
                )
                count += 1
        return count

    def list_groups(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT g.*,
                       (SELECT COUNT(*) FROM messages m WHERE m.group_id=g.id) AS message_count,
                       (SELECT COUNT(*) FROM tasks t WHERE t.source_group_id=g.id AND t.status='open') AS open_tasks
                FROM groups g ORDER BY selected DESC, name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def selected_groups(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM groups WHERE selected=1 ORDER BY name"
            ).fetchall()]

    def select_group(self, group_id: str, selected: bool, lookback_days: int | None = None) -> bool:
        lookback = max(1, min(30, int(lookback_days or config.DEFAULT_LOOKBACK_DAYS)))
        with self.transaction() as conn:
            row = conn.execute("SELECT selected,cursor_time FROM groups WHERE id=?", (group_id,)).fetchone()
            if not row:
                return False
            cursor_time = int(row["cursor_time"] or 0)
            if selected and not cursor_time:
                cursor_time = int((datetime.now().astimezone() - timedelta(days=lookback)).timestamp())
            conn.execute(
                "UPDATE groups SET selected=?,lookback_days=?,cursor_time=?,updated_at=? WHERE id=?",
                (int(selected), lookback, cursor_time, now_iso(), group_id),
            )
        return True

    def insert_messages(self, group_id: str, messages: Iterable[dict[str, Any]]) -> list[int]:
        inserted: list[int] = []
        max_cursor: tuple[int, int] | None = None
        stamp = now_iso()
        with self.transaction() as conn:
            for message in messages:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO messages(
                        group_id,local_id,server_id,sender_id,sender_name,create_time,
                        message_type,app_subtype,content,file_name,file_size,file_md5,
                        local_path,download_state,file_extract_state,inserted_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        group_id, message["local_id"], message.get("server_id"),
                        message.get("sender_id"), message.get("sender_name"), message["create_time"],
                        message["message_type"], message.get("app_subtype", 0), message.get("content", ""),
                        message.get("file_name"), message.get("file_size"), message.get("file_md5"),
                        message.get("local_path"), message.get("download_state", "none"),
                        (
                            "pending" if message.get("message_type") == "file" and message.get("local_path")
                            else "waiting" if message.get("message_type") == "file" else "none"
                        ),
                        stamp,
                    ),
                )
                if cur.rowcount:
                    inserted.append(int(cur.lastrowid))
                cursor = (int(message["create_time"]), int(message["local_id"]))
                max_cursor = cursor if max_cursor is None or cursor > max_cursor else max_cursor
            if max_cursor:
                conn.execute(
                    "UPDATE groups SET cursor_time=?,cursor_local_id=?,last_sync_at=?,updated_at=? WHERE id=?",
                    (max_cursor[0], max_cursor[1], stamp, stamp, group_id),
                )
        return inserted

    def pending_messages(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT m.*,g.name AS group_name FROM messages m
                JOIN groups g ON g.id=m.group_id
                WHERE m.ai_state='pending' AND g.selected=1
                  AND NOT (m.message_type='file' AND m.download_state='available'
                           AND m.file_extract_state='pending')
                ORDER BY m.group_id,m.create_time,m.local_id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def missing_file_messages(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id,file_name,file_size FROM messages
                WHERE message_type='file' AND download_state='missing' AND file_name IS NOT NULL
                ORDER BY create_time DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_file_available(self, message_id: int, local_path: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """UPDATE messages SET local_path=?,download_state='available',
                           file_extract_state='pending',file_extract_error=NULL,ai_state='pending'
                   WHERE id=?""",
                (local_path, int(message_id)),
            )
            conn.execute(
                """
                UPDATE tasks SET attachment_state='checking',updated_at=?
                WHERE id IN (SELECT task_id FROM task_messages WHERE message_id=?)
                  AND requires_attachment=1
                """,
                (now_iso(), int(message_id)),
            )
            conn.execute(
                """DELETE FROM notifications WHERE kind='missing_attachment'
                   AND delivered_at IS NULL
                   AND task_id IN (SELECT task_id FROM task_messages WHERE message_id=?)""",
                (int(message_id),),
            )

    def pending_file_extractions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id,file_name,local_path FROM messages
                WHERE message_type='file' AND download_state='available'
                  AND local_path IS NOT NULL AND file_extract_state='pending'
                ORDER BY create_time LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_file_extraction(
        self, message_id: int, state: str, text: str = "", error: str | None = None
    ) -> None:
        stamp = now_iso()
        attachment_state = "available" if state == "extracted" and text.strip() else "unreadable"
        with self.transaction() as conn:
            conn.execute(
                """UPDATE messages SET file_text=?,file_extract_state=?,file_extract_error=?,ai_state='pending'
                   WHERE id=?""",
                (text[:30000], state, (error or "")[:300] or None, int(message_id)),
            )
            linked = conn.execute(
                """SELECT id,title FROM tasks WHERE requires_attachment=1
                   AND id IN (SELECT task_id FROM task_messages WHERE message_id=?)""",
                (int(message_id),),
            ).fetchall()
            for task in linked:
                conn.execute(
                    "UPDATE tasks SET attachment_state=?,updated_at=? WHERE id=?",
                    (attachment_state, stamp, int(task["id"])),
                )
                if attachment_state == "unreadable":
                    self._schedule_notifications(
                        conn,
                        int(task["id"]),
                        {
                            "title": task["title"],
                            "requires_attachment": True,
                            "attachment_state": "unreadable",
                        },
                        stamp,
                    )

    def mark_messages(self, ids: Iterable[int], state: str, error: str | None = None) -> None:
        ids = [int(value) for value in ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE messages SET ai_state=?,ai_error=? WHERE id IN ({placeholders})",
                (state, error, *ids),
            )

    @staticmethod
    def _fingerprint(task: dict[str, Any], group_id: str | None) -> str:
        raw = "|".join([
            group_id or "manual",
            str(task.get("title", "")).strip().casefold(),
            str(task.get("due_at") or ""),
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def dedup_candidates(self, group_id: str | None, limit: int = 80) -> list[dict[str, Any]]:
        """Recent tasks exposed to the AI so reminders can be merged semantically."""
        cutoff = (datetime.now().astimezone() - timedelta(days=90)).isoformat(timespec="seconds")
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT t.id AS task_id,t.title,t.description,t.action_text,t.due_at,t.status,
                       t.source_group_id,t.source_group_name,t.updated_at,
                       (SELECT COUNT(*) FROM task_messages tm WHERE tm.task_id=t.id) AS evidence_count
                FROM tasks t
                WHERE t.status<>'merged' AND (t.status='open' OR t.updated_at>=?)
                ORDER BY CASE WHEN t.source_group_id=? THEN 0 ELSE 1 END,
                         CASE WHEN t.status='open' THEN 0 ELSE 1 END,
                         t.updated_at DESC
                LIMIT ?
                """,
                (cutoff, group_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def consolidate_duplicate_tasks(
        self, keep_task_id: int, duplicate_task_ids: Iterable[int]
    ) -> int:
        """Hide duplicate rows while preserving them and all evidence for audit/recovery."""
        duplicate_ids = sorted({int(value) for value in duplicate_task_ids if int(value) != int(keep_task_id)})
        if not duplicate_ids:
            return 0
        ids = [int(keep_task_id), *duplicate_ids]
        placeholders = ",".join("?" for _ in ids)
        priority_rank = {"low": 0, "normal": 1, "high": 2, "urgent": 3}
        attachment_rank = {"none": 0, "checking": 1, "missing": 2, "unreadable": 3, "available": 4}
        stamp = now_iso()
        with self.transaction() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders}) AND status<>'merged'",
                ids,
            ).fetchall()
            by_id = {int(row["id"]): row for row in rows}
            keep = by_id.get(int(keep_task_id))
            duplicates = [by_id[value] for value in duplicate_ids if value in by_id]
            if not keep or not duplicates:
                return 0

            ordered = sorted([keep, *duplicates], key=lambda row: str(row["updated_at"] or ""))

            def combined_text(field: str, limit: int) -> str:
                parts: list[str] = []
                for row in ordered:
                    value = str(row[field] or "").strip()
                    if value and not any(value in part for part in parts):
                        parts.append(value)
                return "\n\n合并通知：".join(parts)[:limit]

            due_source = keep if keep["due_at"] else next(
                (row for row in reversed(ordered) if row["due_at"]), keep
            )
            priority = max(
                (str(row["priority"] or "normal") for row in ordered),
                key=lambda value: priority_rank.get(value, 1),
            )
            attachment_state = max(
                (str(row["attachment_state"] or "none") for row in ordered),
                key=lambda value: attachment_rank.get(value, 0),
            )
            completed_at = next(
                (row["completed_at"] for row in reversed(ordered) if row["completed_at"]), None
            )
            status = "done" if completed_at else "open"
            conn.execute(
                """
                UPDATE tasks SET description=?,action_text=?,due_at=?,all_day=?,priority=?,status=?,
                    requires_attachment=?,attachment_state=?,reminder_at=?,confidence=?,updated_at=?,
                    completed_at=? WHERE id=?
                """,
                (
                    combined_text("description", 4000),
                    combined_text("action_text", 1000),
                    due_source["due_at"],
                    int(due_source["all_day"]),
                    priority,
                    status,
                    max(int(row["requires_attachment"]) for row in ordered),
                    attachment_state,
                    due_source["reminder_at"],
                    max(float(row["confidence"] or 0.0) for row in ordered),
                    stamp,
                    completed_at,
                    int(keep_task_id),
                ),
            )
            for duplicate in duplicates:
                conn.execute(
                    """INSERT OR IGNORE INTO task_messages(task_id,message_id)
                       SELECT ?,message_id FROM task_messages WHERE task_id=?""",
                    (int(keep_task_id), int(duplicate["id"])),
                )
                conn.execute(
                    """UPDATE tasks SET status='merged',merged_into_task_id=?,updated_at=?
                       WHERE id=?""",
                    (int(keep_task_id), stamp, int(duplicate["id"])),
                )
            conn.execute(
                "DELETE FROM notifications WHERE task_id=? AND kind='due' AND delivered_at IS NULL",
                (int(keep_task_id),),
            )
            if due_source["reminder_at"] and status == "open":
                self._schedule_notifications(
                    conn,
                    int(keep_task_id),
                    {"title": keep["title"], "reminder_at": due_source["reminder_at"]},
                    stamp,
                )
        return len(duplicates)

    def merge_ai_duplicate(
        self, task_id: int, task: dict[str, Any], message_ids: Iterable[int] = ()
    ) -> bool:
        """Merge a model-confirmed repeat notification and retain all evidence."""
        stamp = now_iso()
        priority_rank = {"low": 0, "normal": 1, "high": 2, "urgent": 3}
        attachment_rank = {"none": 0, "checking": 1, "missing": 2, "unreadable": 3, "available": 4}
        with self.transaction() as conn:
            existing = conn.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
            if not existing:
                return False

            def merge_text(old: Any, new: Any, limit: int) -> str:
                old_text = str(old or "").strip()
                new_text = str(new or "").strip()
                if not new_text or new_text in old_text:
                    return old_text[:limit]
                if not old_text:
                    return new_text[:limit]
                return (old_text + "\n\n最新重复通知：" + new_text)[:limit]

            due_at = task.get("due_at") or existing["due_at"]
            reminder_at = task.get("reminder_at") if task.get("due_at") else existing["reminder_at"]
            priority = max(
                (str(existing["priority"]), str(task.get("priority") or "normal")),
                key=lambda value: priority_rank.get(value, 1),
            )
            old_attachment = str(existing["attachment_state"] or "none")
            new_attachment = str(task.get("attachment_state") or "none")
            attachment_state = max(
                (old_attachment, new_attachment),
                key=lambda value: attachment_rank.get(value, 0),
            )
            conn.execute(
                """
                UPDATE tasks SET description=?,action_text=?,due_at=?,all_day=?,priority=?,
                    requires_attachment=?,attachment_state=?,reminder_at=?,confidence=?,updated_at=?
                WHERE id=?
                """,
                (
                    merge_text(existing["description"], task.get("description"), 4000),
                    merge_text(existing["action_text"], task.get("action_text"), 1000),
                    due_at,
                    int(bool(task.get("all_day"))) if task.get("due_at") else int(existing["all_day"]),
                    priority,
                    max(int(existing["requires_attachment"]), int(bool(task.get("requires_attachment")))),
                    attachment_state,
                    reminder_at,
                    max(float(existing["confidence"]), float(task.get("confidence", 0.5))),
                    stamp,
                    int(task_id),
                ),
            )
            for message_id in message_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO task_messages(task_id,message_id) VALUES(?,?)",
                    (int(task_id), int(message_id)),
                )

            if task.get("due_at"):
                conn.execute(
                    "DELETE FROM notifications WHERE task_id=? AND kind='due' AND delivered_at IS NULL",
                    (int(task_id),),
                )
                if reminder_at:
                    self._schedule_notifications(
                        conn,
                        int(task_id),
                        {"title": existing["title"], "reminder_at": reminder_at},
                        stamp,
                    )
            if attachment_state != old_attachment and attachment_state in {"missing", "unreadable"}:
                self._schedule_notifications(
                    conn,
                    int(task_id),
                    {
                        "title": existing["title"],
                        "requires_attachment": True,
                        "attachment_state": attachment_state,
                    },
                    stamp,
                )
        return True

    def upsert_task(self, task: dict[str, Any], message_ids: Iterable[int] = ()) -> int:
        stamp = now_iso()
        group_id = task.get("source_group_id")
        fingerprint = task.get("fingerprint") or self._fingerprint(task, group_id)
        due_at = task.get("due_at") or None
        reminder_at = task.get("reminder_at") or None
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    fingerprint,title,description,action_text,due_at,all_day,priority,status,
                    source_group_id,source_group_name,requires_attachment,attachment_state,
                    reminder_at,confidence,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    description=CASE WHEN length(excluded.description)>length(tasks.description)
                                     THEN excluded.description ELSE tasks.description END,
                    action_text=CASE WHEN length(excluded.action_text)>length(tasks.action_text)
                                     THEN excluded.action_text ELSE tasks.action_text END,
                    priority=excluded.priority,
                    requires_attachment=max(tasks.requires_attachment,excluded.requires_attachment),
                    attachment_state=CASE WHEN tasks.attachment_state='available' THEN 'available'
                                          ELSE excluded.attachment_state END,
                    updated_at=excluded.updated_at
                """,
                (
                    fingerprint, task["title"].strip(), task.get("description", "").strip(),
                    task.get("action_text", "").strip(), due_at, int(bool(task.get("all_day"))),
                    task.get("priority", "normal"), task.get("status", "open"), group_id,
                    task.get("source_group_name"), int(bool(task.get("requires_attachment"))),
                    task.get("attachment_state", "none"), reminder_at,
                    float(task.get("confidence", 0.5)), stamp, stamp,
                ),
            )
            row = conn.execute("SELECT id FROM tasks WHERE fingerprint=?", (fingerprint,)).fetchone()
            task_id = int(row[0])
            for message_id in message_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO task_messages(task_id,message_id) VALUES(?,?)",
                    (task_id, int(message_id)),
                )
            self._schedule_notifications(conn, task_id, task, stamp)
        return task_id

    @staticmethod
    def _schedule_notifications(conn: sqlite3.Connection, task_id: int, task: dict[str, Any], stamp: str) -> None:
        title = task["title"].strip()
        reminder_at = task.get("reminder_at")
        if reminder_at:
            conn.execute(
                "INSERT OR IGNORE INTO notifications(task_id,kind,scheduled_at,text,created_at) VALUES(?,?,?,?,?)",
                (task_id, "due", reminder_at, f"待办提醒：{title}", stamp),
            )
        if task.get("requires_attachment") and task.get("attachment_state") == "missing":
            conn.execute(
                "INSERT OR IGNORE INTO notifications(task_id,kind,scheduled_at,text,created_at) VALUES(?,?,?,?,?)",
                (task_id, "missing_attachment", stamp, f"需要附件但尚未自动下载：{title}", stamp),
            )
        if task.get("requires_attachment") and task.get("attachment_state") == "unreadable":
            conn.execute(
                "INSERT OR IGNORE INTO notifications(task_id,kind,scheduled_at,text,created_at) VALUES(?,?,?,?,?)",
                (task_id, "unreadable_attachment", stamp, f"必要附件已下载但无法自动读取，请手动查看：{title}", stamp),
            )

    def list_tasks(self, status: str = "open") -> list[dict[str, Any]]:
        where = "WHERE t.status<>'merged'" if status == "all" else "WHERE t.status=?"
        params: tuple[Any, ...] = () if status == "all" else (status,)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT t.*,
                       (SELECT COUNT(*) FROM task_messages tm WHERE tm.task_id=t.id) AS evidence_count
                FROM tasks t {where}
                ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                         WHEN 'normal' THEN 2 ELSE 3 END,
                         t.due_at IS NULL,t.due_at,t.created_at DESC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def task_detail(self, task_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                return None
            messages = conn.execute(
                """
                SELECT m.id,m.sender_name,m.create_time,m.message_type,m.content,m.file_name,m.local_path
                FROM messages m JOIN task_messages tm ON tm.message_id=m.id
                WHERE tm.task_id=? ORDER BY m.create_time
                """,
                (task_id,),
            ).fetchall()
        result = dict(task)
        result["messages"] = [dict(row) for row in messages]
        return result

    def update_task(self, task_id: int, updates: dict[str, Any]) -> bool:
        allowed = {"title", "description", "action_text", "due_at", "priority", "status", "reminder_at"}
        clean = {key: value for key, value in updates.items() if key in allowed}
        if not clean:
            return False
        if clean.get("status") == "done":
            clean["completed_at"] = now_iso()
            allowed.add("completed_at")
        clean["updated_at"] = now_iso()
        fields = ",".join(f"{key}=?" for key in clean)
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE tasks SET {fields} WHERE id=?",
                (*clean.values(), int(task_id)),
            )
            if cur.rowcount and ({"title", "reminder_at"} & clean.keys()):
                task = conn.execute(
                    "SELECT title,reminder_at FROM tasks WHERE id=?", (int(task_id),)
                ).fetchone()
                conn.execute(
                    "DELETE FROM notifications WHERE task_id=? AND kind='due' AND delivered_at IS NULL",
                    (int(task_id),),
                )
                if task and task["reminder_at"]:
                    self._schedule_notifications(
                        conn,
                        int(task_id),
                        {"title": task["title"], "reminder_at": task["reminder_at"]},
                        now_iso(),
                    )
        return bool(cur.rowcount)

    def dashboard(self) -> dict[str, Any]:
        tasks = self.list_tasks("open")
        now = datetime.now().astimezone()
        today = now.date().isoformat()
        counts = {"open": len(tasks), "overdue": 0, "today": 0, "upcoming": 0, "missing": 0}
        for task in tasks:
            due = task.get("due_at")
            if task.get("attachment_state") in {"missing", "unreadable"}:
                counts["missing"] += 1
            if not due:
                continue
            due_day = str(due)[:10]
            if due_day < today:
                counts["overdue"] += 1
            elif due_day == today:
                counts["today"] += 1
            else:
                counts["upcoming"] += 1
        return {"counts": counts, "tasks": tasks, "groups": self.list_groups(), "generated_at": now_iso()}

    def qa_context(self) -> dict[str, Any]:
        with self.connection() as conn:
            recent = conn.execute(
                """
                SELECT g.name AS group_name,m.sender_name,m.create_time,m.message_type,m.content,
                       m.file_name,m.download_state,m.file_text,m.file_extract_state,m.file_extract_error
                FROM messages m JOIN groups g ON g.id=m.group_id
                WHERE g.selected=1 ORDER BY m.create_time DESC LIMIT 80
                """
            ).fetchall()
        return {"tasks": self.list_tasks("open")[:80], "recent_messages": [dict(r) for r in recent]}

    def claim_due_notifications(self, limit: int = 10) -> list[dict[str, Any]]:
        stamp = now_iso()
        stale = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat(timespec="seconds")
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT n.*,t.title,t.due_at,t.source_group_name FROM notifications n
                LEFT JOIN tasks t ON t.id=n.task_id
                WHERE n.delivered_at IS NULL AND n.scheduled_at<=?
                  AND (n.claimed_at IS NULL OR n.claimed_at<?)
                  AND (t.status IS NULL OR t.status='open')
                ORDER BY n.scheduled_at LIMIT ?
                """,
                (stamp, stale, int(limit)),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                conn.execute(
                    f"UPDATE notifications SET claimed_at=? WHERE id IN ({','.join('?' for _ in ids)})",
                    (stamp, *ids),
                )
        return [dict(row) for row in rows]

    def finish_notification(self, notification_id: int, delivered: bool, error: str | None = None) -> bool:
        with self.transaction() as conn:
            if delivered:
                cur = conn.execute(
                    "UPDATE notifications SET delivered_at=?,delivery_error=NULL WHERE id=?",
                    (now_iso(), int(notification_id)),
                )
            else:
                cur = conn.execute(
                    "UPDATE notifications SET claimed_at=NULL,delivery_error=? WHERE id=?",
                    ((error or "delivery failed")[:300], int(notification_id)),
                )
        return bool(cur.rowcount)

    def recent_notifications(self, after_id: int = 0) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE id>? AND scheduled_at<=? ORDER BY id LIMIT 50",
                (int(after_id), now_iso()),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_setting(self, key: str, value: Any) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), now_iso()),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default
