"""M3-T2：Tier0 遥测底座 —— 记录 + 窗口/维度聚合。

单元层：直接喂摘要给 TelemetryStore，验证窗口过滤与聚合指标。
集成层：经 /v1/infer 真实产生摘要，验证能聚合出 count/error_rate/outcome。
"""
import itertools

import pytest_asyncio

from app.telemetry import TelemetryStore

HI = {"Authorization": "Bearer key-search-machine"}
_seq = itertools.count()


@pytest_asyncio.fixture
async def store(flush_redis):
    return TelemetryStore(flush_redis)


def _row(ts, outcome="ok", total_ms=10.0, caller="svc:search", backend="gpt-a"):
    return {
        "trace_id": f"t{next(_seq)}", "ts": ts, "caller": caller, "model": "gpt",
        "backend": backend, "outcome": outcome, "total_ms": total_ms,
        "degraded": False, "served_fallback": outcome == "served_fallback",
        "input_tokens": 5, "output_tokens": 3,
    }


async def test_window_filters_old_rows(store):
    now = 1000.0
    await store.record(_row(now - 5))      # 窗口内
    await store.record(_row(now - 50))     # 窗口外
    rows = await store.query(window_seconds=10, now=now)
    assert len(rows) == 1
    assert rows[0]["ts"] == now - 5


async def test_aggregate_overall_metrics(store):
    now = 1000.0
    for i in range(8):
        await store.record(_row(now - i, total_ms=float(i)))
    await store.record(_row(now - 1, outcome="rate_limited"))
    await store.record(_row(now - 1, outcome="served_fallback"))

    agg = (await store.aggregate(window_seconds=60, now=now))["_all"]
    assert agg["count"] == 10
    assert agg["outcomes"]["ok"] == 8
    assert agg["outcomes"]["rate_limited"] == 1
    assert agg["error_rate"] == round(2 / 10, 4)
    assert agg["latency_p99"] >= agg["latency_p50"]


async def test_aggregate_group_by_outcome(store):
    now = 1000.0
    await store.record(_row(now, outcome="ok"))
    await store.record(_row(now, outcome="preempted"))
    await store.record(_row(now, outcome="preempted"))
    agg = await store.aggregate(window_seconds=60, group_by="outcome", now=now)
    assert agg["ok"]["count"] == 1
    assert agg["preempted"]["count"] == 2


async def test_infer_feeds_telemetry(make_client, flush_redis):
    c = await make_client()
    for _ in range(5):
        r = await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=HI)
        assert r.status_code == 200
    store = TelemetryStore(flush_redis)
    agg = (await store.aggregate(window_seconds=60))["_all"]
    assert agg["count"] == 5
    assert agg["outcomes"].get("ok") == 5
