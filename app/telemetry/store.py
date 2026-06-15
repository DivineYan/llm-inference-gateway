"""遥测存储与聚合 —— M3 §4.3（Tier 0）。

存储：每条请求摘要存进 Redis sorted set，score = 时间戳，超出保留窗口滚动清理。
查询：按时间窗取原始摘要；按维度（caller/model/backend/outcome）聚合出
count / error_rate / 延迟分位 / outcome 分布 / token——直接喂给诊断与报表工具。

now 可注入，便于测试用确定时间窗而不依赖真实时钟。
"""
import json
import time

from app.context import RequestContext

TELEMETRY_KEY = "telemetry:traces"
RETENTION_SECONDS = 3600  # 默认保留最近 1 小时


def summarize(ctx: RequestContext) -> dict:
    """从 RequestContext 抽一条扁平摘要。outcome = 拒绝原因，否则 ok。"""
    outcome = "ok"
    for d in ctx.decision_log:
        if not d.allowed:
            outcome = d.reason
            break
    if ctx.served_fallback:
        outcome = "served_fallback"
    output = (ctx.result or {}).get("output") or ""
    return {
        "trace_id": ctx.trace_id,
        "ts": time.time(),
        "caller": ctx.caller_profile.caller_id if ctx.caller_profile else None,
        "model": ctx.requested_model,
        "backend": ctx.chosen_backend.name if ctx.chosen_backend else None,
        "outcome": outcome,
        "total_ms": round(sum(d.elapsed_ms for d in ctx.decision_log), 3),
        "degraded": ctx.degraded,
        "served_fallback": ctx.served_fallback,
        "input_tokens": len(ctx.input or "") // 4,   # token 占位估算（接真模型后用真值）
        "output_tokens": len(output) // 4,
    }


def _percentile(values: list[float], p: float) -> float:
    """线性插值分位数。空列表返回 0。"""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 3)


def _metrics(rows: list[dict]) -> dict:
    """对一组摘要算指标。error = outcome != ok。"""
    n = len(rows)
    lat = [r["total_ms"] for r in rows]
    outcomes: dict[str, int] = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    ok = outcomes.get("ok", 0)
    return {
        "count": n,
        "error_rate": round((n - ok) / n, 4) if n else 0.0,
        "latency_p50": _percentile(lat, 0.50),
        "latency_p95": _percentile(lat, 0.95),
        "latency_p99": _percentile(lat, 0.99),
        "outcomes": outcomes,
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
    }


class TelemetryStore:
    def __init__(self, redis, retention_seconds: int = RETENTION_SECONDS):
        self.redis = redis
        self.retention = retention_seconds

    async def record(self, summary: dict) -> None:
        ts = summary["ts"]
        await self.redis.zadd(TELEMETRY_KEY, {json.dumps(summary): ts})
        await self.redis.zremrangebyscore(TELEMETRY_KEY, 0, ts - self.retention)

    async def query(self, window_seconds: float, now: float | None = None) -> list[dict]:
        """取最近 window_seconds 内的原始摘要。"""
        now = time.time() if now is None else now
        rows = await self.redis.zrangebyscore(TELEMETRY_KEY, now - window_seconds, now)
        return [json.loads(r) for r in rows]

    async def aggregate(
        self, window_seconds: float, group_by: str | None = None, now: float | None = None
    ) -> dict:
        """按维度聚合。group_by ∈ {caller, model, backend, outcome}；None 为全局。"""
        rows = await self.query(window_seconds, now)
        groups: dict[str, list[dict]] = {}
        for r in rows:
            key = r.get(group_by) if group_by else "_all"
            groups.setdefault(str(key), []).append(r)
        return {k: _metrics(v) for k, v in groups.items()}
