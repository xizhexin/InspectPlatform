"""并发压测：合成帧驱动全链路，输出真实指标到 reports/metrics.json。

明确边界：
- 帧是合成的，检测器是 StubDetector，不接真实模型权重，因此不声明任何 mAP/精度。
- 指标衡量的是业务系统本身：幂等去重、并发写入、事务一致性、降级可用性、延迟。
- clock 随帧推进，模拟 1 小时巡检窗口（跨多个 300s 去重桶），避免全部落在同一桶。
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import platform
import random
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import sqlalchemy

from ai.detector import StubDetector
from ai.service import InspectionService
from app.repo import Repo
from web.dashboard import render

CLASSES = ["indicator_abnormal", "lane_occupied", "meter_abnormal", "rust"]
BASE = datetime(2026, 1, 1, 9, 0, 0)


def pct(values, q):
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[min(len(s) - 1, int(len(s) * q))], 3)


def make_jobs(frames, seed=20261109):
    rng = random.Random(seed)
    jobs = []
    for _ in range(frames):
        point = rng.randint(1, 6)
        defects = [
            {"cls": rng.choice(CLASSES), "score": round(rng.uniform(0.30, 0.99), 3)}
            for _ in range(rng.randint(1, 3))
        ]
        jobs.append((point, {"defects": defects}))
    return jobs


def run_degrade_test(n=50):
    """AI 全故障时，主流程（巡检落库 + 人工报修建单）是否仍 100% 可用。"""
    import logging
    logging.getLogger("ai.service").setLevel(logging.ERROR)  # 避免 50 条 warning 刷屏
    with tempfile.TemporaryDirectory() as td:
        repo = Repo(os.path.join(td, "degrade.db"), reset=True)
        svc = InspectionService(StubDetector(fail_rate=1.0), repo)
        try:
            frames = [svc.handle_frame(1, {"defects": []}) for _ in range(n)]
            reports = [svc.manual_report(1, f"manual_{i}") for i in range(n)]
            degraded = sum(1 for r in frames if r["degraded"])
            manual_ok = sum(1 for r in reports if r["created"] == 1)
            return {
                "frames": n,
                "all_marked_degraded": degraded == n,
                "manual_reports_ok": manual_ok,
                "main_flow_success_rate": round(manual_ok / n, 4),
                "degraded_audit_rows": repo.stats()["degraded_calls"],
            }
        finally:
            svc.close()
            repo.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--db", default="data/inspect.db")
    args = ap.parse_args()

    Path("data").mkdir(exist_ok=True)
    Path("reports").mkdir(exist_ok=True)
    repo = Repo(args.db, reset=True)

    tick = itertools.count()
    def clock():
        return BASE + timedelta(seconds=next(tick) * 3600.0 / args.frames)

    svc = InspectionService(StubDetector(seed=42), repo, clock=clock)
    jobs = make_jobs(args.frames)
    totals = {"created": 0, "review": 0, "deduplicated": 0,
              "discarded": 0, "degraded": 0}
    latencies = []

    def one(job):
        t0 = time.perf_counter()
        out = svc.handle_frame(job[0], job[1])
        return (time.perf_counter() - t0) * 1000, out

    t_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for ms, out in pool.map(one, jobs):
            latencies.append(ms)
            for k in totals:
                totals[k] += int(out[k]) if k == "degraded" else out[k]
    elapsed = time.perf_counter() - t_start
    svc.close()

    s = repo.stats()
    violations = repo.duplicate_ticket_violations()
    unique_keys = repo.unique_dedup_keys()

    metrics = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "third_party_deps": f"sqlalchemy {sqlalchemy.__version__}",
        },
        "workload": {
            "frames": args.frames,
            "concurrent_workers": args.workers,
            "points": 6,
            "simulated_window": "1h (clock advances with frame index)",
            "detector": "StubDetector (synthetic frames, NOT a trained model)",
        },
        "results": {
            "elapsed_s": round(elapsed, 3),
            "throughput_fps": round(args.frames / elapsed, 2),
            "latency_ms": {
                "p50": pct(latencies, 0.50),
                "p95": pct(latencies, 0.95),
                "p99": pct(latencies, 0.99),
                "max": round(max(latencies), 3),
            },
            "auto_tickets": totals["created"],
            "sent_to_review": totals["review"],
            "deduplicated": totals["deduplicated"],
            "discarded_low_confidence": totals["discarded"],
            "dedup_rate": round(totals["deduplicated"] /
                                max(1, totals["deduplicated"] + s["events"]), 4),
        },
        "integrity": {
            "unique_event_keys": unique_keys,
            "tickets": s["tickets"],
            "duplicate_ticket_violations": violations,
            "pending_reviews": s["pending_reviews"],
            "degraded_calls": s["degraded_calls"],
        },
        "degrade_test": run_degrade_test(),
        "note": "合成帧 + StubDetector；未接真实 YOLO/ONNX 权重，不声明检测精度。"
                "所有数字为本机真实运行结果，可用 --frames 复现。",
    }

    out = Path("reports/metrics.json")
    out.write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    render(repo, Path("reports/dashboard.html"))
    repo.close()

    print(f"frames={args.frames} workers={args.workers} "
          f"elapsed={metrics['results']['elapsed_s']}s "
          f"fps={metrics['results']['throughput_fps']}")
    print(f"p95={metrics['results']['latency_ms']['p95']}ms "
          f"tickets={s['tickets']} dedup={totals['deduplicated']} "
          f"violations={violations}")
    print(f"degrade_test={metrics['degrade_test']['main_flow_success_rate']}")
    print(f"-> {out} / reports/dashboard.html")


if __name__ == "__main__":
    main()
