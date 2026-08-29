"""DeepSeek API client for structured task extraction and school Q&A."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

import requests

from . import config


class DeepSeekClient:
    def __init__(self):
        self.api_key = config.DEEPSEEK_API_KEY
        self.base_url = config.DEEPSEEK_BASE_URL
        self.model = config.DEEPSEEK_MODEL

    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def healthcheck(self) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "error": "未配置 DeepSeek API key"}
        try:
            response = requests.get(
                f"{self.base_url}/models", headers=self._headers, timeout=20
            )
            if response.status_code >= 400:
                return {"ok": False, "error": f"DeepSeek HTTP {response.status_code}"}
            model_ids = [item.get("id") for item in response.json().get("data", [])]
            return {"ok": True, "model_available": self.model in model_ids, "model": self.model}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_output: bool,
        max_tokens: int,
    ) -> str:
        if not self.configured():
            raise RuntimeError("未配置 DeepSeek API key")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "thinking": {"type": "disabled"},
        }
        if json_output:
            body["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    timeout=config.DEEPSEEK_TIMEOUT,
                )
                if response.status_code in {408, 409, 429, 500, 502, 503, 504}:
                    raise RuntimeError(f"DeepSeek HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise ValueError(f"DeepSeek 请求被拒绝：HTTP {response.status_code}")
                payload = response.json()
                content = payload["choices"][0]["message"].get("content", "").strip()
                if not content:
                    raise RuntimeError("DeepSeek 返回空内容")
                return content
            except ValueError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError(str(last_error or "DeepSeek 调用失败"))

    def extract_tasks(
        self,
        messages: list[dict[str, Any]],
        existing_tasks: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        compact = []
        attachment_text_budget = 30_000
        for message in messages:
            file_text = str(message.get("file_text") or "")
            excerpt = file_text[: min(10_000, attachment_text_budget)]
            attachment_text_budget -= len(excerpt)
            compact.append({
                "message_id": message["id"],
                "time": datetime.fromtimestamp(message["create_time"]).astimezone().isoformat(timespec="seconds"),
                "group": message["group_name"],
                "sender": message.get("sender_name") or "",
                "type": message["message_type"],
                "content": (message.get("content") or "")[:2500],
                "file": {
                    "name": message.get("file_name"),
                    "size": message.get("file_size"),
                    "download_state": message.get("download_state"),
                    "extract_state": message.get("file_extract_state"),
                    "extract_error": message.get("file_extract_error"),
                    "text": excerpt,
                } if message.get("file_name") else None,
            })
        candidates = [
            {
                "task_id": task["task_id"],
                "title": str(task.get("title") or "")[:180],
                "description": str(task.get("description") or "")[:500],
                "action_text": str(task.get("action_text") or "")[:300],
                "due_at": task.get("due_at"),
                "status": task.get("status"),
                "source_group": task.get("source_group_name"),
                "evidence_count": task.get("evidence_count", 0),
            }
            for task in (existing_tasks or [])[:80]
        ]

        system = f"""你是大学生的校务通知整理助手。当前中国标准时间是 {now}。
分析群聊消息，只有当消息明确要求用户做事、参加活动、提交材料、签到、选课、缴费、下载并阅读必要附件，或包含需要记住的课程/会议/截止时间时，才生成事项。闲聊、表情、广告、重复转发、无行动要求的信息不要生成事项。

你必须只输出 JSON 对象，格式：
{{"tasks":[{{
  "title":"短标题",
  "description":"通知要点和要求",
  "action_text":"用户具体要做什么",
  "due_at":"带 +08:00 的 ISO 时间；只有日期则 YYYY-MM-DD；确实没有则 null",
  "all_day":false,
  "priority":"urgent|high|normal|low",
  "requires_attachment":false,
  "reminder_lead_minutes":1440,
  "confidence":0.0,
  "duplicate_task_id":"与已有事项相同则填候选 task_id，否则 null",
  "related_message_ids":[整数]
}}]}}

规则：
- “明天/下周/本周五”等必须以消息自己的 time 为基准换算。
- 不得编造截止时间；没有就填 null，但仍可进入待确认清单。
- 只有通知随消息附带文件且用户必须阅读/填写该文件时，requires_attachment=true；用户自行准备材料不算“待下载附件”。
- file.text 是本地提取的附件正文；extract_state 为 empty/error/unsupported 时不得臆测文件内容，应生成手动查看事项。
- related_message_ids 只能取输入中的 message_id。
- 同一件事连续多条消息合并成一个任务。
- 必须将新消息与“已有事项候选”做语义去重。同一活动/课程/讲堂的再次提醒、催办、措辞变化或补充说明，duplicate_task_id 填对应候选 task_id，不得新建。
- 例如“通识课程开始选课”“通识课程选课提醒”“通识课程选课即将截止”通常是同一事项；明确说明“截止时间更正/延期”的也应合并并采用新时间。不同课程主题、不同场次、不同学期，或没有更正关系却明显日期不同的才是新事项。
- duplicate_task_id 只能使用候选中真实存在的 task_id；不确定是否同一事项时填 null。
- JSON 中不要包含任何额外说明。"""
        user = (
            "【已有事项候选】\n" + json.dumps(candidates, ensure_ascii=False)
            + "\n\n【新群聊消息】\n" + json.dumps(compact, ensure_ascii=False)
            + "\n\n请提取事项、判断语义重复并输出 JSON。"
        )
        raw = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_output=True,
            max_tokens=5000,
        )
        data = json.loads(raw)
        tasks = data.get("tasks", [])
        return [task for task in tasks if isinstance(task, dict) and str(task.get("title", "")).strip()]

    def find_duplicate_task_groups(
        self, existing_tasks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Conservatively cluster existing rows that describe the same obligation."""
        compact = [
            {
                "task_id": task["task_id"],
                "title": str(task.get("title") or "")[:180],
                "description": str(task.get("description") or "")[:700],
                "action_text": str(task.get("action_text") or "")[:300],
                "due_at": task.get("due_at"),
                "status": task.get("status"),
                "source_group": task.get("source_group_name"),
                "evidence_count": task.get("evidence_count", 0),
            }
            for task in existing_tasks[:100]
        ]
        system = """你是校务待办的保守去重审核器。找出列表中描述同一个现实事项的重复记录。

同一活动、同一选课、同一作业或同一材料提交的多次提醒、催办、改写和补充说明应合并。比如同一学期“通识课程开始选课”“通识课程选课提醒”“通识课程选课即将截止”通常属于同一事项。

以下情况绝不能合并：不同讲座主题、不同场次、不同课程/作业、不同学期、没有延期/更正关系却明显日期不同，或者只是标题泛称相似但没有证据证明是同一件事。明确写明原事项截止时间更正或延期的，仍属于同一事项。拿不准就不要合并。

只输出 JSON：
{"duplicate_groups":[{"keep_task_id":整数,"merge_task_ids":[整数],"reason":"简短理由"}]}

要求：ID 必须来自输入；每个 ID 最多出现一次；同组至少两项；若同组有已完成项，优先保留已完成项，否则保留信息最完整的一项。没有重复则返回空数组。"""
        raw = self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": "请审核以下已有事项：\n" + json.dumps(compact, ensure_ascii=False)},
            ],
            json_output=True,
            max_tokens=2500,
        )
        data = json.loads(raw)
        groups = data.get("duplicate_groups", [])
        return [group for group in groups if isinstance(group, dict)]

    def answer(self, question: str, context: dict[str, Any]) -> str:
        system = """你是用户的学校事务助理。只能根据给出的本地事项和群消息回答；不确定时明确说不确定，并指出应查看哪个群或附件。回答简洁、可执行，优先列出时间最紧的事项。不要声称已经替用户完成、提交或回复任何事情。"""
        user = (
            "用户问题：" + question[:1000] + "\n\n"
            "本地资料：\n" + json.dumps(context, ensure_ascii=False, default=str)[:50000]
        )
        return self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_output=False,
            max_tokens=1800,
        )
