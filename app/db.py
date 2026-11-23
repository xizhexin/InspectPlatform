"""Engine / Session / 建表 / 种子数据。

Base = 表元数据，engine = 连接池 + 方言，Session = 一次工作单元（管事务和对象缓存）。
SQLite 有两处默认行为必须改：外键默认不校验、默认事务是 DEFERRED（并发下升级锁会失败）。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from .orm import Base, Device, Plan, User

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


def make_engine(path: str | Path, *, readonly: bool = False):
    """建 engine。readonly 走 SQLite URI 的 mode=ro，物理禁写。"""
    p = Path(path)
    if not readonly:
        p.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{p.as_posix()}"
    else:
        url = f"sqlite:///file:{p.as_posix()}?mode=ro&uri=true"

    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):          # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        # 关掉 pysqlite 的隐式 BEGIN，事务交给 SQLAlchemy，否则会重复开事务
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _on_begin(conn):                           # noqa: ANN001
        # 一开事务就拿写锁，避免并发时锁升级失败
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def init_db(path: str | Path, *, seed: bool = True, reset: bool = False):
    """建库并返回 engine。create_all 幂等，只有 reset=True 才删文件。"""
    p = Path(path)
    if reset and p.exists():
        p.unlink()
    engine = make_engine(p)
    Base.metadata.create_all(engine)
    if seed:
        _seed(engine)
    return engine


def _seed(engine) -> None:
    """写入演示数据。先查已有标识再补缺的，真正兜底的是唯一约束。"""
    with Session(engine) as s:
        known_users = set(s.scalars(select(User.username)))
        for username, role, area, token in SEED_USERS:
            if username not in known_users:
                s.add(User(username=username, role=role, area=area, token=token))

        known_codes = set(s.scalars(select(Device.code)))
        for code, name, area, group in SEED_DEVICES:
            if code not in known_codes:
                s.add(Device(code=code, name=name, area=area,
                             owner_group=group, enabled=1))
        s.flush()      # 先落库，下面才查得到自增 id

        planned = set(s.scalars(select(Plan.device_id)))
        for dev_id in s.scalars(select(Device.id)):
            if dev_id not in planned:
                s.add(Plan(device_id=dev_id, interval_s=300, enabled=1, running=0))
        s.commit()


def make_session(engine) -> sessionmaker:
    """返回 Session 工厂。expire_on_commit=False 让 commit 后还能直接读属性。"""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
