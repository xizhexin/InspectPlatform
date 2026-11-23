# SmartInspect · 园区智能巡检平台

> 一个**传统业务系统**（设备台账 / 巡检计划 / 工单状态机 / RBAC / 审计 / 看板），
> 把视觉检测作为**可插拔、可降级**的 AI 模块接进来。
> 模型超时或异常时，巡检记录、人工报修、工单流转照常可用。

## 为什么是这种结构

巡检场景里 AI 通常只是某个环节，工程上真正重要的是：需求变了能不能接、线上出了问题能不能定位、代码别人能不能维护。所以本项目代码 80% 是传统系统工程，AI 只占 `ai/` 三个文件，且通过 `Detector` 协议与业务层解耦——换模型不改一行业务代码。

## 快速开始（Python 3.10+）

```bash
cd InspectPlatform
pip install -r requirements.txt                   # sqlalchemy + fastapi + uvicorn
python -m unittest discover -s tests -v          # 30 个用例
python bench.py --frames 2000 --workers 8         # 压测，生成 reports/metrics.json + 看板
python run.py --no-serve                          # 只初始化 + 生成看板
python run.py                                     # 起服务：http://127.0.0.1:8000/dashboard
```

数据层 SQLAlchemy 2.0 ORM，接入层 FastAPI + uvicorn，其余（看板渲染、压测、配置）是标准库。

服务起来后另有 `/docs`（Swagger UI，12 个接口可现场调）和 `/redoc`。
也可以直接用工厂模式起：
```bash
uvicorn app.main:create_app --factory --port 8000
```
Docker 文件已提供，**本机 Docker daemon 未启动，build/run 未执行验证**。

## 目录

```
config.py                 阈值规则（读 configs/rules.json）
configs/rules.json        置信度三档、去重窗口、SLA、超时——改配置不改代码
app/db.py                 engine / session / 建表 / 种子数据
app/orm.py                SQLAlchemy 模型：7 张表 + 索引 + 约束
app/models.py             领域模型（Event.key 幂等键在这里）
app/ticket.py             工单状态机 + SLA 逾期
app/auth.py               RBAC + 行级数据权限
app/repo.py               数据访问：事务、幂等、分页、审计、计划抢占、误报导出
app/scheduler.py          巡检计划防重跑 + 崩溃残留回收
app/main.py               FastAPI 应用：路由 + Depends 鉴权 + Pydantic 请求体
ai/detector.py            Detector 协议 / StubDetector / OnnxDetector
ai/service.py             编排：超时保护 / 三档分流 / 去重 / 复核 / 降级 / 人工报修
ai/rules.py               规则入口（实现在 config.py，避免循环依赖）
web/dashboard.py          静态看板渲染
bench.py                  并发压测 + 指标落盘
tests/                    6 个模块 / 30 个用例
```

## 数据流

```
摄像头/人工上报一帧
  → InspectionService._infer（线程池 + 超时保护）
  → 检测器异常/超时：标记 degraded + 写审计，主流程继续
  → 三档分流：<0.35 丢弃 | 0.35~0.75 转人工复核 | ≥0.75 自动建单
  → 幂等键 = 点位 + 缺陷类型 + 5 分钟时间桶 + 模型版本
  → on_conflict_do_nothing + UNIQUE 索引双保险（防 check-then-act 竞态）
  → 同一事务内：事件落库 + 建单 / 入复核队列
  → 复核确认补建工单；判误报导出训练反馈（/api/feedback）
```

## 数据模型（7 张表）

| 表 | 作用 | 关键约束 |
|---|---|---|
| `user` | 账号 / 角色 / 可见区域 | token 唯一；`area='*'` 全区域 |
| `device` | 点位台账 | code 唯一，`idx_device_area` |
| `plan` | 巡检计划 | `running` 标志防重跑，`interval_s` 控制周期 |
| `event` | 检测事件 | **`dedup_key` 唯一索引** = 幂等最后一道保险 |
| `ticket` | 工单 | 状态机 + SLA，`idx_ticket_point_status` |
| `review` | 人工复核队列 | PENDING / CONFIRMED / REJECTED |
| `audit_log` | 审计 | 降级、去重、流转、复核全部留痕 |

## API（请求头 `X-Token`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（免鉴权） |
| POST | `/api/login` | `{"username":"admin"}` → token |
| GET/POST | `/api/devices` | 设备台账（按区域过滤）/ 新增（admin） |
| GET | `/api/tickets?status=&page=` | 工单分页 |
| POST | `/api/tickets/transition` | `{"id":1,"action":"CLAIM"}` |
| GET | `/api/reviews` | 待复核队列 |
| POST | `/api/reviews/resolve` | `{"id":1,"action":"confirm|reject"}` |
| POST | `/api/frames` | `{"point_id":1,"defects":[{"cls":"rust","score":0.9}]}` |
| POST | `/api/manual-report` | AI 降级时的人工报修 |
| GET | `/api/metrics` | 统计 |
| GET | `/api/feedback` | 误报样本（需 export 权限） |

demo token：`tk-admin`(全区域管理员) / `tk-op-a`(A 区操作员) / `tk-op-b` /
`tk-audit`(只读+导出) / `tk-view-a`(A 区只读)。**固定 token 仅用于本地演示，生产接 SSO**。

## 本机实测结果（Python 3.11.4 / Windows 10 / SQLAlchemy 2.0 ORM）

来源：`reports/metrics.json`，`python bench.py --frames 2000 --workers 8` 可复现。
压测为单次运行，机器负载不同会有波动，**以下以最终落盘的 `metrics.json` 为准，其他文档已同步**。

| 指标 | 实测值 |
|---|---|
| 压测规模 | 2000 帧 / 8 并发 / 6 点位 / 模拟 1 小时窗口 |
| 总耗时 / 吞吐 | 5.610 s / **356.48 fps** |
| 延迟 p50 / p95 / p99 / max | 21.97 / **26.46** / 44.45 / 58.85 ms |
| 自动建单 / 转复核 / 低分丢弃 | 108 / 180 / 273（并发下 ±1 波动） |
| 幂等去重命中 | 3428 次（去重率 92.25%） |
| 唯一事件键 | 288（= 6 点位 × 4 缺陷 × 12 个 300s 时间桶，与设计一致） |
| **重复建单违规** | **0** |
| AI 全故障降级专项 | 50 帧全部标记 degraded，人工报修 50/50 成功，主流程成功率 **100%** |
| 单元测试 | **30/30 通过**（1.27 s） |

> 压测刻意让同一 (点位, 缺陷) 重复出现以压幂等路径，**92.25% 是负载重复率，不是业务优化前后的告警下降比例**，不要写成「告警下降 92%」。

## 已实测 / 未实测（如实标注）

- ✅ 已实测：全部业务链路、30 个单测、并发压测、降级专项、FastAPI 路由（TestClient 走真实 HTTP 层）、`run.py` 起 uvicorn 后逐个接口打过一遍（health / login / devices / frames / tickets / transition / metrics / dashboard / openapi / 404）。
- ⚠️ 接口预留、未接真实权重：`OnnxDetector`（需注入 `decode`；未下载 YOLO/ONNX 权重，不声明 mAP/精度）。
- ⚠️ 未执行：Docker build/run（本机 daemon 未启动）、真实摄像头视频流、生产数据库。
- 检测器 `StubDetector` 用合成帧，**只用于验证业务系统能力，不产生任何模型精度结论**。

## 三句话总结

1. 主体是设备台账 + 巡检计划 + 工单状态机 + RBAC 的传统系统，AI 只是一个可插拔的检测接口。
2. 检测结果按置信度三档分流，落库前用「点位+缺陷+时间桶+模型版本」做幂等去重，事件和工单放在同一事务里。
3. 模型超时或异常自动降级并留审计，巡检和人工报修照常可用；低置信进人工复核，判误报的样本回流成训练反馈。

## 已知限制与生产演进

- SQLite 单写者 + 进程内锁串行化所有写事务，是本次压测 p95 的主要来源 → 生产换 MySQL / GaussDB，拆连接池与读写分离。已开 WAL（读写不互斥）并按线程分配 Session，`BEGIN IMMEDIATE` 会让读事务也去抢写锁，所以改成普通 `BEGIN` + 写方法统一加锁。
- 推理超时用线程池 `future.result(timeout=...)`，**Python 无法强制取消已提交线程**，超时后后台线程仍会跑完 → 生产改为独立推理服务 + 请求级超时/熔断。
- 登录用固定 token、权限是进程内查表 → 生产接 SSO + 网关鉴权。
- 点位规模上去后，计划调度换分布式锁 / 消息队列；事件表按时间分区。
