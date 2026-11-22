"""巡检计划调度：DB 标志位抢占 + 崩溃残留回收，不依赖外部调度框架。"""
from __future__ import annotations

import time


class PlanRunner:
    def __init__(self, repo, clock=time.time):
        self.repo = repo
        self.clock = clock

    def tick(self, on_due=None):
        """on_due(plan) 为真正抓帧/调检测器的回调；无论成败都释放 running。"""
        claimed = self.repo.claim_plan(self.clock())
        results = []
        for plan in claimed:
            try:
                if on_due is not None:
                    results.append(on_due(plan))
            except Exception as exc:            # 单次巡检失败不影响其它点位
                self.repo.audit("scheduler", "plan_failed",
                                f"plan:{plan['id']}", repr(exc))
            finally:
                self.repo.release_plan(plan["id"])
        return {"due": len(claimed), "results": results}

    def recover(self, max_age_s=600):
        return self.repo.recover_stale(self.clock(), max_age_s)
