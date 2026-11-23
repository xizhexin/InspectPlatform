"""SQLAlchemy ORM 模型：7 张表 + 索引 + 约束。

类属性即列：Mapped[str] → NOT NULL，Mapped[str | None] → 可空，由 2.0 自动推断。
与 app/models.py 同名类有意分开：那边是领域对象（带 Event.key() 等业务方法），
这边只管读写，导入时统一写 orm.Event。
表名列名保持和手写 SQL 版一致，旧库里已有数据可直接复用。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有模型的基类，Base.metadata.create_all() 靠它收集表定义。"""


def _now() -> str:
    """存 ISO 字符串：SQLite 无原生 datetime，且 ISO 字典序即时间序。"""
    return datetime.now().isoformat(timespec="seconds")


class User(Base):
    """账号表。role 管功能权限，area 管数据权限（'*' = 全区域）。"""

    __tablename__ = "user"
    __table_args__ = (
        CheckConstraint("role IN ('admin','operator','auditor','viewer')",
                        name="ck_user_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String, nullable=False)
    area: Mapped[str] = mapped_column(String, nullable=False, default="*")
    token: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    def to_dict(self) -> dict:
        """转 dict，让上层不依赖 ORM 对象的生命周期（出了事务也能读）。"""
        return {"id": self.id, "username": self.username, "role": self.role,
                "area": self.area, "token": self.token}


class Device(Base):
    """点位台账。静态属性，与"多久巡一次"无关（那在 Plan 表）。"""

    __tablename__ = "device"
    __table_args__ = (Index("idx_device_area", "area"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    area: Mapped[str] = mapped_column(String, nullable=False)
    owner_group: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
                                         server_default="1")
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {"id": self.id, "code": self.code, "name": self.name,
                "area": self.area, "owner_group": self.owner_group,
                "enabled": self.enabled, "created_at": self.created_at}


class Plan(Base):
    """巡检计划：每个点位一条。running 是抢占标志，last_run_at 是 epoch 秒。"""

    __tablename__ = "plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("device.id"), nullable=False)
    interval_s: Mapped[int] = mapped_column(Integer, nullable=False, default=300,
                                            server_default="300")
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
                                         server_default="1")
    last_run_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    running: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                         server_default="0")

    def to_dict(self) -> dict:
        return {"id": self.id, "device_id": self.device_id,
                "interval_s": self.interval_s, "enabled": self.enabled,
                "last_run_at": self.last_run_at, "running": self.running}


class Event(Base):
    """检测事件。dedup_key 是幂等地基：唯一索引 + on_conflict_do_nothing。"""

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint("route IN ('auto','review')", name="ck_event_route"),
        Index("idx_event_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 幂等的最后一道闸，不可去掉
    dedup_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    point_id: Mapped[int] = mapped_column(ForeignKey("device.id"), nullable=False)
    cls: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    model_ver: Mapped[str] = mapped_column(String, nullable=False)
    route: Mapped[str] = mapped_column(String, nullable=False)   # auto / review
    degraded: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                          server_default="0")
    ts: Mapped[str] = mapped_column(String, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {"id": self.id, "dedup_key": self.dedup_key,
                "point_id": self.point_id, "cls": self.cls, "score": self.score,
                "model_ver": self.model_ver, "route": self.route,
                "degraded": self.degraded, "ts": self.ts}


class Ticket(Base):
    """维修工单，event_id 指回来源事件。合法流转在 app/ticket.py，不在这里。"""

    __tablename__ = "ticket"
    __table_args__ = (
        CheckConstraint("priority IN ('P1','P2','P3')", name="ck_ticket_priority"),
        Index("idx_ticket_point_status", "point_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("event.id"), nullable=True)
    point_id: Mapped[int] = mapped_column(ForeignKey("device.id"), nullable=False)
    cls: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN",
                                        server_default="OPEN")
    assignee: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now)
    sla_due_at: Mapped[str] = mapped_column(String, nullable=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "event_id": self.event_id,
                "point_id": self.point_id, "cls": self.cls,
                "priority": self.priority, "status": self.status,
                "assignee": self.assignee, "created_at": self.created_at,
                "updated_at": self.updated_at, "sla_due_at": self.sla_due_at}


class Review(Base):
    """人工复核队列。CONFIRMED 补建工单，REJECTED 通过 /api/feedback 导出负样本。"""

    __tablename__ = "review"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','CONFIRMED','REJECTED')",
                        name="ck_review_status"),
        Index("idx_review_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING",
                                        server_default="PENDING")
    reviewer: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {"id": self.id, "event_id": self.event_id, "status": self.status,
                "reviewer": self.reviewer, "note": self.note,
                "created_at": self.created_at}


class AuditLog(Base):
    """审计流水。故意不加外键：被审计对象删了，审计记录仍要存在。"""

    __tablename__ = "audit_log"
    __table_args__ = (Index("idx_audit_action", "action", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[str] = mapped_column(String, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {"id": self.id, "actor": self.actor, "action": self.action,
                "target": self.target, "detail": self.detail, "ts": self.ts}
