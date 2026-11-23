"""SQLAlchemy ORM 模型：7 张表 + 索引 + 约束。

====================================================================
【这份文件在学什么】SQLAlchemy 2.0 声明式写法
====================================================================
一个类 = 一张表，类属性 = 列：

    class User(Base)                 ←→  CREATE TABLE user (...)
        id: Mapped[int]              ←→  id INTEGER
        username: Mapped[str]        ←→  username TEXT NOT NULL
            = mapped_column(unique=True)  ←→  UNIQUE
        __table_args__ = (Index(...),)    ←→  CREATE INDEX ...
        __table_args__ = (CheckConstraint(...),)  ←→  CHECK (...)

【类型标注的两条规则，记住就够】
    Mapped[str]        → 列 NOT NULL
    Mapped[str | None] → 列允许 NULL
SQLAlchemy 2.0 会从标注自动推断，所以下面很多 nullable=False 是冗余写法，
保留只是为了让人一眼看到约束。

【和 app/models.py 的关系】
models.py 里的 Event / User / Ticket 是「领域对象」（dataclass，带业务方法），
本文件里的同名类是「数据库行」。两者故意分开：
  - 领域对象：Event.key() 算幂等键、Ticket 状态机用得上
  - ORM 对象：只管读写
导入时统一写 `from . import orm` 然后 `orm.Event`，避免两个 Event 打架。

【为什么表名列名一个字都没改】
data/inspect.db 里已有旧数据，改列名就得写迁移脚本。
保持同名，ORM 只换"怎么读写"，数据文件能直接用。
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
    """所有模型的基类。

    SQLAlchemy 通过它收集全部表的元数据（Base.metadata）。
    `Base.metadata.create_all(engine)` 就是拿这份元数据去建表，
    所以只要模块被 import 过，建表时不会漏表。
    """


def _now() -> str:
    """默认时间戳。

    存 ISO 字符串而不是 datetime 对象，三个原因：
    1. SQLite 没有原生 DATETIME 类型，存字符串最省事；
    2. 看板/接口直接展示，不用二次转换；
    3. ISO 格式的字典序 = 时间序，ORDER BY 仍然正确。
    """
    return datetime.now().isoformat(timespec="seconds")


class User(Base):
    """账号表。

    role 决定功能权限（能做哪些操作），area 决定数据权限（能看哪些点位）。
    area='*' 表示全区域 —— 这是"行级数据权限"的落点，
    不用给每张业务表都加一个租户字段。
    """

    __tablename__ = "user"
    # 表级约束：SQLite 里 CHECK 会被真正执行，写入非法角色会报 IntegrityError
    __table_args__ = (
        CheckConstraint("role IN ('admin','operator','auditor','viewer')",
                        name="ck_user_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)              # 自增主键
    username: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String, nullable=False)               # 四种角色之一
    area: Mapped[str] = mapped_column(String, nullable=False, default="*")   # '*' = 全区域
    token: Mapped[str] = mapped_column(String, nullable=False, unique=True)  # demo 固定 token

    def to_dict(self) -> dict:
        """转普通 dict。上层（API/看板）只认 dict，不依赖 ORM 对象。

        这样出了事务也还能安全使用 —— 不会因为 Session 关闭而拿到过期属性。
        """
        return {"id": self.id, "username": self.username, "role": self.role,
                "area": self.area, "token": self.token}


class Device(Base):
    """点位台账。摄像头 / 传感器都记在这里，一条 = 一个可巡检的点。

    enabled=0 表示停用；台账是静态属性，和"什么时候巡"无关（那在 Plan 表）。
    """

    __tablename__ = "device"
    __table_args__ = (Index("idx_device_area", "area"),)   # 按区域过滤是高频查询

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String, nullable=False, unique=True)  # A-CAM-01
    name: Mapped[str] = mapped_column(String, nullable=False)
    area: Mapped[str] = mapped_column(String, nullable=False)               # A / B
    owner_group: Mapped[str] = mapped_column(String, nullable=False)        # 维修班组
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
                                         server_default="1")
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {"id": self.id, "code": self.code, "name": self.name,
                "area": self.area, "owner_group": self.owner_group,
                "enabled": self.enabled, "created_at": self.created_at}


class Plan(Base):
    """巡检计划：每个点位一条，控制"多久巡一次"。

    两个运行态字段是本表的核心：
      last_run_at —— 上次被抢占时的时间戳（epoch 秒，不是字符串）
      running     —— 0/1，谁把它从 0 改成 1 谁就拿到了这次巡检的执行权
    抢占逻辑见 repo.claim_plan，用 UPDATE ... WHERE running=0 做 CAS。
    """

    __tablename__ = "plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("device.id"), nullable=False)
    interval_s: Mapped[int] = mapped_column(Integer, nullable=False, default=300,
                                            server_default="300")   # 5 分钟一轮
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
                                         server_default="1")
    last_run_at: Mapped[float | None] = mapped_column(Float, nullable=True)   # epoch 秒
    running: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                         server_default="0")

    def to_dict(self) -> dict:
        return {"id": self.id, "device_id": self.device_id,
                "interval_s": self.interval_s, "enabled": self.enabled,
                "last_run_at": self.last_run_at, "running": self.running}


class Event(Base):
    """检测事件：一行 = 一次「某点位某缺陷」的判定结果。

    dedup_key 是整套幂等机制的地基：
    它是唯一索引，配合 on_conflict_do_nothing，保证同一条消息
    重复投递时只落一条记录（实现见 repo.save_event）。

    键的算法在 app/models.py 的 Event.key()：
        sha1(点位 | 缺陷类型 | 5 分钟时间桶 | 模型版本)
    """

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint("route IN ('auto','review')", name="ck_event_route"),
        Index("idx_event_ts", "ts"),                    # 按时间查
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 唯一约束 = 幂等的最后一道闸，不要去掉
    dedup_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    point_id: Mapped[int] = mapped_column(ForeignKey("device.id"), nullable=False)
    cls: Mapped[str] = mapped_column(String, nullable=False)      # rust / smoke / ...
    score: Mapped[float] = mapped_column(Float, nullable=False)   # 置信度 0~1
    model_ver: Mapped[str] = mapped_column(String, nullable=False)  # stub-1.0 / onnx-1.0
    route: Mapped[str] = mapped_column(String, nullable=False)    # auto=建单 review=复核
    degraded: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                          server_default="0")     # 1=模型故障时产生的
    ts: Mapped[str] = mapped_column(String, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {"id": self.id, "dedup_key": self.dedup_key,
                "point_id": self.point_id, "cls": self.cls, "score": self.score,
                "model_ver": self.model_ver, "route": self.route,
                "degraded": self.degraded, "ts": self.ts}


class Ticket(Base):
    """维修工单。由事件派生，event_id 指回来源，能一路追溯到原始检测。

    status 的合法流转不写在这里，在 app/ticket.py 的 TRANSITIONS 表。
    模型只管"存什么"，状态机管"能怎么变" —— 职责分开，改流程不用动表结构。
    """

    __tablename__ = "ticket"
    __table_args__ = (
        CheckConstraint("priority IN ('P1','P2','P3')", name="ck_ticket_priority"),
        # 复合索引：工单列表按 point_id 过滤 + status 筛选 + created_at 排序
        Index("idx_ticket_point_status", "point_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("event.id"), nullable=True)
    point_id: Mapped[int] = mapped_column(ForeignKey("device.id"), nullable=False)
    cls: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[str] = mapped_column(String, nullable=False)   # P1/P2/P3
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN",
                                        server_default="OPEN")
    assignee: Mapped[str | None] = mapped_column(String, nullable=True)  # 认领人
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now)
    sla_due_at: Mapped[str] = mapped_column(String, nullable=False)  # 建单时按优先级算

    def to_dict(self) -> dict:
        return {"id": self.id, "event_id": self.event_id,
                "point_id": self.point_id, "cls": self.cls,
                "priority": self.priority, "status": self.status,
                "assignee": self.assignee, "created_at": self.created_at,
                "updated_at": self.updated_at, "sla_due_at": self.sla_due_at}


class Review(Base):
    """人工复核队列。

    置信度落在 0.35~0.75 的事件进这里，由人判断：
      CONFIRMED → 补建工单
      REJECTED  → 记为误报，通过 /api/feedback 导出成训练负样本
    """

    __tablename__ = "review"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','CONFIRMED','REJECTED')",
                        name="ck_review_status"),
        Index("idx_review_status", "status"),      # 待办队列只查 PENDING
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING",
                                        server_default="PENDING")
    reviewer: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)   # 判误报的理由
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {"id": self.id, "event_id": self.event_id, "status": self.status,
                "reviewer": self.reviewer, "note": self.note,
                "created_at": self.created_at}


class AuditLog(Base):
    """审计流水：降级、去重命中、状态流转、复核，全部留痕。

    故意不加外键 —— 审计记录要在被审计对象删除之后依然存在。
    target 用 "ticket:12" / "device:3" 这种字符串指代，弱引用，不强约束。
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("idx_audit_action", "action", "ts"),)  # 按动作统计

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String, nullable=False)     # 谁做的
    action: Mapped[str] = mapped_column(String, nullable=False)    # detector_degraded / dedup_hit / ...
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[str] = mapped_column(String, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {"id": self.id, "actor": self.actor, "action": self.action,
                "target": self.target, "detail": self.detail, "ts": self.ts}
