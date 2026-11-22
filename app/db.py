"""SQLite 建表、索引、连接与事务。核心零第三方依赖。

设计要点：
- event.dedup_key 唯一索引是幂等去重最后一道保险（防 check-then-act 竞态）。
- ticket(point_id, status, created_at) 复合索引覆盖看板与工单分页的主要查询。
- 所有写操作走 BEGIN IMMEDIATE + 进程内锁，SQLite 单写者模型下语义清晰。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS user (
    id       INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    role     TEXT NOT NULL CHECK(role IN ('admin','operator','auditor','viewer')),
    area     TEXT NOT NULL DEFAULT '*',
    token    TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS device (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    area        TEXT NOT NULL,
    owner_group TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES device(id),
    interval_s  INTEGER NOT NULL DEFAULT 300,
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_run_at REAL,
    running     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS event (
    id         INTEGER PRIMARY KEY,
    dedup_key  TEXT NOT NULL UNIQUE,
    point_id   INTEGER NOT NULL REFERENCES device(id),
    cls        TEXT NOT NULL,
    score      REAL NOT NULL,
    model_ver  TEXT NOT NULL,
    route      TEXT NOT NULL CHECK(route IN ('auto','review')),
    degraded   INTEGER NOT NULL DEFAULT 0,
    ts         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER REFERENCES event(id),
    point_id    INTEGER NOT NULL REFERENCES device(id),
    cls         TEXT NOT NULL,
    priority    TEXT NOT NULL CHECK(priority IN ('P1','P2','P3')),
    status      TEXT NOT NULL DEFAULT 'OPEN',
    assignee    TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    sla_due_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES event(id),
    status      TEXT NOT NULL DEFAULT 'PENDING'
                     CHECK(status IN ('PENDING','CONFIRMED','REJECTED')),
    reviewer    TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT,
    detail  TEXT,
    ts      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ticket_point_status ON ticket(point_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_event_ts            ON event(ts);
CREATE INDEX IF NOT EXISTS idx_review_status       ON review(status);
CREATE INDEX IF NOT EXISTS idx_device_area         ON device(area);
CREATE INDEX IF NOT EXISTS idx_audit_action        ON audit_log(action, ts);
"""

SEED_USERS = [
    ("admin", "admin", "*", "tk-admin"),
    ("operator_a", "operator", "A", "tk-op-a"),
    ("operator_b", "operator", "B", "tk-op-b"),
    ("auditor", "auditor", "*", "tk-audit"),
    ("viewer_a", "viewer", "A", "tk-view-a"),
]

SEED_DEVICES = [
    ("A-CAM-01", "1号门摄像头", "A", "维修一组"),
    ("A-CAM-02", "配电房摄像头", "A", "维修一组"),
    ("A-CAM-03", "连廊摄像头", "A", "维修二组"),
    ("B-CAM-01", "车库入口摄像头", "B", "维修二组"),
    ("B-CAM-02", "水泵房摄像头", "B", "维修三组"),
    ("B-CAM-03", "屋顶摄像头", "B", "维修三组"),
]


def connect(path, *, readonly=False):
    """建立连接。readonly 走 URI mode=ro，物理上杜绝写库。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True,
                               check_same_thread=False, isolation_level=None)
    else:
        conn = sqlite3.connect(str(p), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path, *, seed=True, reset=False):
    """建库。reset=True 用于压测前清空，保证结果可复现。"""
    p = Path(path)
    if reset and p.exists():
        p.unlink()
    conn = connect(path)
    conn.executescript(SCHEMA)
    if seed:
        now = datetime.now().isoformat(timespec="seconds")
        conn.executemany(
            "INSERT OR IGNORE INTO user(username, role, area, token) VALUES(?,?,?,?)",
            SEED_USERS,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO device(code, name, area, owner_group, created_at) "
            "VALUES(?,?,?,?,?)",
            [(c, n, a, g, now) for c, n, a, g in SEED_DEVICES],
        )
        conn.executemany(
            "INSERT INTO plan(device_id, interval_s) "
            "SELECT id, 300 FROM device "
            "WHERE code = ? AND NOT EXISTS (SELECT 1 FROM plan WHERE device_id = device.id)",
            [(c,) for c, *_ in SEED_DEVICES],
        )
    conn.commit()
    return conn


@contextmanager
def transaction(conn, lock=None):
    """BEGIN IMMEDIATE 事务；异常回滚；lock 串行化跨线程写入。"""
    if lock is not None:
        lock.acquire()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if lock is not None:
            lock.release()
