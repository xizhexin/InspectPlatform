"""工单状态机与 SLA。非法流转直接抛错，绝不静默吞掉。"""
from __future__ import annotations

from datetime import datetime

# status -> {action: next_status}
TRANSITIONS = {
    "OPEN": {"CLAIM": "CLAIMED", "CLOSE": "CLOSED"},
    "CLAIMED": {"RESOLVE": "RESOLVED", "REOPEN": "OPEN"},
    "RESOLVED": {"CLOSE": "CLOSED", "REOPEN": "CLAIMED"},
    "CLOSED": {},
}


def transition(row, action: str, user: str, now: datetime | None = None) -> dict:
    """row: sqlite3.Row / dict。返回更新后的字段字典（不改库，由 Repo 落库）。"""
    now = now or datetime.now()
    t = dict(row)
    allowed = TRANSITIONS.get(t["status"], {})
    if action not in allowed:
        raise ValueError(f"illegal transition: {t['status']} -> {action}")
    stamp = now.isoformat(timespec="seconds")
    return {
        "status": allowed[action],
        "assignee": user if action == "CLAIM" else t.get("assignee"),
        "updated_at": stamp,
    }


def is_overdue(row, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if row["status"] == "CLOSED":
        return False
    return datetime.fromisoformat(row["sla_due_at"]) < now
