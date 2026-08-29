from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from school_assistant.database import Store, now_iso


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.store.upsert_groups([{"id": "123@chatroom", "name": "测试群"}])

    def tearDown(self):
        self.temp.cleanup()

    def test_group_selection_and_message_dedup(self):
        self.assertTrue(self.store.select_group("123@chatroom", True, 3))
        group = self.store.selected_groups()[0]
        self.assertGreater(group["cursor_time"], 0)
        message = {
            "local_id": 7,
            "create_time": int(datetime.now().timestamp()),
            "message_type": "text",
            "content": "请明天提交材料",
        }
        first = self.store.insert_messages("123@chatroom", [message])
        second = self.store.insert_messages("123@chatroom", [message])
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(self.store.pending_messages()), 1)

    def test_task_and_missing_attachment_notification(self):
        message_id = self.store.insert_messages("123@chatroom", [{
            "local_id": 9,
            "create_time": int(datetime.now().timestamp()),
            "message_type": "file",
            "content": "文件：通知.pdf",
            "file_name": "通知.pdf",
            "file_size": 100,
            "download_state": "missing",
        }])[0]
        task_id = self.store.upsert_task({
            "title": "阅读通知附件",
            "source_group_id": "123@chatroom",
            "source_group_name": "测试群",
            "requires_attachment": True,
            "attachment_state": "missing",
            "confidence": 0.9,
        }, [message_id])
        task = self.store.task_detail(task_id)
        self.assertEqual(task["attachment_state"], "missing")
        claimed = self.store.claim_due_notifications()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["kind"], "missing_attachment")
        self.assertTrue(self.store.finish_notification(claimed[0]["id"], True))

    def test_downloaded_but_unreadable_attachment_is_flagged(self):
        local_file = Path(self.temp.name) / "扫描件.pdf"
        local_file.write_bytes(b"placeholder")
        message_id = self.store.insert_messages("123@chatroom", [{
            "local_id": 10,
            "create_time": int(datetime.now().timestamp()),
            "message_type": "file",
            "content": "文件：扫描件.pdf",
            "file_name": "扫描件.pdf",
            "file_size": local_file.stat().st_size,
            "local_path": str(local_file),
            "download_state": "available",
        }])[0]
        task_id = self.store.upsert_task({
            "title": "阅读扫描通知",
            "source_group_id": "123@chatroom",
            "requires_attachment": True,
            "attachment_state": "checking",
        }, [message_id])

        pending = self.store.pending_file_extractions()
        self.assertEqual([row["id"] for row in pending], [message_id])
        self.store.finish_file_extraction(message_id, "empty", "", "未启用 OCR")
        self.assertEqual(self.store.task_detail(task_id)["attachment_state"], "unreadable")
        claimed = self.store.claim_due_notifications()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["kind"], "unreadable_attachment")

    def test_future_notification_is_not_shown_early(self):
        future = (datetime.now().astimezone() + timedelta(days=1)).isoformat(timespec="seconds")
        task_id = self.store.upsert_task({
            "title": "未来事项",
            "reminder_at": future,
            "confidence": 1,
        })
        self.assertEqual(self.store.recent_notifications(), [])

        revised = (datetime.now().astimezone() + timedelta(days=2)).isoformat(timespec="seconds")
        self.assertTrue(self.store.update_task(task_id, {"title": "改期事项", "reminder_at": revised}))
        with self.store.connection() as conn:
            pending = conn.execute(
                "SELECT scheduled_at,text FROM notifications WHERE task_id=? AND delivered_at IS NULL",
                (task_id,),
            ).fetchall()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["scheduled_at"], revised)
        self.assertIn("改期事项", pending[0]["text"])

    def test_ai_confirmed_duplicate_merges_evidence_instead_of_creating_task(self):
        now = int(datetime.now().timestamp())
        first_id = self.store.insert_messages("123@chatroom", [{
            "local_id": 20,
            "create_time": now,
            "message_type": "text",
            "content": "通识课程开始选课",
        }])[0]
        task_id = self.store.upsert_task({
            "title": "完成通识课程选课",
            "description": "通识课程现已开放选课",
            "action_text": "进入系统选课",
            "due_at": "2026-09-03T18:00:00+08:00",
            "priority": "normal",
            "source_group_id": "123@chatroom",
            "source_group_name": "测试群",
            "confidence": 0.8,
        }, [first_id])
        second_id = self.store.insert_messages("123@chatroom", [{
            "local_id": 21,
            "create_time": now + 60,
            "message_type": "text",
            "content": "再次提醒：通识课程选课即将截止",
        }])[0]

        candidates = self.store.dedup_candidates("123@chatroom")
        self.assertIn(task_id, {row["task_id"] for row in candidates})
        self.assertTrue(self.store.merge_ai_duplicate(task_id, {
            "description": "再次提醒：通识课程选课即将截止，请尽快完成",
            "action_text": "尽快进入系统完成选课",
            "due_at": "2026-09-03T18:00:00+08:00",
            "reminder_at": "2026-09-03T08:00:00+08:00",
            "priority": "high",
            "confidence": 0.95,
        }, [second_id]))

        tasks = self.store.list_tasks("open")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], task_id)
        self.assertEqual(tasks[0]["evidence_count"], 2)
        self.assertEqual(tasks[0]["priority"], "high")
        self.assertIn("最新重复通知", tasks[0]["description"])

    def test_existing_duplicate_rows_are_preserved_as_merged(self):
        now = int(datetime.now().timestamp())
        message_ids = self.store.insert_messages("123@chatroom", [
            {"local_id": 30, "create_time": now, "message_type": "text", "content": "通识课程开始选课"},
            {"local_id": 31, "create_time": now + 1, "message_type": "text", "content": "通识课程选课截止提醒"},
        ])
        keep_id = self.store.upsert_task({
            "title": "完成通识课程选课",
            "description": "通识课程开始选课",
            "due_at": "2026-09-03T18:00:00+08:00",
            "source_group_id": "123@chatroom",
        }, [message_ids[0]])
        duplicate_id = self.store.upsert_task({
            "title": "通识课程选课截止提醒",
            "description": "再次提醒尽快选课",
            "due_at": "2026-09-03T22:00:00+08:00",
            "source_group_id": "123@chatroom",
        }, [message_ids[1]])

        self.assertEqual(self.store.consolidate_duplicate_tasks(keep_id, [duplicate_id]), 1)
        self.assertEqual(len(self.store.list_tasks("open")), 1)
        self.assertEqual(
            self.store.task_detail(keep_id)["due_at"], "2026-09-03T18:00:00+08:00"
        )
        self.assertEqual(len(self.store.task_detail(keep_id)["messages"]), 2)
        with self.store.connection() as conn:
            duplicate = conn.execute(
                "SELECT status,merged_into_task_id FROM tasks WHERE id=?", (duplicate_id,)
            ).fetchone()
        self.assertEqual(duplicate["status"], "merged")
        self.assertEqual(duplicate["merged_into_task_id"], keep_id)


if __name__ == "__main__":
    unittest.main()
