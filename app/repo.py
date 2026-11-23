"""数据访问层：事务、幂等、分页、状态机落库、审计、计划抢占。

所有 SQL 收在这里，业务层只调方法，换库时只改这一层。
两处原子操作是重点：save_event 的 on_conflict_do_nothing（幂等）、
claim_plan 的 UPDATE ... WHERE running=0（CAS 抢占）。
"""
from __future__ import annotations

import functools
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


def _write(fn):
    """写方法统一拿全局可重入锁：SQLite 单写者，多线程写必须串行。

    RLock 可重入，所以被 transaction() 包着调用时不会自锁。
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return fn(self, *args, **kwargs)

    return wrapper


class Repo:
    def __init__(self, db_path, *, rules=None, seed=True, reset=False):
        self.rules = rules or load_rules()
        self.engine = init_db(db_path, seed=seed, reset=reset)
        self.Session = make_session(self.engine)
        # Session 不是线程安全的：FastAPI 的 sync 路由跑在线程池里，
        # 所以按线程各建一个，并记下来好在 close 时统一关掉
        self._local = threading.local()
        self._sessions: set = set()
        self._sessions_lock = threading.Lock()
        self.lock = threading.RLock()   # 可重入，全局串行化写事务

    @property
    def session(self):
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = self.Session()
            self._local.session = sess
            with self._sessions_lock:
                self._sessions.add(sess)
        return sess

    @property
    def _depth(self) -> int:
        return getattr(self._local, "depth", 0)

    @_depth.setter
    def _depth(self, value: int) -> None:
        self._local.depth = value

    # ---------------- 事务与连接 ----------------

    @contextmanager
    def transaction(self):
        """可嵌套事务：只有最外层 commit/rollback，内层只加计数。

        需要嵌套是因为 resolve_review 里会调 create_ticket，
        不计数的话内层一提交，外层的审计就跑到事务外面去了。
        """
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
        """不在事务里才立刻提交，让写方法在事务内外都正确。"""
        if self._depth == 0:
            self.session.commit()

    def close(self):
        # 线程池里的 Session 不关掉，SQLite 文件会一直被占着
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for sess in sessions:
            sess.close()
        self.engine.dispose()

    # ---------------- 事件 / 工单 / 复核 ----------------

    @_write
    def save_event(self, ev: Event, route: str, now: datetime | None = None):
        """幂等插入：撞唯一索引返回 None。

        键由 ev.key() 算（点位 + 缺陷 + 时间桶 + 模型版本），同一条消息重复投递
        得到相同的键。on_conflict_do_nothing 比 INSERT OR IGNORE 精确——只忽略
        dedup_key 冲突，NOT NULL / CHECK 违反照常抛错。
        判定必须看 rowcount：冲突时 lastrowid 会返回上一次的 id，不是 0。
        """
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

    @_write
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

    @_write
    def push_review(self, event_id: int, now: datetime | None = None) -> int:
        now = now or datetime.now()
        r = orm.Review(event_id=event_id, status="PENDING", created_at=_stamp(now))
        self.session.add(r)
        self.session.flush()
        self._commit_if_top()
        return r.id

    @_write
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

    @_write
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

    @_write
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

    @_write
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

    @_write
    def claim_plan(self, now_ts: float):
        """抢占到期计划：先查候选，再逐条 CAS 抢。

        候选查询是乐观的，多个 worker 会拿到同一批；真正决定胜负的是
        UPDATE ... WHERE running=0——只有影响行数为 1 的那个把 0 改成了 1。
        所以不需要分布式锁。
        """
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
                # 只发 SQL 不刷内存对象，后面不再用这个对象
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

    @_write
    def release_plan(self, plan_id: int):
        self.session.execute(
            update(orm.Plan).where(orm.Plan.id == plan_id).values(running=0)
            .execution_options(synchronize_session=False))
        self._commit_if_top()

    @_write
    def recover_stale(self, now_ts: float, max_age_s: float = 600):
        """回收崩溃残留的 running=1，阈值需大于单次巡检最长耗时。"""
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
