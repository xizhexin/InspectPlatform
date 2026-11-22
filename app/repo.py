"""数据访问层：事务、幂等、分页、状态机落库、审计、计划抢占。"""
from __future__ import annotations

import threading
from datetime import datetime

from config import load_rules, priority_of, sla_due
from . import ticket as ticket_mod
from .db import init_db, transaction
from .models import Event


class Repo:
    def __init__(self, db_path, *, rules=None, seed=True, reset=False):
        self.rules = rules or load_rules()
        self.conn = init_db(db_path, seed=seed, reset=reset)
        self.lock = threading.RLock()

    def transaction(self):
        return transaction(self.conn, self.lock)

    def close(self):
        self.conn.close()

    # ---------------- 事件 / 工单 / 复核 ----------------

    def save_event(self, ev: Event, route: str, now: datetime | None = None):
        """INSERT OR IGNORE：唯一索引兜底并发去重。返回 event_id，重复返回 None。"""
        now = now or datetime.now()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO event"
            "(dedup_key, point_id, cls, score, model_ver, route, degraded, ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (ev.key(self.rules["dedup_window_s"]), ev.point_id, ev.cls,
             ev.score, ev.model_ver, route, int(ev.degraded),
             now.isoformat(timespec="seconds")),
        )
        return cur.lastrowid if cur.rowcount else None

    def seen(self, dedup_key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM event WHERE dedup_key = ?", (dedup_key,)
        ).fetchone() is not None

    def create_ticket(self, point_id, cls, priority, event_id=None,
                      now: datetime | None = None, assignee=None) -> int:
        now = now or datetime.now()
        stamp = now.isoformat(timespec="seconds")
        cur = self.conn.execute(
            "INSERT INTO ticket(event_id, point_id, cls, priority, status, assignee,"
            "created_at, updated_at, sla_due_at) VALUES(?,?,?,?,'OPEN',?,?,?,?)",
            (event_id, point_id, cls, priority, assignee, stamp, stamp,
             sla_due(priority, now, self.rules)),
        )
        return cur.lastrowid

    def push_review(self, event_id: int, now: datetime | None = None) -> int:
        now = now or datetime.now()
        return self.conn.execute(
            "INSERT INTO review(event_id, status, created_at) VALUES(?,'PENDING',?)",
            (event_id, now.isoformat(timespec="seconds")),
        ).lastrowid

    def audit(self, actor, action, target=None, detail=None,
              now: datetime | None = None):
        now = now or datetime.now()
        self.conn.execute(
            "INSERT INTO audit_log(actor, action, target, detail, ts) VALUES(?,?,?,?,?)",
            (actor, action, target, detail, now.isoformat(timespec="seconds")),
        )
        if not self.conn.in_transaction:
            self.conn.commit()

    def mark_degraded(self, point_id: int, message: str):
        self.audit("system", "detector_degraded", f"device:{point_id}", message)

    # ---------------- 查询 ----------------

    def devices(self, area=None):
        sql = "SELECT * FROM device"
        args = []
        if area:
            sql += " WHERE area = ?"
            args.append(area)
        sql += " ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def add_device(self, code, name, area, owner_group) -> int:
        cur = self.conn.execute(
            "INSERT INTO device(code, name, area, owner_group, created_at) "
            "VALUES(?,?,?,?,?)",
            (code, name, area, owner_group,
             datetime.now().isoformat(timespec="seconds")),
        )
        if not self.conn.in_transaction:
            self.conn.commit()
        return cur.lastrowid

    def list_tickets(self, status=None, area=None, page=1, size=20):
        where, args = [], []
        if status:
            where.append("status = ?")
            args.append(status)
        if area:
            where.append("point_id IN (SELECT id FROM device WHERE area = ?)")
            args.append(area)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.conn.execute(
            "SELECT COUNT(*) FROM ticket" + clause, args
        ).fetchone()[0]
        rows = self.conn.execute(
            "SELECT * FROM ticket" + clause +
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size],
        ).fetchall()
        return {"total": total, "page": page, "size": size,
                "items": [dict(r) for r in rows]}

    def list_reviews(self, status="PENDING", limit=50):
        return [dict(r) for r in self.conn.execute(
            "SELECT r.*, e.cls, e.score, e.point_id FROM review r "
            "JOIN event e ON e.id = r.event_id "
            "WHERE r.status = ? ORDER BY r.id DESC LIMIT ?", (status, limit)
        ).fetchall()]

    def stats(self):
        c = self.conn
        by_status = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM ticket GROUP BY status")}
        by_cls = {r["cls"]: r["n"] for r in c.execute(
            "SELECT cls, COUNT(*) n FROM event GROUP BY cls")}
        return {
            "devices": c.execute("SELECT COUNT(*) FROM device").fetchone()[0],
            "events": c.execute("SELECT COUNT(*) FROM event").fetchone()[0],
            "tickets": c.execute("SELECT COUNT(*) FROM ticket").fetchone()[0],
            "open_tickets": sum(n for s, n in by_status.items() if s != "CLOSED"),
            "pending_reviews": c.execute(
                "SELECT COUNT(*) FROM review WHERE status='PENDING'").fetchone()[0],
            "degraded_calls": c.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='detector_degraded'"
            ).fetchone()[0],
            "duplicate_guard_hits": c.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='dedup_hit'"
            ).fetchone()[0],
            "by_status": by_status,
            "by_cls": by_cls,
        }

    def false_positives(self):
        """复核判误报的事件，导出为训练反馈，形成数据闭环。"""
        return [dict(r) for r in self.conn.execute(
            "SELECT e.id, e.cls, e.score, e.point_id, r.note FROM review r "
            "JOIN event e ON e.id = r.event_id WHERE r.status = 'REJECTED'"
        ).fetchall()]

    # ---------------- 状态机落库 ----------------

    def transition_ticket(self, ticket_id, action, user,
                          now: datetime | None = None):
        with self.transaction():
            row = self.conn.execute(
                "SELECT t.*, d.area FROM ticket t JOIN device d ON d.id = t.point_id "
                "WHERE t.id = ?", (ticket_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"ticket {ticket_id} not found")
            from .auth import can_see
            if not can_see(user, row["area"]):
                raise PermissionError("cross-area access denied")
            patch = ticket_mod.transition(row, action, user.username, now)
            self.conn.execute(
                "UPDATE ticket SET status=?, assignee=?, updated_at=? WHERE id=?",
                (patch["status"], patch["assignee"], patch["updated_at"], ticket_id),
            )
            self.audit(user.username, "ticket_transition", f"ticket:{ticket_id}",
                       f"{row['status']}->{patch['status']}")
            return {"id": ticket_id, **patch}

    def resolve_review(self, review_id, action, reviewer, note=None,
                       now: datetime | None = None):
        """confirm -> 补建工单；reject -> 记为误报，回流训练样本。"""
        with self.transaction():
            row = self.conn.execute(
                "SELECT r.*, e.cls, e.score, e.point_id FROM review r "
                "JOIN event e ON e.id = r.event_id WHERE r.id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"review {review_id} not found")
            if row["status"] != "PENDING":
                raise ValueError("review already resolved")
            status = "CONFIRMED" if action == "confirm" else "REJECTED"
            stamp = (now or datetime.now()).isoformat(timespec="seconds")
            self.conn.execute(
                "UPDATE review SET status=?, reviewer=?, note=? WHERE id=?",
                (status, reviewer, note, review_id),
            )
            ticket_id = None
            if status == "CONFIRMED":
                p = priority_of(row["cls"], row["score"], self.rules)
                ticket_id = self.create_ticket(row["point_id"], row["cls"], p,
                                               event_id=row["event_id"], now=now)
            self.audit(reviewer, "review_" + status, f"review:{review_id}", note)
            return {"review_id": review_id, "status": status,
                    "ticket_id": ticket_id}

    # ---------------- 巡检计划（防重跑） ----------------

    def claim_plan(self, now_ts: float):
        """原子抢占：running=0 的 plan 才能被 UPDATE 命中，避免多 worker 重复巡检。"""
        rows = self.conn.execute(
            "SELECT id, device_id, interval_s FROM plan "
            "WHERE enabled=1 AND running=0 "
            "AND (last_run_at IS NULL OR last_run_at + interval_s <= ?)",
            (now_ts,),
        ).fetchall()
        claimed = []
        with self.transaction():
            for r in rows:
                cur = self.conn.execute(
                    "UPDATE plan SET running=1, last_run_at=? "
                    "WHERE id=? AND running=0", (now_ts, r["id"]))
                if cur.rowcount:
                    claimed.append(dict(r))
        return claimed

    def release_plan(self, plan_id: int):
        self.conn.execute("UPDATE plan SET running=0 WHERE id=?", (plan_id,))
        if not self.conn.in_transaction:
            self.conn.commit()

    def recover_stale(self, now_ts: float, max_age_s: float = 600):
        """进程崩溃会残留 running=1；超过 max_age 强制回收，避免计划永久卡死。"""
        cur = self.conn.execute(
            "UPDATE plan SET running=0 WHERE running=1 AND last_run_at < ?",
            (now_ts - max_age_s,),
        )
        if not self.conn.in_transaction:
            self.conn.commit()
        return cur.rowcount
