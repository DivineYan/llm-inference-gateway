"""M3-T7：Tier3 优化建议（只读）。

- weight_rebalance：gpt-a 熔断 → 建议把 gpt-a 降权。
- ratelimit：调用方被限流 → 建议复核/提额。
两者只读，不改任何配置。
"""
H = {"Authorization": "Bearer m"}


async def test_weight_rebalance_advisory(make_client, flush_redis):
    c = await make_client("tests/configs/diag.yaml")
    for _ in range(8):  # gpt-a(weight3) 必失败 → 熔断打开
        await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=H)
    reg = c._transport.app.state.tools

    adv = await reg.execute("weight_rebalance_advisory", {})
    recs = {r["backend"]: r for r in adv["recommendations"]}
    assert "gpt-a" in recs
    assert recs["gpt-a"]["recommendation"] == "lower_weight"
    assert recs["gpt-a"]["current_weight"] == 3
    assert recs["gpt-a"]["suggested_weight"] == 1


async def test_ratelimit_advisory(make_client, flush_redis):
    c = await make_client("tests/configs/small_limits.yaml")
    # 1/s、burst 2：连发 6 次 → 后续被限流
    seen_429 = False
    for _ in range(6):
        r = await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=H)
        seen_429 = seen_429 or r.status_code == 429
    assert seen_429
    reg = c._transport.app.state.tools

    adv = await reg.execute("ratelimit_advisory", {})
    recs = {r["caller"]: r for r in adv["recommendations"]}
    assert "svc:test" in recs
    assert recs["svc:test"]["rate_limited_count"] >= 1
    assert recs["svc:test"]["current_rate_per_sec"] == 1


async def test_advisory_registered_in_tools_endpoint(client):
    r = await client.get("/v1/tools")
    names = {t["name"] for t in r.json()["tools"]}
    assert {"weight_rebalance_advisory", "ratelimit_advisory"} <= names
