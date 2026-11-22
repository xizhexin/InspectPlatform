"""阈值与业务规则外置。新增缺陷类型/调门槛只改 configs/rules.json。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_RULES = Path(__file__).parent / "configs" / "rules.json"


def load_rules(path=None) -> dict:
    with open(path or DEFAULT_RULES, encoding="utf-8") as f:
        return json.load(f)


def route_of(score: float, rules: dict):
    """低于 min_score 直接丢弃；中间档进人工复核；高档自动建单。"""
    if score < rules["min_score"]:
        return None
    return "auto" if score >= rules["auto_score"] else "review"


def priority_of(cls: str, score: float, rules: dict) -> str:
    if cls in rules["critical"] and score >= 0.90:
        return "P1"
    if score >= rules["auto_score"]:
        return "P2"
    return "P3"


def sla_due(priority: str, now: datetime, rules: dict) -> str:
    minutes = rules["sla_minutes"].get(priority, 1440)
    return (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
