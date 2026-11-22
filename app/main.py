"""REST API，纯标准库 http.server。业务逻辑集中在 Api.dispatch，便于单测。

GET  /health                     健康检查
POST /api/login                  登录（demo 固定 token，生产接 SSO）
GET  /api/devices                设备台账（按区域过滤）
POST /api/devices                新增设备（admin）
GET  /api/tickets?status=&page=  工单分页
POST /api/tickets/transition     工单状态流转
GET  /api/reviews                待复核队列
POST /api/reviews/resolve        复核确认/判误报
POST /api/frames                 上报一帧（触发检测编排）
POST /api/manual-report          人工报修（AI 降级时可用）
GET  /api/metrics                统计
GET  /api/feedback               误报样本导出（训练回流）
"""
from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .auth import area_filter, authenticate, can_see, require

log = logging.getLogger(__name__)


def _token(headers) -> str:
    for k, v in headers.items():
        if k.lower() == "x-token":
            return v or ""
    return ""


class Api:
    def __init__(self, repo, service):
        self.repo = repo
        self.service = service

    def _user(self, headers, perm):
        return require(authenticate(self.repo.conn, _token(headers)), perm)

    def dispatch(self, method, path, query, body, headers):
        if path == "/health":
            return 200, {"status": "ok"}

        if path == "/api/login" and method == "POST":
            row = self.repo.conn.execute(
                "SELECT username, role, area, token FROM user WHERE username = ?",
                (body.get("username", ""),),
            ).fetchone()
            if row is None:
                return 401, {"error": "unknown user"}
            return 200, {"token": row["token"], "role": row["role"],
                         "area": row["area"]}

        if path == "/api/devices" and method == "GET":
            user = self._user(headers, "read")
            return 200, {"items": self.repo.devices(area_filter(user))}

        if path == "/api/devices" and method == "POST":
            self._user(headers, "device:write")
            did = self.repo.add_device(body["code"], body["name"],
                                       body["area"], body.get("owner_group", ""))
            self.repo.audit("api", "device_created", f"device:{did}", body["code"])
            return 201, {"id": did}

        if path == "/api/tickets" and method == "GET":
            user = self._user(headers, "read")
            page = int(query.get("page", ["1"])[0])
            size = min(int(query.get("size", ["20"])[0]), 200)
            return 200, self.repo.list_tickets(
                status=query.get("status", [None])[0],
                area=area_filter(user), page=page, size=size)

        if path == "/api/tickets/transition" and method == "POST":
            user = self._user(headers, "ticket:transition")
            return 200, self.repo.transition_ticket(
                int(body["id"]), body["action"], user)

        if path == "/api/reviews" and method == "GET":
            self._user(headers, "read")
            return 200, {"items": self.repo.list_reviews()}

        if path == "/api/reviews/resolve" and method == "POST":
            user = self._user(headers, "review:write")
            action = body.get("action")
            if action not in ("confirm", "reject"):
                return 400, {"error": "action must be confirm|reject"}
            return 200, self.repo.resolve_review(
                int(body["id"]), action, user.username, body.get("note"))

        if path == "/api/frames" and method == "POST":
            user = self._user(headers, "frame:push")
            pid = int(body["point_id"])
            dev = self.repo.conn.execute(
                "SELECT area FROM device WHERE id = ?", (pid,)).fetchone()
            if dev is None:
                return 404, {"error": "device not found"}
            if not can_see(user, dev["area"]):
                return 403, {"error": "cross-area access denied"}
            return 200, self.service.handle_frame(pid, {"defects": body.get("defects", [])})

        if path == "/api/manual-report" and method == "POST":
            self._user(headers, "ticket:write")
            return 200, self.service.manual_report(
                int(body["point_id"]), body["cls"])

        if path == "/api/metrics" and method == "GET":
            self._user(headers, "read")
            return 200, self.repo.stats()

        if path == "/api/feedback" and method == "GET":
            self._user(headers, "export")
            return 200, {"items": self.repo.false_positives()}

        return 404, {"error": f"no route {method} {path}"}


class Handler(BaseHTTPRequestHandler):
    api: Api | None = None

    def _send(self, code, payload, content_type="application/json; charset=utf-8"):
        import json
        raw = (json.dumps(payload, ensure_ascii=False, indent=2)
               if content_type.startswith("application/json")
               else str(payload)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        import json
        u = urlparse(self.path)
        if method == "GET" and u.path in ("/", "/dashboard"):
            from web.dashboard import render
            html = render(self.api.repo, Path("reports/dashboard.html"))
            return self._send(200, html, "text/html; charset=utf-8")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})
        try:
            code, payload = self.api.dispatch(
                method, u.path, parse_qs(u.query), body, dict(self.headers))
        except PermissionError as exc:
            code, payload = 403, {"error": str(exc)}
        except (KeyError, ValueError, TypeError) as exc:
            code, payload = 400, {"error": f"{type(exc).__name__}: {exc}"}
        except Exception as exc:
            log.exception("unhandled api error")
            code, payload = 500, {"error": "internal error",
                                  "detail": type(exc).__name__}
        return self._send(code, payload)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)


def make_server(host="127.0.0.1", port=8000, *, repo, service):
    Handler.api = Api(repo, service)
    return ThreadingHTTPServer((host, port), Handler)
