"""一键启动：python run.py [--no-serve] [--host 0.0.0.0] [--port 8000]"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from ai.detector import StubDetector
from ai.service import InspectionService
from app.main import create_app
from app.repo import Repo
from web.dashboard import render


def main():
    ap = argparse.ArgumentParser(description="SmartInspect 园区智能巡检平台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/inspect.db")
    ap.add_argument("--no-serve", action="store_true",
                    help="只初始化数据库并生成看板，不起服务")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    Path("data").mkdir(exist_ok=True)
    Path("reports").mkdir(exist_ok=True)
    repo = Repo(args.db)
    service = InspectionService(StubDetector(), repo)
    out = Path("reports/dashboard.html")
    render(repo, out)
    print(f"dashboard -> {out}")
    if args.no_serve:
        service.close()
        repo.close()
        return

    app = create_app(repo, service)
    print(f"API: http://{args.host}:{args.port}/dashboard  (X-Token: tk-admin)")
    print(f"Docs: http://{args.host}:{args.port}/docs")
    # 关闭时由 app 的 lifespan 统一 close service / repo
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
