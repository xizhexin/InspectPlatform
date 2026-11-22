"""AI 阈值规则入口。实现统一放在顶层 config.py，避免 app/ai 之间循环依赖。"""
from config import load_rules, priority_of, route_of, sla_due

__all__ = ["load_rules", "route_of", "priority_of", "sla_due"]
