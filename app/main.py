"""FastAPI 应用：路由 + 鉴权依赖 + Pydantic 请求体。

GET  /health                     健康检查（免鉴权）
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
GET  /                           看板（同 /dashboard）

鉴权做成依赖：每个路由声明自己需要的权限，权限不足抛 PermissionError，
由统一异常处理器转成 403；请求体由 Pydantic 校验，不合法转 400。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import area_filter, authenticate, can_see, require

log = logging.getLogger(__name__)


# ---------------- 请求体模型 ----------------

class LoginIn(BaseModel):
    username: str


class DeviceIn(BaseModel):
    code: str
    name: str
    area: str
    owner_group: str = ""


class TransitionIn(BaseModel):
    id: int
    action: str


class ReviewResolveIn(BaseModel):
    id: int
    action: str
    note: str | None = None


class Defect(BaseModel):
    cls: str
    score: float = Field(ge=0.0, le=1.0)


class FrameIn(BaseModel):
    point_id: int
    defects: list[Defect] = Field(default_factory=list)


class ManualReportIn(BaseModel):
    point_id: int
    cls: str


# ---------------- 应用工厂 ----------------

def create_app(repo=None, service=None, *, db_path="data/inspect.db") -> FastAPI:
    """组装应用。传入 repo/service 便于测试复用临时库。"""
    if repo is None:
        from .repo import Repo
        repo = Repo(db_path)
    if service is None:
        from ai.detector import StubDetector
        from ai.service import InspectionService
        service = InspectionService(StubDetector(), repo)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        service.close()
        repo.close()

    app = FastAPI(title="SmartInspect", version="2.0.0", lifespan=lifespan)

    # ---- 鉴权依赖 ----

    def guard(perm: str):
        """生成依赖：校验 token 与角色权限，返回 User。"""

        def dep(x_token: str = Header(default="", alias="X-Token")):
            return require(authenticate(repo.session, x_token), perm)

        return dep

    # ---- 统一错误出口：业务异常 → 4xx，形状固定 {"error": ...} ----

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_req, exc):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(PermissionError)
    async def _permission_error(_req, exc):
        return JSONResponse({"error": str(exc)}, status_code=403)

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(_req, exc):
        return JSONResponse({"error": "invalid request", "detail": exc.errors()},
                            status_code=400)

    async def _bad_request(_req, exc):
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=400)

    # 业务层用 KeyError/ValueError 表示"参数不对"，统一转 400
    for _exc_type in (KeyError, ValueError, TypeError):
        app.add_exception_handler(_exc_type, _bad_request)

    # ---- 路由 ----

    @app.get("/health", tags=["public"])
    def health():
        return {"status": "ok"}

    @app.post("/api/login", tags=["public"])
    def login(body: LoginIn):
        row = repo.find_user(body.username)
        if row is None:
            raise HTTPException(401, "unknown user")
        return {"token": row["token"], "role": row["role"], "area": row["area"]}

    @app.get("/api/devices", tags=["device"])
    def list_devices(user=Depends(guard("read"))):
        return {"items": repo.devices(area_filter(user))}

    @app.post("/api/devices", status_code=201, tags=["device"])
    def create_device(body: DeviceIn, user=Depends(guard("device:write"))):
        did = repo.add_device(body.code, body.name, body.area, body.owner_group)
        repo.audit("api", "device_created", f"device:{did}", body.code)
        return {"id": did}

    @app.get("/api/tickets", tags=["ticket"])
    def list_tickets(status: str | None = None, page: int = 1,
                     size: int = Query(20, ge=1, le=200),
                     user=Depends(guard("read"))):
        return repo.list_tickets(status=status, area=area_filter(user),
                                 page=page, size=size)

    @app.post("/api/tickets/transition", tags=["ticket"])
    def transition_ticket(body: TransitionIn,
                          user=Depends(guard("ticket:transition"))):
        return repo.transition_ticket(body.id, body.action, user)

    @app.get("/api/reviews", tags=["review"])
    def list_reviews(user=Depends(guard("read"))):
        return {"items": repo.list_reviews()}

    @app.post("/api/reviews/resolve", tags=["review"])
    def resolve_review(body: ReviewResolveIn, user=Depends(guard("review:write"))):
        if body.action not in ("confirm", "reject"):
            raise HTTPException(400, "action must be confirm|reject")
        return repo.resolve_review(body.id, body.action, user.username, body.note)

    @app.post("/api/frames", tags=["detect"])
    def push_frame(body: FrameIn, user=Depends(guard("frame:push"))):
        dev_area = repo.device_area(body.point_id)
        if dev_area is None:
            raise HTTPException(404, "device not found")
        if not can_see(user, dev_area):
            raise HTTPException(403, "cross-area access denied")
        return service.handle_frame(
            body.point_id, {"defects": [d.model_dump() for d in body.defects]})

    @app.post("/api/manual-report", tags=["detect"])
    def manual_report(body: ManualReportIn, user=Depends(guard("ticket:write"))):
        return service.manual_report(body.point_id, body.cls)

    @app.get("/api/metrics", tags=["stats"])
    def metrics(user=Depends(guard("read"))):
        return repo.stats()

    @app.get("/api/feedback", tags=["stats"])
    def feedback(user=Depends(guard("export"))):
        return {"items": repo.false_positives()}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard():
        from web.dashboard import render
        out = Path("reports/dashboard.html")
        out.parent.mkdir(parents=True, exist_ok=True)
        return HTMLResponse(render(repo, out))

    return app
