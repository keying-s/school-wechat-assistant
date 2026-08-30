from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from school_assistant.database import Store
from school_assistant.pipeline import Pipeline


class _FakeAI:
    def configured(self):
        return True

    def extract_tasks(self, messages, candidates):
        return [{
            "title": "通识课程选课提醒",
            "description": "再次提醒尽快完成选课",
            "action_text": "进入系统完成选课",
            "due_at": "2026-09-03T18:00:00+08:00",
            "priority": "high",
            "confidence": 0.95,
            "duplicate_task_id": candidates[0]["task_id"],
            "related_message_ids": [messages[0]["id"]],
        }]


class _CleanupAI:
    def __init__(self):
        self.calls = 0

    def configured(self):
        return True

    def find_duplicate_task_groups(self, candidates):
        self.calls += 1
        return [{
            "keep_task_id": candidates[0]["task_id"],
            "merge_task_ids": [candidates[1]["task_id"]],
            "reason": "同一讲堂选课的重复提醒",
        }]


class PipelineDedupTests(unittest.TestCase):
    def test_conflicting_duplicate_claims_keep_only_best_title_match(self):
        extracted = [
            {
                "title": "参加新生专题报告2",
                "due_at": "2026-08-31T10:30:00+08:00",
                "duplicate_task_id": 12,
                "related_message_ids": [284],
            },
            {
                "title": "参加安全通识培训",
                "due_at": "2026-08-31T17:20:00+08:00",
                "duplicate_task_id": 12,
                "related_message_ids": [284],
            },
        ]
        candidates = [{
            "task_id": 12,
            "title": "参加新生安全通识培训",
            "due_at": "2026-08-31T17:20:00+08:00",
        }]
        messages = [{
            "id": 284,
            "content": "明日安排：上午新生专题报告2；下午安全通识培训。",
        }]

        result = Pipeline._validate_duplicate_assignments(extracted, candidates, messages)

        self.assertIsNone(result[0]["duplicate_task_id"])
        self.assertEqual(result[1]["duplicate_task_id"], 12)

    def test_different_day_is_not_merged_without_correction_notice(self):
        extracted = [{
            "title": "参加新生专题报告2",
            "due_at": "2026-08-31T09:00:00+08:00",
            "duplicate_task_id": 11,
            "related_message_ids": [284],
        }]
        candidates = [{
            "task_id": 11,
            "title": "参加新生专题报告",
            "due_at": "2026-09-04T08:30:00+08:00",
        }]
        messages = [{"id": 284, "content": "明日9:00参加新生专题报告2。"}]

        result = Pipeline._validate_duplicate_assignments(extracted, candidates, messages)

        self.assertIsNone(result[0]["duplicate_task_id"])

    def test_explicit_reschedule_can_merge_across_dates(self):
        extracted = [{
            "title": "参加新生专题报告3",
            "due_at": "2026-09-04T08:30:00+08:00",
            "duplicate_task_id": 11,
            "related_message_ids": [300],
        }]
        candidates = [{
            "task_id": 11,
            "title": "参加新生专题报告3",
            "due_at": "2026-09-01T08:30:00+08:00",
        }]
        messages = [{"id": 300, "content": "原定9月1日的报告3调整为9月4日。"}]

        result = Pipeline._validate_duplicate_assignments(extracted, candidates, messages)

        self.assertEqual(result[0]["duplicate_task_id"], 11)

    def test_model_duplicate_id_uses_merge_path(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            store.upsert_groups([{"id": "g@chatroom", "name": "教务群"}])
            store.select_group("g@chatroom", True)
            now = int(datetime.now().timestamp())
            first_id = store.insert_messages("g@chatroom", [{
                "local_id": 1,
                "create_time": now,
                "message_type": "text",
                "content": "通识课程开放选课",
            }])[0]
            task_id = store.upsert_task({
                "title": "完成通识课程选课",
                "description": "通识课程开放选课",
                "source_group_id": "g@chatroom",
                "source_group_name": "教务群",
            }, [first_id])
            store.mark_messages([first_id], "processed")
            second_id = store.insert_messages("g@chatroom", [{
                "local_id": 2,
                "create_time": now + 60,
                "message_type": "text",
                "content": "再次提醒：通识课程选课即将截止",
            }])[0]

            pipeline = Pipeline(store, reader=object(), ai=_FakeAI())
            self.assertEqual(pipeline._process_ai(), 0)
            tasks = store.list_tasks("open")
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["id"], task_id)
            self.assertEqual(tasks[0]["evidence_count"], 2)
            self.assertEqual(pipeline.status()["last_merged_tasks"], 1)
            self.assertEqual(store.pending_messages(), [])
            self.assertIn(second_id, {message["id"] for message in store.task_detail(task_id)["messages"]})

    def test_existing_task_cleanup_runs_once_per_task_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            store.upsert_groups([{"id": "g@chatroom", "name": "教务群"}])
            first = store.upsert_task({
                "title": "完成通识课程选课",
                "description": "现已开放选课",
                "source_group_id": "g@chatroom",
            })
            second = store.upsert_task({
                "title": "通识课程选课提醒",
                "description": "请尽快选课",
                "source_group_id": "g@chatroom",
            })
            ai = _CleanupAI()
            pipeline = Pipeline(store, reader=object(), ai=ai)

            self.assertEqual(pipeline._cleanup_existing_duplicates(), 1)
            self.assertEqual(pipeline._cleanup_existing_duplicates(), 0)
            self.assertEqual(ai.calls, 1)
            self.assertEqual(len(store.list_tasks("open")), 1)
            with store.connection() as conn:
                merged = conn.execute(
                    "SELECT merged_into_task_id FROM tasks WHERE id=?", (second,)
                ).fetchone()[0]
            self.assertEqual(merged, first)


if __name__ == "__main__":
    unittest.main()
