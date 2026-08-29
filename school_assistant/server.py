"""Local HTTP API and static web server."""

from __future__ import annotations

import json
import logging
import mimetypes
import posixpath
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import config
from .database import Store
from .deepseek_client import DeepSeekClient
from .pipeline import Pipeline


logger = logging.getLogger("school_assistant.http")


class AppContext:
    def __init__(self, store: Store, pipeline: Pipeline, ai: DeepSeekClient):
        self.store = store
        self.pipeline = pipeline
        self.ai = ai


def _manual_reminder(due_at: str | None, lead_minutes: int) -> str | None:
    if not due_at:
        return None
    try:
        if len(due_at) == 10:
            due = datetime.fromisoformat(due_at).astimezone().replace(hour=18, minute=0)
        else:
            due = datetime.fromisoformat(due_at.replace("Z", "+00:00")).astimezone()
        reminder = due - timedelta(minutes=max(0, min(10080, int(lead_minutes))))
        return max(reminder, datetime.now().astimezone()).isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return None


def make_handler(context: AppContext):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SchoolAssistant/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("%s - %s", self.address_string(), fmt % args)

        def _json(self, data: Any, status: int = 200) -> None:
            payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: int, message: str) -> None:
            self._json({"ok": False, "error": message}, status)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > 2 * 1024 * 1024:
                raise ValueError("请求体过大")
            if not length:
                return {}
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON 请求体必须是对象")
            return payload

        def _static(self, path: str) -> None:
            relative = posixpath.normpath(unquote(path)).lstrip("/") or "index.html"
            if relative.startswith("api/") or ".." in Path(relative).parts:
                self._error(404, "not found")
                return
            target = (config.STATIC_DIR / relative).resolve()
            try:
                target.relative_to(config.STATIC_DIR.resolve())
            except ValueError:
                self._error(404, "not found")
                return
            if not target.is_file():
                target = config.STATIC_DIR / "index.html"
            content = target.read_bytes()
            media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", media_type + ("; charset=utf-8" if media_type.startswith("text/") else ""))
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                if path == "/api/status":
                    self._json({
                        "ok": True,
                        "service": context.pipeline.status(),
                        "deepseek": {
                            "configured": context.ai.configured(),
                            "model": context.ai.model,
                        },
                    })
                elif path == "/api/dashboard":
                    self._json({"ok": True, **context.store.dashboard(), "service": context.pipeline.status()})
                elif path == "/api/groups":
                    self._json({"ok": True, "groups": context.store.list_groups()})
                elif path == "/api/tasks":
                    status = query.get("status", ["open"])[0]
                    self._json({"ok": True, "tasks": context.store.list_tasks(status)})
                elif path.startswith("/api/tasks/"):
                    task_id = int(path.rsplit("/", 1)[1])
                    task = context.store.task_detail(task_id)
                    self._json({"ok": bool(task), "task": task}, 200 if task else 404)
                elif path == "/api/notifications":
                    after = int(query.get("after", ["0"])[0])
                    self._json({"ok": True, "notifications": context.store.recent_notifications(after)})
                else:
                    self._static(path)
            except Exception as exc:
                logger.exception("GET %s failed", path)
                self._error(500, f"{type(exc).__name__}: {exc}")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                body = self._body()
                if path.startswith("/api/groups/") and path.endswith("/select"):
                    group_id = unquote(path[len("/api/groups/"):-len("/select")].strip("/"))
                    ok = context.store.select_group(
                        group_id, bool(body.get("selected")), body.get("lookback_days")
                    )
                    if ok:
                        context.pipeline.request_sync()
                    self._json({"ok": ok}, 200 if ok else 404)
                elif path == "/api/sync":
                    context.pipeline.request_sync()
                    self._json({"ok": True, "message": "已请求后台同步"}, 202)
                elif path == "/api/tasks":
                    title = str(body.get("title", "")).strip()
                    if not title:
                        self._error(400, "title 不能为空")
                        return
                    due_at = str(body.get("due_at") or "").strip() or None
                    task = {
                        "title": title[:180],
                        "description": str(body.get("description", ""))[:4000],
                        "action_text": str(body.get("action_text", ""))[:1000],
                        "due_at": due_at,
                        "all_day": bool(body.get("all_day", len(due_at or "") == 10)),
                        "priority": body.get("priority") if body.get("priority") in {"urgent", "high", "normal", "low"} else "normal",
                        "status": "open",
                        "requires_attachment": False,
                        "attachment_state": "none",
                        "reminder_at": _manual_reminder(due_at, int(body.get("reminder_lead_minutes", 1440))),
                        "confidence": 1.0,
                    }
                    task_id = context.store.upsert_task(task)
                    self._json({"ok": True, "task_id": task_id}, 201)
                elif path == "/api/qa":
                    question = str(body.get("question", "")).strip()
                    if not question:
                        self._error(400, "question 不能为空")
                        return
                    answer = context.ai.answer(question, context.store.qa_context())
                    self._json({"ok": True, "answer": answer})
                elif path == "/api/internal/reminders/claim":
                    reminders = context.store.claim_due_notifications(int(body.get("limit", 10)))
                    self._json({"ok": True, "reminders": reminders})
                elif path.startswith("/api/internal/reminders/") and path.endswith("/finish"):
                    notification_id = int(path.split("/")[-2])
                    ok = context.store.finish_notification(
                        notification_id, bool(body.get("delivered")), body.get("error")
                    )
                    self._json({"ok": ok}, 200 if ok else 404)
                elif path == "/api/deepseek/check":
                    self._json(context.ai.healthcheck())
                else:
                    self._error(404, "not found")
            except (ValueError, json.JSONDecodeError) as exc:
                self._error(400, str(exc))
            except Exception as exc:
                logger.exception("POST %s failed", path)
                self._error(500, f"{type(exc).__name__}: {exc}")

        def do_PATCH(self) -> None:
            path = urlparse(self.path).path
            try:
                if not path.startswith("/api/tasks/"):
                    self._error(404, "not found")
                    return
                task_id = int(path.rsplit("/", 1)[1])
                body = self._body()
                if "title" in body:
                    body["title"] = str(body["title"]).strip()[:180]
                    if not body["title"]:
                        self._error(400, "title 不能为空")
                        return
                if "description" in body:
                    body["description"] = str(body["description"])[:4000]
                if "priority" in body and body["priority"] not in {"urgent", "high", "normal", "low"}:
                    body["priority"] = "normal"
                if "due_at" in body:
                    body["due_at"] = str(body.get("due_at") or "").strip() or None
                    body["reminder_at"] = _manual_reminder(
                        body["due_at"], int(body.pop("reminder_lead_minutes", 1440))
                    )
                ok = context.store.update_task(task_id, body)
                self._json({"ok": ok}, 200 if ok else 404)
            except (ValueError, json.JSONDecodeError) as exc:
                self._error(400, str(exc))
            except Exception as exc:
                logger.exception("PATCH %s failed", path)
                self._error(500, f"{type(exc).__name__}: {exc}")

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Allow", "GET,POST,PATCH,OPTIONS")
            self.end_headers()

    return Handler


def create_server(context: AppContext, host: str = config.HOST, port: int = config.PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(context))
