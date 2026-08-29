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
