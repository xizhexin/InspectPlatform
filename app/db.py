"""Engine / Session / 建表 / 种子数据。

====================================================================
【三个概念，先分清】这是理解 SQLAlchemy 的第一步
====================================================================
Base     —— 表的元数据集合。所有模型都继承它，create_all() 靠它建表。
engine   —— 连接池 + SQL 方言（dialect）。全局一个，进程内复用。
            它自己不带事务，也不缓存对象。
Session  —— 一次"工作单元"：缓存对象、攒 SQL、控制事务边界。
            短生命周期，用完就关。

调用链：engine → Session(bind=engine) → session.add / execute / commit

====================================================================
【为什么不能直接用 create_engine 的默认配置】
====================================================================
1. SQLite 默认**不检查外键**，必须每条连接执行 PRAGMA foreign_keys=ON；
2. 默认事务是 DEFERRED（读不加锁，写时才尝试升级），并发下升级会失败。
   这里改成 BEGIN IMMEDIATE：一开事务就拿写锁，慢一点但不会中途报锁错误。
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
    """建 engine，并挂两个事件钩子。

    参数 readonly=True 时走 SQLite 的 URI 模式（mode=ro），
    物理上禁止写库 —— 比"约定只读"可靠。
    """
    p = Path(path)
    if not readonly:
        p.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{p.as_posix()}"
    else:
        url = f"sqlite:///file:{p.as_posix()}?mode=ro&uri=true"

    engine = create_engine(url, future=True)

    # 钩子一：每建立一条底层连接时触发（连接池新建连接，不是每次查询）
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):          # noqa: ANN001
        # SQLite 外键约束默认关闭，必须逐连接开启
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        # 关掉 pysqlite 自带的隐式 BEGIN，把事务控制权交给 SQLAlchemy，
        # 否则会出现"事务里又开事务"的经典报错
        dbapi_conn.isolation_level = None

    # 钩子二：每次 SQLAlchemy 要开事务时触发，把默认的 BEGIN 换成 BEGIN IMMEDIATE
    @event.listens_for(engine, "begin")
    def _on_begin(conn):                           # noqa: ANN001
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def init_db(path: str | Path, *, seed: bool = True, reset: bool = False):
    """建库并返回 engine。

    create_all 是幂等的：表存在就跳过，不会动数据。
    只有 reset=True 才真的删文件（压测前用，保证结果可复现）。
    """
    p = Path(path)
    if reset and p.exists():
        p.unlink()
    engine = make_engine(p)
    Base.metadata.create_all(engine)      # 按 orm.py 里的模型定义建表 + 索引
    if seed:
        _seed(engine)
    return engine


def _seed(engine) -> None:
    """写入演示数据。

    每个实体先 select 一遍已存在的标识，再补缺的 ——
    这是"应用层幂等"的简单形态。并发跑两次 seed 仍有窗口，
    真正兜底的是 username / code 上的唯一约束。
    """
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
        s.flush()      # 先落库，下面才能查到自增 id

        planned = set(s.scalars(select(Plan.device_id)))
        for dev_id in s.scalars(select(Device.id)):
            if dev_id not in planned:
                s.add(Plan(device_id=dev_id, interval_s=300, enabled=1, running=0))
        s.commit()


def make_session(engine) -> sessionmaker:
    """返回 Session 工厂。

    expire_on_commit=False 很关键：默认 commit 之后对象属性会"过期"，
    下次访问 name 会偷偷再查一次库；关掉后 commit 完还能直接读，
    于是 to_dict() 在事务外也能安全调用。
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
