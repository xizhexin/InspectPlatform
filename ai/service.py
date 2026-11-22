"""AI 编排层：阈值分流 / 幂等去重 / 人工复核 / 模型故障降级。

关键设计：AI 只是主流程中的一个环节。检测器超时或抛异常时，
巡检记录、人工报修、工单流转照常可用 —— 这是"进项目部能跟进"的核心。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime

from app.models import Event
from config import load_rules, priority_of, route_of

log = logging.getLogger(__name__)


class InspectionService:
    def __init__(self, detector, repo, rules=None, clock=datetime.now,
                 pool_size=8):
        self.detector = detector
        self.repo = repo
        self.rules = rules or load_rules()
        self.clock = clock
        self._pool = ThreadPoolExecutor(max_workers=pool_size,
                                        thread_name_prefix="infer")

    def close(self):
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _infer(self, frame):
        """推理超时保护。线程无法被强制取消，超时后仍在后台跑完，见 README 限制。"""
        fut = self._pool.submit(self.detector.infer, frame)
        try:
            return fut.result(timeout=self.rules["detector_timeout_s"])
        except FutureTimeout as exc:
            raise TimeoutError(
                f"detector timeout > {self.rules['detector_timeout_s']}s"
            ) from exc

    def handle_frame(self, point_id: int, frame) -> dict:
        ts = self.clock()
        detector_ok = True
        try:
            dets = self._infer(frame)
        except Exception as exc:                      # 模型故障 = 降级，不是系统故障
            detector_ok = False
            dets = []
            log.warning("detector_unavailable point=%s err=%r", point_id, exc)
            self.repo.mark_degraded(point_id, f"{type(exc).__name__}: {exc}")

        out = {"created": 0, "review": 0, "deduplicated": 0,
               "discarded": 0, "degraded": not detector_ok}
        with self.repo.transaction():                 # 去重 + 落库 + 建单同一事务
            for d in dets:
                route = route_of(d["score"], self.rules)
                if route is None:
                    out["discarded"] += 1
                    continue
                ev = Event(point_id, d["cls"], d["score"], ts,
                           self.detector.version)
                eid = self.repo.save_event(ev, route, now=ts)
                if eid is None:                       # 唯一索引命中：重复缺陷
                    out["deduplicated"] += 1
                    self.repo.audit("system", "dedup_hit", f"device:{point_id}",
                                    ev.key(self.rules["dedup_window_s"]))
                    continue
                if route == "auto":
                    p = priority_of(ev.cls, ev.score, self.rules)
                    self.repo.create_ticket(point_id, ev.cls, p,
                                            event_id=eid, now=ts)
                    out["created"] += 1
                else:
                    self.repo.push_review(eid, now=ts)
                    out["review"] += 1
        return out

    def manual_report(self, point_id: int, cls: str, score: float = 1.0,
                      now: datetime | None = None) -> dict:
        """AI 不可用时的人工报修入口：证明业务闭环不依赖模型。"""
        ts = now or self.clock()
        ev = Event(point_id, cls, score, ts, "manual", degraded=True)
        with self.repo.transaction():
            eid = self.repo.save_event(ev, "auto", now=ts)
            if eid is None:
                return {"created": 0, "deduplicated": 1}
            p = priority_of(cls, score, self.rules)
            self.repo.create_ticket(point_id, cls, p, event_id=eid, now=ts)
            self.repo.audit("system", "manual_report", f"device:{point_id}", cls)
            return {"created": 1}
