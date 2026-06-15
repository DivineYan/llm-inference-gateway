"""T8：RequestContext 贯穿 + decision_log + 各段耗时。

直接持有 app，请求后从 trace 留存里取出上下文，校验决策链完整。
"""
from httpx import ASGITransport, AsyncClient

from app.context import RequestContext
from app.main import create_app
from app.observability import TraceStore

HI = {"Authorization": "Bearer key-search-machine"}


def test_trace_store_capacity_eviction():
    s = TraceStore(capacity=2)
    for i in range(3):
        s.add(RequestContext(requested_model="gpt", input="x", credential="c", trace_id=f"t{i}"))
    assert s.get("t0") is None  # 最旧被淘汰
    assert s.get("t1") is not None
    assert s.get("t2") is not None


async def test_decision_log_records_each_gate(flush_redis):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=HI)
    ctx = app.state.traces.get(r.json()["trace_id"])

    gates = [d.gate for d in ctx.decision_log]
    assert gates == ["authenticator", "rate_limiter", "scheduler", "router", "dispatcher"]
    assert all(d.allowed for d in ctx.decision_log)
    assert all(d.elapsed_ms >= 0 for d in ctx.decision_log)


async def test_decision_log_on_rejection(flush_redis):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # 错误凭证 → 在 authenticator 处拦截
        r = await c.post(
            "/v1/infer",
            json={"model": "gpt", "input": "x"},
            headers={"Authorization": "Bearer wrong"},
        )
    ctx = app.state.traces.get(r.json()["trace_id"])
    last = ctx.decision_log[-1]
    assert last.gate == "authenticator"
    assert last.allowed is False
    assert last.reason == "unauthenticated"
