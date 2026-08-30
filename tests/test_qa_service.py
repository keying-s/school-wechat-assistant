from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from school_assistant.database import Store
from school_assistant.qa_service import SchoolQAService


class _FakeAI:
    def __init__(self):
        self.histories = []

    def plan_query(self, question, history):
        self.histories.append(list(history))
        prefix = "名师讲堂" if history else ""
        return {
            "standalone_question": prefix + question,
            "search_terms": ["名师讲堂"],
            "time_scope": None,
        }

    def answer_with_sources(self, question, standalone_question, history, retrieval):
        return f"回答：{standalone_question} [S1]"


class _FakeRetrieval:
    def search(self, question, *, search_terms=(), top_k=14):
        return {
            "sources": [{
                "ref": "S1",
                "kind": "attachment",
                "group": "新生群",
                "time": "2026-08-28T12:00+08:00",
                "title": "名师讲堂.docx",
                "content": "课程1",
            }],
            "neighbor_messages": [],
            "index": {"chunks": 1},
        }


class QAServiceTests(unittest.TestCase):
    def test_follow_up_receives_previous_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "qa.sqlite3")
            ai = _FakeAI()
            qa = SchoolQAService(store, ai, _FakeRetrieval())
            qa.ask("student", "有哪些课程？")
            result = qa.ask("student", "1和3什么时候？")
            self.assertEqual(len(ai.histories[1]), 2)
            self.assertIn("有哪些课程", ai.histories[1][0]["content"])
            self.assertIn("名师讲堂", result["standalone_question"])

    def test_reset_clears_only_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "qa.sqlite3")
            qa = SchoolQAService(store, _FakeAI(), _FakeRetrieval())
            qa.ask("student", "有哪些课程？")
            result = qa.ask("student", "清除上下文")
            self.assertTrue(result["cleared"])
            self.assertEqual(store.qa_history("student"), [])


if __name__ == "__main__":
    unittest.main()
