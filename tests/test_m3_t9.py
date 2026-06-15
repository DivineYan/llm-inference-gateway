"""M3-T9（H14）：agent 模型调用受 M1 治理。

- 鉴权：agent/skill 端点无凭证 → 401。
- 用量归属：agent 的模型调用记进遥测，source=agent，归属到发起调用方。
- 限流：调用方额度耗尽后，agent 的模型调用被限流 → 429（不再绕过网关消耗后端）。
"""
from app.telemetry import TelemetryStore

HI = {"Authorization": "Bearer key-search-machine"}
M = {"Authorization": "Bearer m"}


async def test_agent_requires_auth(make_client, flush_redis):
    c = await make_client()
    assert (await c.post("/v1/agent", json={"goal": "x"})).status_code == 401
    assert (await c.post("/v1/skills/usage_report/run", json={})).status_code == 401


async def test_agent_usage_attributed_to_caller(make_client, flush_redis):
    c = await make_client()
    r = await c.post("/v1/agent", json={"goal": "诊断", "task_id": "g1"}, headers=HI)
    assert r.json()["status"] == "success"

    store = TelemetryStore(flush_redis)
    rows = await store.query(window_seconds=60)
    agent_rows = [x for x in rows if x.get("source") == "agent"]
    assert agent_rows                                   # agent 用量可见
    assert all(x["caller"] == "svc:search" for x in agent_rows)  # 归属到发起方


async def test_agent_model_call_is_rate_limited(make_client, flush_redis):
    c = await make_client("tests/configs/small_limits.yaml")  # rate 1 / burst 2
    # 先用直连推理耗尽 svc:test 的额度
    drained = False
    for _ in range(4):
        r = await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=M)
        drained = drained or r.status_code == 429
    assert drained
    # 此时 agent 的模型调用也应被同一调用方维度限流 → 429
    r = await c.post("/v1/agent", json={"goal": "诊断", "task_id": "ag1"}, headers=M)
    assert r.status_code == 429
