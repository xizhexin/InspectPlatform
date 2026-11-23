"""数据访问层（ORM 版）：事务、幂等、分页、状态机落库、审计、计划抢占。

原子性仍由数据库保证：
- 幂等：sqlite_insert(...).on_conflict_do_nothing(index_elements=["dedup_key"])
- 计划抢占：UPDATE ... WHERE running=0，用 rowcount 判定
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from config import load_rules, priority_of, sla_due

from . import orm, ticket as ticket_mod
from .db import init_db, make_session
from .models import Event


def _stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class Repo:
    def __init__(self, db_path, *, rules=None, seed=True, reset=False):
        self.rules = rules or load_rules()
        self.engine = init_db(db_path, seed=seed, reset=reset)
        self.Session = make_session(self.engine)
        self.session = self.Session()
        self.lock = threading.RLock()
        self._depth = 0          # 事务嵌套深度

    # ---------------- 事务与连接 ----------------

    @contextmanager
    def transaction(self):
        """显式事务。嵌套时复用同一事务，最外层负责 commit/rollback。"""
        with self.lock:
            self._depth += 1
            try:
                yield self.session
                self._depth -= 1
                if self._depth == 0:
                    self.session.commit()
            except Exception:
                self._depth -= 1
                if self._depth == 0:
                    self.session.rollback()
                raise

    def _commit_if_top(self):
        if self._depth == 0:
            self.session.commit()

    def close(self):
        self.session.close()
        self.engine.dispose()

    # ---------------- 事件 / 工单 / 复核 ----------------

    def save_event(self, ev: Event, route: str, now: datetime | None = None):
        """幂等插入：唯一索引撞了返回 None。"""
        now = now or datetime.now()
        stmt = (
            sqlite_insert(orm.Event)
            .values(
                dedup_key=ev.key(self.rules["dedup_window_s"]),
                point_id=ev.point_id,
                cls=ev.cls,
                score=ev.score,
                model_ver=ev.model_ver,
                route=route,
                degraded=int(ev.degraded),
                ts=_stamp(now),
            )
            .on_conflict_do_nothing(index_elements=["dedup_key"])
        )
        res = self.session.execute(stmt)
        self._commit_if_top()
        return res.lastrowid if res.rowcount else None

    def seen(self, dedup_key: str) -> bool:
        return self.session.scalar(
            select(orm.Event.id).where(orm.Event.dedup_key == dedup_key)
        ) is not None

    def create_ticket(self, point_id, cls, priority, event_id=None,
                      now: datetime | None = None, assignee=None) -> int:
        now = now or datetime.now()
        stamp = _stamp(now)
        t = orm.Ticket(event_id=event_id, point_id=point_id, cls=cls,
                       priority=priority, status="OPEN", assignee=assignee,
                       created_at=stamp, updated_at=stamp,
                       sla_due_at=sla_due(priority, now, self.rules))
        self.session.add(t)
        self.session.flush()
        self._commit_if_top()
        return t.id

    def push_review(self, event_id: int, now: datetime | None = None) -> int:
        now = now or datetime.now()
        r = orm.Review(event_id=event_id, status="PENDING", created_at=_stamp(now))
        self.session.add(r)
        self.session.flush()
        self._commit_if_top()
        return r.id

    def audit(self, actor, action, target=None, detail=None,
              now: datetime | None = None):
        now = now or datetime.now()
        self.session.add(orm.AuditLog(actor=actor, action=action, target=target,
                                      detail=detail, ts=_stamp(now)))
        self.session.flush()
        self._commit_if_top()

    def mark_degraded(self, point_id: int, message: str):
        self.audit("system", "detector_degraded", f"device:{point_id}", message)

    # ---------------- 查询 ----------------

    def devices(self, area=None):
        stmt = select(orm.Device).order_by(orm.Device.id)
        if area:
            stmt = stmt.where(orm.Device.area == area)
        return [d.to_dict() for d in self.session.scalars(stmt)]

    def add_device(self, code, name, area, owner_group) -> int:
        d = orm.Device(code=code, name=name, area=area, owner_group=owner_group,
                       enabled=1, created_at=_stamp(datetime.now()))
        self.session.add(d)
        self.session.flush()
        self._commit_if_top()
        return d.id

    def device_area(self, point_id: int):
        return self.session.scalar(
            select(orm.Device.area).where(orm.Device.id == point_id)
        )

    def find_user(self, username: str):
        u = self.session.scalar(select(orm.User).where(orm.User.username == username))
        return u.to_dict() if u else None

    def list_tickets(self, status=None, area=None, page=1, size=20):
        stmt = select(orm.Ticket)
        if status:
            stmt = stmt.where(orm.Ticket.status == status)
        if area:
            stmt = stmt.where(orm.Ticket.point_id.in_(
                select(orm.Device.id).where(orm.Device.area == area)))
        total = self.session.scalar(
            select(func.count()).select_from(stmt.subquery()))
        rows = self.session.scalars(
            stmt.order_by(orm.Ticket.created_at.desc(), orm.Ticket.id.desc())
                .limit(size).offset((page - 1) * size))
        return {"total": total, "page": page, "size": size,
                "items": [t.to_dict() for t in rows]}

    def list_reviews(self, status="PENDING", limit=50):
        stmt = (select(orm.Review, orm.Event)
                .join(orm.Event, orm.Event.id == orm.Review.event_id)
                .where(orm.Review.status == status)
                .order_by(orm.Review.id.desc()).limit(limit))
        out = []
        for r, e in self.session.execute(stmt):
            d = r.to_dict()
            d.update({"cls": e.cls, "score": e.score, "point_id": e.point_id})
            out.append(d)
        return out

    def stats(self):
        s = self.session
        by_status = {row[0]: row[1] for row in s.execute(
            select(orm.Ticket.status, func.count()).group_by(orm.Ticket.status))}
        by_cls = {row[0]: row[1] for row in s.execute(
            select(orm.Event.cls, func.count()).group_by(orm.Event.cls))}
        return {
            "devices": s.scalar(select(func.count()).select_from(orm.Device)),
            "events": s.scalar(select(func.count()).select_from(orm.Event)),
            "tickets": s.scalar(select(func.count()).select_from(orm.Ticket)),
            "open_tickets": sum(n for st, n in by_status.items() if st != "CLOSED"),
            "pending_reviews": s.scalar(
                select(func.count()).select_from(orm.Review)
                .where(orm.Review.status == "PENDING")),
            "degraded_calls": s.scalar(
                select(func.count()).select_from(orm.AuditLog)
                .where(orm.AuditLog.action == "detector_degraded")),
            "duplicate_guard_hits": s.scalar(
                select(func.count()).select_from(orm.AuditLog)
                .where(orm.AuditLog.action == "dedup_hit")),
            "by_status": by_status,
            "by_cls": by_cls,
        }

    def false_positives(self):
        stmt = (select(orm.Event, orm.Review)
                .join(orm.Review, orm.Review.event_id == orm.Event.id)
                .where(orm.Review.status == "REJECTED"))
        return [{"id": e.id, "cls": e.cls, "score": e.score,
                 "point_id": e.point_id, "note": r.note}
                for e, r in self.session.execute(stmt)]

    # ---------------- 状态机落库 ----------------

    def transition_ticket(self, ticket_id, action, user, now=None):
        with self.transaction():
            t = self.session.get(orm.Ticket, ticket_id)
            if t is None:
                raise KeyError(f"ticket {ticket_id} not found")
            area = self.device_area(t.point_id)
            from .auth import can_see
            if not can_see(user, area):
                raise PermissionError("cross-area access denied")
            old_status = t.status
            patch = ticket_mod.transition(t.to_dict(), action, user.username, now)
            t.status = patch["status"]
            t.assignee = patch["assignee"]
            t.updated_at = patch["updated_at"]
            self.audit(user.username, "ticket_transition", f"ticket:{ticket_id}",
                       f"{old_status}->{patch['status']}")
            return {"id": ticket_id, **patch}

    def resolve_review(self, review_id, action, reviewer, note=None, now=None):
        with self.transaction():
            r = self.session.get(orm.Review, review_id)
            if r is None:
                raise KeyError(f"review {review_id} not found")
            if r.status != "PENDING":
                raise ValueError("review already resolved")
            e = self.session.get(orm.Event, r.event_id)
            status = "CONFIRMED" if action == "confirm" else "REJECTED"
            r.status = status
            r.reviewer = reviewer
            r.note = note
            ticket_id = None
            if status == "CONFIRMED":
                p = priority_of(e.cls, e.score, self.rules)
                ticket_id = self.create_ticket(e.point_id, e.cls, p,
                                               event_id=e.id, now=now)
            self.audit(reviewer, "review_" + status, f"review:{review_id}", note)
            return {"review_id": review_id, "status": status,
                    "ticket_id": ticket_id}

    # ---------------- 巡检计划 ----------------

    def claim_plan(self, now_ts: float):
        """原子抢占：running=0 才能被命中，避免多 worker 重复巡检。"""
        candidates = self.session.scalars(
            select(orm.Plan).where(
                orm.Plan.enabled == 1,
                orm.Plan.running == 0,
                (orm.Plan.last_run_at.is_(None))
                | (orm.Plan.last_run_at + orm.Plan.interval_s <= now_ts),
            )
        ).all()

        claimed = []
        with self.transaction():
            for p in candidates:
                res = self.session.execute(
                    update(orm.Plan)
                    .where(orm.Plan.id == p.id, orm.Plan.running == 0)
                    .values(running=1, last_run_at=now_ts)
                    .execution_options(synchronize_session=False)
                )
                if res.rowcount:
                    claimed.append({"id": p.id, "device_id": p.device_id,
                                    "interval_s": p.interval_s})
        return claimed

    def release_plan(self, plan_id: int):
        self.session.execute(
            update(orm.Plan).where(orm.Plan.id == plan_id).values(running=0)
            .execution_options(synchronize_session=False))
        self._commit_if_top()

    def recover_stale(self, now_ts: float, max_age_s: float = 600):
        res = self.session.execute(
            update(orm.Plan)
            .where(orm.Plan.running == 1, orm.Plan.last_run_at < now_ts - max_age_s)
            .values(running=0)
            .execution_options(synchronize_session=False))
        self._commit_if_top()
        return res.rowcount

    # ---------------- 压测自检 ----------------

    def duplicate_ticket_violations(self) -> int:
        sub = (select(orm.Event.dedup_key, func.count(orm.Ticket.id).label("n"))
               .join(orm.Event, orm.Event.id == orm.Ticket.event_id)
               .group_by(orm.Event.dedup_key).subquery())
        return self.session.scalar(
            select(func.count()).select_from(sub).where(sub.c.n > 1))

    def unique_dedup_keys(self) -> int:
        return self.session.scalar(
            select(func.count(func.distinct(orm.Event.dedup_key))))
