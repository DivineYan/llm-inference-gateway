"""内置只读工具 —— M3 §4.2。

读本平台 telemetry/config/health，供诊断(ReAct)与报表(Workflow)使用。
全部只读：不改任何配置或状态（M3 不做 Tier4 写操作）。
"""
import time
from typing import Any

from app.agent.tools.registry import ToolContext
from app.gates.scheduler import Scheduler
from app.inflight import INFLIGHT_KEY, MAX_REQUEST_TTL

DEFAULT_WINDOW = 300  # 默认聚合窗口（秒）


async def query_metrics(ctx: ToolContext, window_seconds: float = DEFAULT_WINDOW,
                        group_by: str | None = None) -> dict:
    """按窗口/维度聚合指标：count/error_rate/延迟分位/outcome 分布/token。"""
    return await ctx.telemetry.aggregate(window_seconds, group_by)


async def query_traces(ctx: ToolContext, window_seconds: float = DEFAULT_WINDOW,
                       outcome: str | None = None, limit: int = 20) -> list[dict]:
    """捞最近的请求摘要；可按 outcome 过滤（如只看 served_fallback/preempted）。"""
    rows = await ctx.telemetry.query(window_seconds)
    if outcome:
        rows = [r for r in rows if r["outcome"] == outcome]
    return rows[-limit:]


async def get_backend_health(ctx: ToolContext) -> dict:
    """各后端熔断状态/healthy + 当前在途水位（与 /health 同源）。"""
    await ctx.redis.zremrangebyscore(INFLIGHT_KEY, 0, time.time() - MAX_REQUEST_TTL)
    inflight = await ctx.redis.zcard(INFLIGHT_KEY)
    mode = await ctx.redis.get(Scheduler.MODE_KEY) or "normal"
    return {
        "backends": [
            {
                "name": b.name,
                "model": b.model,
                "healthy": b.healthy,
                "circuit": await ctx.circuit.state(ctx.redis, b.name),
            }
            for b in ctx.config.backends
        ],
        "water_level": {
            "inflight": inflight,
            "high_watermark": ctx.config.thresholds.high_watermark,
            "low_watermark": ctx.config.thresholds.low_watermark,
            "mode": mode,
        },
    }


async def query_usage(ctx: ToolContext, window_seconds: float = 3600,
                      group_by: str = "caller") -> dict:
    """按调用方/部门/模型聚合用量：调用次数 + token。"""
    agg = await ctx.telemetry.aggregate(window_seconds, group_by)
    return {
        k: {
            "count": v["count"],
            "input_tokens": v["input_tokens"],
            "output_tokens": v["output_tokens"],
        }
        for k, v in agg.items()
    }


async def get_config(ctx: ToolContext, section: str | None = None) -> Any:
    """读当前配置（剔除凭证）。section ∈ {backends, callers, thresholds, safeguard}。"""
    sections = {
        "backends": [b.model_dump() for b in ctx.config.backends],
        "callers": [c.model_dump() for c in ctx.config.callers],  # 画像不含凭证
        "thresholds": ctx.config.thresholds.model_dump(),
        "safeguard": ctx.config.safeguard.model_dump(),
    }
    return sections.get(section) if section else sections


async def render_report(ctx: ToolContext, title: str, sections: list[dict],
                        fmt: str = "markdown") -> str:
    """把 [{heading, body}] 渲染成 markdown 报表。body 为 str 或可序列化对象。"""
    lines = [f"# {title}", ""]
    for s in sections:
        lines.append(f"## {s.get('heading', '')}")
        body = s.get("body", "")
        lines.append(body if isinstance(body, str) else f"```\n{body}\n```")
        lines.append("")
    return "\n".join(lines)
