"""Tier4 写提案工具 —— M3_TIER4.md。

agent 只能**提案**（不直接改配置）：propose_change 创建一份变更提案、自动校验与
预演，返回提案 id；真正生效要人工 approve（/v1/changes/{id}/approve）。
"""
from app.agent.change_control import ChangeDeps, propose
from app.agent.tools.registry import ToolContext, ToolError


def _deps(ctx: ToolContext) -> ChangeDeps:
    if ctx.overrides is None or ctx.proposals is None:
        raise ToolError("变更控制未启用（缺 overrides/proposals 句柄）")
    return ChangeDeps(config=ctx.config, overrides=ctx.overrides,
                     telemetry=ctx.telemetry, redis=ctx.redis, store=ctx.proposals)


async def propose_change(ctx: ToolContext, field: str, value=None, rationale: str = "") -> dict:
    """提交一份配置变更提案（不直接生效，需人工批准）。"""
    p = await propose(_deps(ctx), field, value, rationale, proposer="agent")
    return {"id": p.id, "field": p.field, "value": p.value, "current": p.current,
            "valid": p.valid, "errors": p.errors, "predicted_effect": p.predicted_effect,
            "status": p.status, "note": "已创建提案，需人工 approve 才生效"}
