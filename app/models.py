"""领域模型。保持贫血 + frozen，业务规则放在 ticket.py / ai/rules.py。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    area: str
    token: str


@dataclass(frozen=True)
class Device:
    id: int
    code: str
    name: str
    area: str
    owner_group: str
    enabled: bool
    created_at: str


@dataclass(frozen=True)
class Detection:
    cls: str
    score: float
    box: tuple = ()


@dataclass(frozen=True)
class Event:
    point_id: int
    cls: str
    score: float
    ts: datetime
    model_ver: str
    degraded: bool = False

    def key(self, window_s: int = 300) -> str:
        """幂等键：点位 + 缺陷类型 + 时间桶 + 模型版本。

        模型版本进键是刻意的：灰度换模型后，同一缺陷应允许重新评估。
        """
        bucket = int(self.ts.timestamp() // window_s)
        raw = f"{self.point_id}|{self.cls}|{bucket}|{self.model_ver}"
        return hashlib.sha1(raw.encode()).hexdigest()


@dataclass(frozen=True)
class Ticket:
    id: int
    event_id: Optional[int]
    point_id: int
    cls: str
    priority: str
    status: str
    assignee: Optional[str]
    created_at: str
    updated_at: str
    sla_due_at: str


@dataclass(frozen=True)
class Review:
    id: int
    event_id: int
    status: str
    reviewer: Optional[str]
    note: Optional[str]
    created_at: str
