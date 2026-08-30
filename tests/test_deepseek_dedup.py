from __future__ import annotations

import unittest
from unittest.mock import patch

from school_assistant.deepseek_client import DeepSeekClient


class DeepSeekDedupTests(unittest.TestCase):
    def test_existing_tasks_are_sent_for_ai_semantic_dedup(self):
        client = DeepSeekClient()
        response = (
            '{"tasks":[{"title":"完成通识课程选课","duplicate_task_id":42,'
            '"related_message_ids":[7],"confidence":0.95}]}'
        )
        messages = [{
            "id": 7,
            "create_time": 1787904000,
            "group_name": "教务通知群",
            "sender_name": "老师",
            "message_type": "text",
            "content": "再次提醒：通识课程选课即将截止",
        }]
        candidates = [{
            "task_id": 42,
            "title": "完成通识课程选课",
            "description": "通识课程开放选课",
            "status": "open",
            "source_group_name": "教务通知群",
        }]
        with patch.object(client, "_chat", return_value=response) as chat:
            tasks = client.extract_tasks(messages, candidates)

        self.assertEqual(tasks[0]["duplicate_task_id"], 42)
        prompt = chat.call_args.args[0][1]["content"]
        self.assertIn('"task_id": 42', prompt)
        self.assertIn("通识课程选课即将截止", prompt)

    def test_prompt_requires_independent_schedule_items_to_be_split(self):
        client = DeepSeekClient()
        response = (
            '{"tasks":['
            '{"title":"参加新生专题报告2","due_at":"2026-08-31T09:00:00+08:00",'
            '"duplicate_task_id":null,"related_message_ids":[284]},'
            '{"title":"完成安全通识培训","due_at":"2026-08-31T16:00:00+08:00",'
            '"duplicate_task_id":null,"related_message_ids":[284]}]}'
        )
        messages = [{
            "id": 284,
            "create_time": 1788077930,
            "group_name": "新生群",
            "sender_name": "老师",
            "message_type": "text",
            "content": (
                "明日安排：1. 9:00-10:30新生专题报告2（必修），线下签到；"
                "2. 16:00-17:20安全通识培训（线上），必须完成。"
            ),
        }]
        with patch.object(client, "_chat", return_value=response) as chat:
            tasks = client.extract_tasks(messages, [])

        self.assertEqual(len(tasks), 2)
        system_prompt = chat.call_args.args[0][0]["content"]
        self.assertIn("一条消息可以且经常需要生成多个任务", system_prompt)
        self.assertIn("上午线下报告和下午线上培训", system_prompt)
        self.assertIn("活动、课程、会议的 due_at 必须填开始时间", system_prompt)

    def test_cleanup_prompt_forbids_merging_distinct_sessions(self):
        client = DeepSeekClient()
        candidates = [
            {"task_id": 11, "title": "参加新生专题报告2", "due_at": "2026-08-31T09:00:00+08:00"},
            {"task_id": 12, "title": "参加安全通识培训", "due_at": "2026-08-31T16:00:00+08:00"},
        ]
        with patch.object(client, "_chat", return_value='{ "duplicate_groups": [] }') as chat:
            groups = client.find_duplicate_task_groups(candidates)

        self.assertEqual(groups, [])
        system_prompt = chat.call_args.args[0][0]["content"]
        self.assertIn("上午线下报告与下午线上培训", system_prompt)
        self.assertIn("系列汇总事项不能吸收", system_prompt)

    def test_existing_duplicate_groups_are_parsed(self):
        client = DeepSeekClient()
        candidates = [
            {"task_id": 1, "title": "完成通识课程选课", "status": "open"},
            {"task_id": 2, "title": "通识课程选课提醒", "status": "open"},
        ]
        response = (
            '{"duplicate_groups":[{"keep_task_id":1,"merge_task_ids":[2],'
            '"reason":"同一选课重复提醒"}]}'
        )
        with patch.object(client, "_chat", return_value=response):
            groups = client.find_duplicate_task_groups(candidates)
        self.assertEqual(groups[0]["keep_task_id"], 1)
        self.assertEqual(groups[0]["merge_task_ids"], [2])


if __name__ == "__main__":
    unittest.main()
