"""可选增强：FastAPI/uvicorn 适配器。复用 Api.dispatch，不重复业务逻辑。

需要 fastapi + uvicorn（本机 monai 3.11 环境已装）；纯标准库环境请用 app/main.py。
启动：uvicorn app.api_fastapi:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

try:
    from fastapi import FastAPI, Header, Request
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("fastapi not installed; use app/main.py") from exc

from ai.detector import StubDetector
from ai.service import InspectionService
from app.repo import Repo
from .main import Api

repo = Repo("data/inspect.db")
service = InspectionService(StubDetector(), repo)
core = Api(repo, service)
app = FastAPI(title="SmartInspect", version="1.0.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.api_route("/api/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request, x_token: str = Header(default="")):
    body = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
    code, payload = core.dispatch(
        request.method, "/api/" + path, dict(request.query_params), body,
        {"X-Token": x_token or ""},
    )
    return JSONResponse(payload, status_code=code)
