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
    def __init__(self):
        self.last_time_scope = None
        self.last_question = None
        self.last_search_terms = []

    def search(self, question, *, search_terms=(), time_scope=None, top_k=14):
        self.last_time_scope = time_scope
        self.last_question = question
        self.last_search_terms = list(search_terms)
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

    def test_time_scope_is_forwarded_to_retrieval(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "qa.sqlite3")
            ai = _FakeAI()
            retrieval = _FakeRetrieval()
            original = ai.plan_query

            def dated_plan(question, history):
                plan = original(question, history)
                plan["time_scope"] = "2026-08-31"
                return plan

            ai.plan_query = dated_plan
            qa = SchoolQAService(store, ai, retrieval)
            qa.ask("student", "今天有什么安排？")
            self.assertEqual(retrieval.last_time_scope, "2026-08-31")

    def test_date_query_includes_explicit_personal_course_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "qa.sqlite3")
            store.append_qa_exchange(
                "student",
                "通识课程什么时候？我选的是 1  3",
                "课程1在8月31日，课程3在9月1日。",
            )
            ai = _FakeAI()
            retrieval = _FakeRetrieval()
            original = ai.plan_query

            def dated_plan(question, history):
                plan = original(question, history)
                plan["time_scope"] = "2026-08-31"
                return plan

            ai.plan_query = dated_plan
            qa = SchoolQAService(store, ai, retrieval)
            qa.ask("student", "今天有什么安排？")

            self.assertIn("用户此前明确的个人安排", retrieval.last_question)
            self.assertIn("通识课程", retrieval.last_search_terms)
            self.assertIn("课程1", retrieval.last_search_terms)
            self.assertIn("课程3", retrieval.last_search_terms)

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
