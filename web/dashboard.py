"""看板渲染：纯标准库生成静态 HTML，无前端构建步骤。"""
from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

CSS = """
:root{--bg:#f5f7fa;--card:#fff;--ink:#1f2937;--muted:#6b7280;
--line:#e5e7eb;--accent:#2563eb;--good:#059669;--warn:#d97706;--bad:#dc2626}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;font-size:14px}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 48px}
h1{font-size:24px;margin:0 0 6px}h2{font-size:16px;margin:28px 0 12px}
.sub{color:var(--muted);margin-bottom:22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;box-shadow:0 1px 2px rgba(0,0,0,.03)}
.card .n{font-size:26px;font-weight:650;letter-spacing:-.5px}
.card .t{color:var(--muted);font-size:12px;margin-top:4px}
.arch{background:#eef4ff;border:1px solid #d3e2ff;border-radius:10px;padding:16px 18px;
line-height:1.9;color:#1e3a8a}
.arch b{color:#1d4ed8}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);font-size:13px}
th{background:#f9fafb;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:none}
.tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;
background:#eef2ff;color:#4338ca}
.P1{background:#fee2e2;color:#b91c1c}.P2{background:#fef3c7;color:#b45309}
.P3{background:#e5e7eb;color:#374151}
.ok{color:var(--good)}.muted{color:var(--muted)}
footer{margin-top:32px;color:var(--muted);font-size:12px;line-height:1.8}
code{background:#eef2f7;padding:1px 5px;border-radius:4px}
"""


def _card(n, title):
    return f'<div class="card"><div class="n">{n:,}</div><div class="t">{title}</div></div>'


def render(repo, out_path=None) -> str:
    s = repo.stats()
    tickets = repo.list_tickets(size=20)["items"]
    reviews = repo.list_reviews(limit=10)
    devices = {d["id"]: d for d in repo.devices()}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cards = "".join([
        _card(s["devices"], "接入点位"),
        _card(s["events"], "巡检事件"),
        _card(s["tickets"], "累计工单"),
        _card(s["open_tickets"], "未闭环工单"),
        _card(s["duplicate_guard_hits"], "重复告警被去重"),
        _card(s["degraded_calls"], "AI 降级次数"),
    ])

    rows = "".join(
        "<tr><td>#{}</td><td>{}</td><td>{}</td>"
        '<td><span class="tag {}">{}</span></td><td>{}</td><td class="muted">{}</td></tr>'.format(
            t["id"],
            escape(devices.get(t["point_id"], {}).get("code", str(t["point_id"]))),
            escape(t["cls"]), t["priority"], t["priority"],
            escape(t["status"]), escape(t["sla_due_at"]))
        for t in tickets
    ) or '<tr><td colspan="6" class="muted">暂无工单，运行 <code>python bench.py</code> 生成压测数据</td></tr>'

    dist = "".join(
        f"<tr><td>{escape(k)}</td><td>{v:,}</td></tr>"
        for k, v in sorted(s["by_cls"].items(), key=lambda x: -x[1])
    ) or '<tr><td colspan="2" class="muted">暂无事件</td></tr>'

    rev = "".join(
        "<tr><td>#{}</td><td>{}</td><td>{}</td><td>{:.2f}</td></tr>".format(
            r["id"], escape(r["cls"]), r["status"], r["score"])
        for r in reviews
    ) or '<tr><td colspan="4" class="muted">复核队列为空</td></tr>'

    html = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>SmartInspect 巡检看板</title><style>" + CSS + "</style></head><body>"
        '<div class="wrap"><h1>园区智能巡检平台 · 运行看板</h1>'
        f'<div class="sub">生成时间 {now} ｜ 数据源：data/inspect.db ｜ '
        '检测器：StubDetector（合成帧，非真实模型权重）</div>'
        f'<div class="cards">{cards}</div>'
        '<h2>系统结构：传统业务系统为主，AI 为可降级模块</h2>'
        '<div class="arch"><b>传统层</b>：设备台账 · 巡检计划（防重跑） · 工单状态机 / SLA · '
        'RBAC + 行级数据权限 · 审计日志 · REST API / 看板<br>'
        '<b>AI 层</b>：可插拔 Detector（Stub / ONNX） → 阈值三档分流 → '
        '<b>点位+缺陷+时间桶+模型版本</b> 幂等去重 → 自动建单 / 低置信转人工复核<br>'
        '<b>降级</b>：推理超时或异常时标记 degraded，巡检与人工报修照常可用；'
        '复核判误报的事件导出为训练反馈，形成数据闭环。</div>'
        "<h2>最近工单</h2><table><thead><tr><th>ID</th><th>点位</th><th>缺陷类型</th>"
        "<th>优先级</th><th>状态</th><th>SLA 到期</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "<h2>缺陷类型分布</h2><table><thead><tr><th>类型</th><th>事件数</th></tr></thead>"
        f"<tbody>{dist}</tbody></table>"
        "<h2>待人工复核</h2><table><thead><tr><th>ID</th><th>类型</th>"
        f"<th>状态</th><th>置信度</th></tr></thead><tbody>{rev}</tbody></table>"
        "<footer>复现命令：<code>python bench.py --frames 2000</code> 重新压测并刷新本页；"
        "<code>python -m unittest discover -s tests -v</code> 跑单测。<br>"
        "所有数字来自本机真实运行结果，未接入真实 YOLO 权重，不声明检测精度。"
        "</footer></div></body></html>"
    )
    if out_path is not None:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")
    return html
