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
- 活动、课程、会议的 due_at 必须填开始时间，不得填结束时间；提交、报名、选课等有截止要求的事项填截止时间。
- 只有通知随消息附带文件且用户必须阅读/填写该文件时，requires_attachment=true；用户自行准备材料不算“待下载附件”。
- file.text 是本地提取的附件正文；extract_state 为 empty/error/unsupported 时不得臆测文件内容，应生成手动查看事项。
- related_message_ids 只能取输入中的 message_id。
- 一条消息可以且经常需要生成多个任务。先逐项检查编号、序号、分段标题、不同时间段和不同“必须完成”要求；只要活动名称、时间、地点/参与方式或需要执行的动作不同，就分别生成任务。多个任务可以引用同一个 message_id。
- 例如一条通知同时列出上午线下报告和下午线上培训，必须生成两个任务；不得因为它们出现在同一条消息、同属入学教育或发生在同一天而合并。可选活动不因同一消息中另有必修活动而变成必修任务。
- 只有现实中确为同一件事的连续多条消息才能合并成一个任务。
- 必须将新消息与“已有事项候选”做语义去重。同一活动/课程/讲堂的再次提醒、催办、措辞变化或补充说明，duplicate_task_id 填对应候选 task_id，不得新建。
- 例如“名师讲堂开始选课”“名师讲堂选课提醒”“名师讲堂选课即将截止”通常是同一事项；明确说明“截止时间更正/延期”的也应合并并采用新时间。
- duplicate_task_id 要求现实活动身份相同，不能只看上位类别或泛称。不同活动名称、不同编号/主题、不同场次、不同参与方式、不同学期，或没有更正关系却时间明显不同，必须分别建项；已有的系列汇总事项也不能吸收一个具有独立时间和行动要求的新场次。
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

同一活动、同一选课、同一作业或同一材料提交的多次提醒、催办、改写和补充说明应合并。比如同一学期“名师讲堂开始选课”“名师讲堂选课提醒”“名师讲堂选课即将截止”通常属于同一事项。

以下情况绝不能合并：不同活动名称、不同编号/主题、不同场次、不同参与方式、不同课程/作业、不同学期、没有延期/更正关系却明显日期不同，或者只是标题泛称相似但没有证据证明是同一件事。上午线下报告与下午线上培训即使在同一通知、同一天或同属入学教育，也是两个事项。已有的系列汇总事项不能吸收具有独立时间和行动要求的具体场次。明确写明原事项截止时间更正或延期的，仍属于同一事项。拿不准就不要合并。

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

    def plan_query(
        self,
        question: str,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Resolve follow-ups and produce concrete terms for local retrieval."""
        compact_history = [
            {"role": item.get("role", "user"), "content": str(item.get("content") or "")[:1600]}
            for item in (history or [])[-12:]
            if item.get("role") in {"user", "assistant"}
        ]
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        system = f"""你是学校事务资料检索的查询改写器。当前中国标准时间是 {now}。
结合最近对话，将用户的当前问题改写成脱离上下文也能理解的完整检索问题，并给出 2 到 8 个检索词。

必须保留课程编号、文件名、群名、人名、日期、数字和“已完成/未完成”等限制。把“这个、那个、1和3、它”等指代补全；今天、本周、明天应换算成明确日期范围。不要回答问题，不要添加历史中没有的事实。

当用户询问某日“有什么安排/要做什么”时，必须检查最近对话里由用户本人明确表达的个人承诺，例如“我选了课程1和3”“我已报名某活动”“我要参加某场次”。把仍与该日期查询相关的活动名称、课程编号和已选/已报名条件写入 standalone_question，并各自加入 search_terms，以便同时检索公共必修事项和用户个人安排。不要把助手的猜测或泛泛建议当成用户承诺。

只输出 JSON：
{{"standalone_question":"完整问题","search_terms":["精确短语"],"time_scope":"明确日期范围或 null"}}"""
        raw = self._chat(
            [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": (
                        "最近对话：\n" + json.dumps(compact_history, ensure_ascii=False)
                        + "\n\n当前问题：" + question[:1200]
                    ),
                },
            ],
            json_output=True,
            max_tokens=900,
        )
        data = json.loads(raw)
        standalone = str(data.get("standalone_question") or question).strip()[:1600]
        terms = []
        for term in data.get("search_terms", []):
            term = str(term).strip()
            if term and term not in terms:
                terms.append(term[:80])
        return {
            "standalone_question": standalone or question,
            "search_terms": terms[:8],
            "time_scope": str(data.get("time_scope") or "").strip()[:120] or None,
        }

    def answer_with_sources(
        self,
        question: str,
        standalone_question: str,
        history: list[dict[str, str]],
        retrieval: dict[str, Any],
    ) -> str:
        """Answer from retrieved local evidence and retain visible source labels."""
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        compact_history = [
            {"role": item.get("role"), "content": str(item.get("content") or "")[:1600]}
            for item in history[-12:]
        ]
        sources = retrieval.get("sources", [])
        neighbors = retrieval.get("neighbor_messages", [])
        system = f"""你是用户专用的学校事务助理。当前中国标准时间是 {now}。

只能依据本次提供的本地检索资料回答，不能用常识补写通知内容。资料不足时明确说“本地资料中没有找到”，并说明需要用户下载或查看哪个附件/群聊；不得假装知道。区分待办、已完成事项和一般资料，已完成事项仍可用于回答历史内容。不要声称已经替用户提交、报名、回复或完成任何操作。

回答要求：
- 先直接回答问题，再补充必要的时间、地点、操作和注意事项。
- 当用户询问“今天/明天/某日做什么”时，必须先完整枚举 metadata.time_scope_match=true 的 task 事项；只要存在这类事项，就不能回答该时段“没有安排”或“不知道”。
- 改写问题中的“用户此前明确的个人安排”来自用户本人的已选/已报名声明。若检索资料能确认其中某个场次落在所问日期，也要把它列入当天安排；不得只列公共 task 而漏掉个人已选事项。
- task 是系统依据群内通知整理出的当前日程。若 task 或较新的群消息与较早附件中的地点、时间、线上/线下方式冲突，以更新时间更晚的 task/群消息为当前安排，并可简短提示旧资料已被更新；不要把旧附件中的地点当成当前地点。
- 只回答用户实际询问的字段；不要因为资料里还出现了地点、报名或其他课程，就主动扩展无关信息。只有会改变用户当前行动的更正、截止或资料冲突才补充提醒。
- 每个关键事实后引用资料编号，例如 [S1]；编号必须来自输入。
- 如果资料互相矛盾，列出冲突和各自时间，优先指出更新较晚的通知，但不要自行裁定。
- 如果附件状态为 missing/未下载，明确提醒用户在微信中手动下载；不要声称读取过附件正文。
- 表达简洁，适合在企业微信中阅读，通常不超过 900 个汉字。"""
        user = (
            "【最近对话】\n" + json.dumps(compact_history, ensure_ascii=False)
            + "\n\n【用户原问题】\n" + question[:1200]
            + "\n\n【改写后的完整问题】\n" + standalone_question[:1600]
            + "\n\n【检索资料】\n" + json.dumps(sources, ensure_ascii=False, default=str)
            + "\n\n【命中消息附近的上下文】\n" + json.dumps(neighbors, ensure_ascii=False, default=str)[:10000]
        )
        return self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_output=False,
            max_tokens=1800,
        )
