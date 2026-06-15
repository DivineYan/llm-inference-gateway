"""T4：② RateLimiter —— 令牌桶分层限流（429 rate_limited）。

用小阈值配置（burst=2）确定性触发：前 2 次放行，第 3 次拒绝。
机器按 svc 维度、人类按 user 维度，分别独立计额。
"""
CFG = "tests/configs/small_limits.yaml"


async def test_machine_rate_limited(make_client, flush_redis):
    c = await make_client(CFG)
    h = {"Authorization": "Bearer m"}
    body = {"model": "gpt", "input": "x"}

    assert (await c.post("/v1/infer", json=body, headers=h)).status_code == 200
    assert (await c.post("/v1/infer", json=body, headers=h)).status_code == 200
    r3 = await c.post("/v1/infer", json=body, headers=h)
    assert r3.status_code == 429
    assert r3.json()["reason"] == "rate_limited"
    assert "retry-after" in {k.lower() for k in r3.headers}


async def test_human_rate_limited(make_client, flush_redis):
    c = await make_client(CFG)
    h = {"Authorization": "Bearer h"}
    body = {"model": "gpt", "input": "x"}

    assert (await c.post("/v1/infer", json=body, headers=h)).status_code == 200
    assert (await c.post("/v1/infer", json=body, headers=h)).status_code == 200
    r3 = await c.post("/v1/infer", json=body, headers=h)
    assert r3.status_code == 429
    assert r3.json()["reason"] == "rate_limited"


async def test_dimensions_isolated(make_client, flush_redis):
    # 机器与人类各自独立计额：机器打满不影响人类
    c = await make_client(CFG)
    bm, bh = {"Authorization": "Bearer m"}, {"Authorization": "Bearer h"}
    body = {"model": "gpt", "input": "x"}

    for _ in range(3):
        await c.post("/v1/infer", json=body, headers=bm)
    # 机器已超限，人类仍应有额度
    assert (await c.post("/v1/infer", json=body, headers=bh)).status_code == 200
