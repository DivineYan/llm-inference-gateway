"""遥测底座 —— M3 §4.3（Tier 0）。

把每个请求的摘要按时间存入 Redis，支持按窗口/维度聚合查询，作为
诊断/报表/建议工具的数据源。不追求完整时序库（那是 M4 的 Prometheus）。
"""
from app.telemetry.store import TelemetryStore, summarize

__all__ = ["TelemetryStore", "summarize"]
