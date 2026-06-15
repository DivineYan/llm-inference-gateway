"""优化建议工具 —— M3 §4.6（Tier 3，只读）。

只产出"建议 + 依据"，不写任何配置（自动落地 = Tier 4，留作后续）。
- weight_rebalance_advisory：熔断中的后端建议降权，把流量转给健康同模型后端
  （呼应 M1 故意推迟的动态调权 P1）。信号取自熔断状态（比 telemetry 的
  serving-backend 更准——失败后端不会成为 serving backend）。
- ratelimit_advisory：被限流的调用方给出提额/降额提示，依据近窗口 outcome 分布。
"""
from app.agent.tools.builtin import get_backend_health
from app.agent.tools.registry import ToolContext

ERROR_HINT_THRESHOLD = 0.3


async def weight_rebalance_advisory(ctx: ToolContext, window_seconds: float = 300) -> dict:
    """熔断中的多权重后端 → 建议降权。"""
    health = await get_backend_health(ctx)
    weights = {b.name: b.weight for b in ctx.config.backends}
    recs = []
    for b in health["backends"]:
        w = weights.get(b["name"], 1)
        if b["circuit"] in ("open", "half_open") and w > 1:
            recs.append({
                "backend": b["name"],
                "model": b["model"],
                "current_weight": w,
                "circuit": b["circuit"],
                "recommendation": "lower_weight",
                "suggested_weight": 1,
                "rationale": f"{b['name']} 熔断({b['circuit']})，建议权重 {w}→1，"
                             f"将流量转给同模型健康后端",
            })
    return {"recommendations": recs, "window_seconds": window_seconds}


async def ratelimit_advisory(ctx: ToolContext, window_seconds: float = 300) -> dict:
    """被限流的调用方 → 提额/排查提示。"""
    by_caller = await ctx.telemetry.aggregate(window_seconds, group_by="caller")
    rates = {c.caller_id: c.rate_limit for c in ctx.config.callers}
    recs = []
    for caller, m in by_caller.items():
        rl = m["outcomes"].get("rate_limited", 0)
        if rl > 0:
            cur = rates.get(caller)
            recs.append({
                "caller": caller,
                "rate_limited_count": rl,
                "current_rate_per_sec": cur.rate_per_sec if cur else None,
                "recommendation": "review_rate_limit",
                "rationale": f"{caller} 近窗口被限流 {rl} 次；若为关键业务可考虑提额，"
                             f"否则确认是否客户端异常重试",
            })
    return {"recommendations": recs, "window_seconds": window_seconds}
