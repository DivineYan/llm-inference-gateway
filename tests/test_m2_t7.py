"""M2-T7：观测 —— /health 展示熔断状态，/debug/trace 展示保障决策。"""
SAFEGUARD = "tests/configs/safeguard.yaml"
CIRCUIT_LOW = "tests/configs/circuit_low.yaml"
H = {"Authorization": "Bearer m"}


async def test_health_shows_circuit_closed_by_default(make_client, flush_redis):
    c = await make_client(SAFEGUARD)
    h = await c.get("/health")
    assert h.status_code == 200
    for b in h.json()["backends"]:
        assert b["circuit"] == "closed"   # 无故障记录时全闭合


async def test_health_shows_circuit_open_after_trips(make_client, flush_redis):
    c = await make_client(CIRCUIT_LOW)
    for _ in range(3):                     # 3 次失败 → 跳闸（min_samples=3）
        await c.post("/v1/infer", json={"model": "solo", "input": "x"}, headers=H)
    h = await c.get("/health")
    states = {b["name"]: b["circuit"] for b in h.json()["backends"]}
    assert states["solo-bad"] == "open"


async def test_debug_trace_shows_safeguard_decisions(make_client, flush_redis):
    c = await make_client(SAFEGUARD)
    r = await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=H)
    t = await c.get(f"/debug/trace/{r.json()['trace_id']}")
    body = t.json()
    assert body["degraded"] is True
    assert body["fallback_model"] == "claude"
    # 保障决策链：gpt-bad 失败 → 降级 → claude-ok 成功
    outcomes = [a["outcome"] for a in body["attempts"]]
    assert outcomes == ["failed", "degrade_to", "ok"]
