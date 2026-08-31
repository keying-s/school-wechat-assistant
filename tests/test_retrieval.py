from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from school_assistant.database import Store
from school_assistant.retrieval import RetrievalIndex, split_text


class _FakeEmbeddingModel:
    @staticmethod
    def _vector(text: str) -> np.ndarray:
        if "名师讲堂" in text:
            return np.array([1.0, 0.0, 0.0], dtype=np.float32)
        return np.array([0.0, 1.0, 0.0], dtype=np.float32)

    def embed(self, texts, **_kwargs):
        return (self._vector(text) for text in texts)

    def query_embed(self, query, **_kwargs):
        return iter([self._vector(str(query))])


class _TestIndex(RetrievalIndex):
    def _load_model(self):
        if self._model is None:
            self._model = _FakeEmbeddingModel()
            self._status["model_loaded"] = True
        return self._model


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "rag.sqlite3")
        self.store.upsert_groups([{"id": "g@chatroom", "name": "新生群"}])
        self.store.select_group("g@chatroom", True)

    def tearDown(self):
        self.temp.cleanup()

    def test_split_text_keeps_overlap_and_all_content(self):
        text = "第一段。" + ("课程说明" * 150) + "最后一段。"
        chunks = split_text(text, size=320, overlap=60)
        self.assertGreater(len(chunks), 2)
        self.assertIn("第一段", chunks[0])
        self.assertIn("最后一段", chunks[-1])

    def test_completed_task_and_old_attachment_are_retrievable(self):
        message_id = self.store.insert_messages("g@chatroom", [{
            "local_id": 1,
            "create_time": 1_787_900_000,
            "message_type": "file",
            "content": "文件：名师讲堂环节介绍.docx",
            "file_name": "名师讲堂环节介绍.docx",
            "local_path": "C:/downloaded/name.docx",
            "download_state": "available",
        }])[0]
        self.store.finish_file_extraction(
            message_id,
            "extracted",
            "名师讲堂。课程1时间：8月31日14:00。课程3时间：9月1日15:30。",
        )
        task_id = self.store.upsert_task({
            "title": "名师讲堂选课",
            "description": "选择两门课程",
            "action_text": "在系统选课",
            "status": "open",
        }, [message_id])
        self.store.update_task(task_id, {"status": "done"})

        result = _TestIndex(self.store).search(
            "名师讲堂课程1和课程3是什么时候",
            search_terms=["名师讲堂", "课程1", "课程3"],
        )
        self.assertTrue(result["sources"])
        self.assertEqual(result["sources"][0]["title"], "名师讲堂环节介绍.docx")
        self.assertTrue(any(item["kind"] == "task" for item in result["sources"]))
        self.assertTrue(any("8月31日14:00" in item["content"] for item in result["sources"]))

    def test_exact_date_tasks_are_pinned_ahead_of_older_plan_attachments(self):
        message_id = self.store.insert_messages("g@chatroom", [{
            "local_id": 2,
            "create_time": 1_787_900_000,
            "message_type": "file",
            "content": "旧版活动方案",
            "file_name": "旧版活动方案.docx",
            "local_path": "C:/downloaded/old.docx",
            "download_state": "available",
        }])[0]
        self.store.finish_file_extraction(
            message_id,
            "extracted",
            "2026年8月31日安排：上午报告地点为旧会场，下午未列出活动。",
        )
        morning_id = self.store.upsert_task({
            "title": "参加上午专题报告",
            "description": "8月31日9:00在新会场参加专题报告。",
            "action_text": "9:00前到新会场签到",
            "due_at": "2026-08-31T09:00:00+08:00",
            "source_group_id": "g@chatroom",
            "source_group_name": "新生群",
        })
        afternoon_id = self.store.upsert_task({
            "title": "参加下午安全培训",
            "description": "8月31日16:00线上参加，必须完成。",
            "action_text": "16:00上线参加培训",
            "due_at": "2026-08-31T16:00:00+08:00",
            "source_group_id": "g@chatroom",
            "source_group_name": "新生群",
        })

        result = _TestIndex(self.store).search(
            "2026年8月31日有什么安排",
            search_terms=["8月31日", "安排"],
            time_scope="2026-08-31",
            top_k=2,
        )

        self.assertEqual(
            [item["source_id"] for item in result["sources"][:2]],
            [str(morning_id), str(afternoon_id)],
        )
        self.assertTrue(all(item["kind"] == "task" for item in result["sources"][:2]))
        self.assertTrue(all(item["metadata"]["time_scope_match"] for item in result["sources"][:2]))
        self.assertIn("新会场", result["sources"][0]["content"])
        self.assertIn("线上参加", result["sources"][1]["content"])

    def test_history_is_isolated_by_user_and_can_be_cleared(self):
        self.store.append_qa_exchange("user-a", "名师讲堂？", "有十门课。")
        self.store.append_qa_exchange("user-b", "培养计划？", "请查看附件。")
        history = self.store.qa_history("user-a")
        self.assertEqual([row["content"] for row in history], ["名师讲堂？", "有十门课。"])
        self.store.clear_qa_history("user-a")
        self.assertEqual(self.store.qa_history("user-a"), [])
        self.assertEqual(len(self.store.qa_history("user-b")), 2)


if __name__ == "__main__":
    unittest.main()
