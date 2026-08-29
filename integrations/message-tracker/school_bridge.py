"""Route explicit school questions to the local school-assistant service.

This module never sends proactive messages and never loads the school API key.
"""

from __future__ import annotations

import re

import requests

import config
import llm
from logger import logger


_SCHOOL_PREFIX = re.compile(
    r"^\s*(?:/school\b|school\b|学校事务|学校|校务|校园)\s*[:：]?\s*",
    re.I,
)
_TRACKER_PREFIX = re.compile(
    r"^\s*(?:/tracker\b|tracker\b|信息追踪|资讯追踪|追踪)\s*[:：]?\s*",
    re.I,
)
_ROUTE_SYSTEM = """你是两个本地问答系统的意图路由器。判断用户问题应该交给哪个系统：
- SCHOOL：用户自己的学校微信群、课程、作业、考试、签到、选课、辅导员通知、提交材料、截止日期、附件、日程和个人待办。
- TRACKER：宿主项目原有的信息问答、股票、产业链、新闻、资讯编号和资讯汇总；普通闲聊或确实无法判断时也选它。

只允许输出一个英文单词：SCHOOL 或 TRACKER。不要解释。"""


def classify_query(text: str) -> str:
    """Use explicit user namespaces, otherwise ask DeepSeek to choose a system."""
    value = (text or "").strip()
    if _TRACKER_PREFIX.match(value):
        return "tracker"
    if _SCHOOL_PREFIX.match(value):
        return "school"
    try:
        result = llm.chat(
            [{"role": "user", "content": value[:1000]}],
            system=_ROUTE_SYSTEM,
            max_tokens=20,
            timeout=12,
            retries=1,
            thinking=False,
        ).strip().upper()
        decision = re.search(r"\b(SCHOOL|TRACKER)\b", result)
        route = decision.group(1).lower() if decision else "tracker"
        logger.info("[SchoolBridge] AI 总路由=%s q=%s", route, value[:40])
        return route
    except Exception as exc:
        logger.warning("[SchoolBridge] AI 总路由失败，回退宿主问答: %s", exc)
        return "tracker"


def is_school_query(text: str) -> bool:
    return classify_query(text) == "school"


def clean_tracker_question(text: str) -> str:
    cleaned = _TRACKER_PREFIX.sub("", text or "").strip()
    return cleaned or (text or "")


def _url(path: str) -> str:
    return config.SCHOOL_ASSISTANT_URL.rstrip("/") + path


def ask(question: str) -> str:
    cleaned = _SCHOOL_PREFIX.sub("", question or "").strip()
    try:
        response = requests.post(
            _url("/api/qa"),
            json={"question": cleaned or question},
            timeout=config.SCHOOL_ASSISTANT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("ok") and data.get("answer"):
            return str(data["answer"])
        return "学校事务服务暂时没有生成回答，请先打开日程网站选择要关注的群聊。"
    except Exception as exc:
        logger.warning("[SchoolBridge] 本机问答服务不可用: %s", exc)
        return "学校事务服务暂时未连接。请稍后再试，或打开 http://127.0.0.1:8765 查看状态。"
