"""工具统一清单 —— 一处维护所有工具的 name/描述/参数schema/handler（M3 §4.2）。

handler 逻辑仍分布在 builtin/advisory/write（按主题），但**注册与 schema 全部集中在
此处**：新增/改工具只动这一张表，方便维护与审阅。schema 形状对齐真实 LLM tool 定义。

为什么不是 JSON：handler 是 Python 可调用对象，JSON 放不下；纯 JSON 会逼出一个
额外的"名字→函数"映射、又散成两处。单一 .py 清单让 schema 与 handler 并排。

未来接外部 MCP server 的工具：在 CATALOG 追加条目（或动态扩展）注册进同一 registry。
"""
from app.agent.tools.advisory import ratelimit_advisory, weight_rebalance_advisory
from app.agent.tools.builtin import (
    get_backend_health,
    get_config,
    query_metrics,
    query_traces,
    query_usage,
    render_report,
)
from app.agent.tools.registry import Tool, ToolContext, ToolRegistry
from app.agent.tools.write import propose_change

# 每个工具：(name, description, JSON-schema 参数, handler)
CATALOG: list[Tool] = [
    # ── 只读观测/报表（Tier 1/2）──────────────────────────
    Tool(
        "query_metrics", 
        "按窗口/维度聚合指标(count/error_rate/延迟/outcome分布)",
         {"type": "object", 
          "properties": {
             "window_seconds": {"type": "number", "description": "聚合窗口秒数"},
             "group_by": {"type": "string", "enum": ["caller", "model", "backend", "outcome"]},
         }}, 
         query_metrics),

    Tool(
        "query_traces", 
        "捞最近请求摘要，可按 outcome 过滤",
        {"type": "object", 
         "properties": {
            "window_seconds": {"type": "number"},
            "outcome": {"type": "string"},
            "limit": {"type": "integer"},
        }}, 
        query_traces),

    Tool(
        "get_backend_health", 
        "各后端熔断状态/healthy + 当前在途水位",
        {"type": "object", 
         "properties": {}}, 
        get_backend_health),

    Tool(
        "query_usage", 
        "按调用方/模型聚合用量(次数+token)",
        {"type": "object", 
        "properties": {
            "window_seconds": {"type": "number"},
            "group_by": {
                "type": "string", 
                "enum": ["caller", "model", "backend"]
                },
        }}, 
        query_usage),
         
    Tool(
        "get_config", 
        "读当前配置(剔除凭证)",
        {"type": "object", 
        "properties": {
            "section": {"type": "string",
                        "enum": ["backends", "callers", "thresholds", "safeguard"]},
        }}, 
        get_config),

    Tool(
        "render_report", 
        "把 sections 渲染成 markdown 报表",
        {"type": "object", 
         "properties": {
             "title": {"type": "string"},
             "sections": {"type": "array"},
        }, 
        "required": ["title", "sections"]}, 
        render_report),

    # ── 优化建议（Tier 3，只读）───────────────────────────
    Tool(
        "weight_rebalance_advisory", 
        "熔断中的后端给出降权建议(只读)",
         {"type": "object", 
          "properties": {"window_seconds": {"type": "number"}}},
        weight_rebalance_advisory),

    Tool(
        "ratelimit_advisory", 
        "被限流的调用方给出提额/排查建议(只读)",
         {"type": "object", 
          "properties": {"window_seconds": {"type": "number"}}},
         ratelimit_advisory),
         
    # ── 写提案（Tier 4，只提案、需人工 approve）────────────
    Tool("propose_change",
         "提交一份配置变更提案(只提案、需人工批准)。field 形如 "
         "backend:{name}:weight / caller:{id}:rate_per_sec / thresholds:high_watermark / "
         "reset_circuit:{name}",
         {"type": "object", "properties": {
             "field": {"type": "string"},
             "value": {},
             "rationale": {"type": "string"},
         }, "required": ["field"]}, propose_change),
]


def build_default_registry(context: ToolContext) -> ToolRegistry:
    """按 CATALOG 注册全部工具。"""
    reg = ToolRegistry(context)
    for tool in CATALOG:
        reg.register(tool)
    return reg
