"""SQLAlchemy ORM 模型：7 张表 + 索引 + 约束。

设计原则：
- 表名、列名、约束与手写 SQL 版完全一致，保证数据文件可直接沿用。
- 业务规则（状态机、阈值、优先级）不放模型里，仍留在 ticket.py / config.py。
- 原子性操作（幂等插入、滑动窗口 UPSERT）用 dialect 级 insert，不退回手写 SQL。
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
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class User(Base):
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
        return {"id": self.id, "username": self.username, "role": self.role,
                "area": self.area, "token": self.token}


class Device(Base):
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
    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint("route IN ('auto','review')", name="ck_event_route"),
        Index("idx_event_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    point_id: Mapped[int] = mapped_column(ForeignKey("device.id"), nullable=False)
    cls: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    model_ver: Mapped[str] = mapped_column(String, nullable=False)
    route: Mapped[str] = mapped_column(String, nullable=False)
    degraded: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                          server_default="0")
    ts: Mapped[str] = mapped_column(String, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {"id": self.id, "dedup_key": self.dedup_key,
                "point_id": self.point_id, "cls": self.cls, "score": self.score,
                "model_ver": self.model_ver, "route": self.route,
                "degraded": self.degraded, "ts": self.ts}


class Ticket(Base):
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
