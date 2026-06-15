"""T6：④ Router —— 加权轮询命中分布 + 无后端 503。

默认配置 gpt 有两后端 gpt-a:gpt-b = 3:1。SWRR 命中比例应稳定逼近权重。
local 模型仅有不健康后端 → 503 no_backend。
"""
HI = {"Authorization": "Bearer key-search-machine"}  # 高优先级，rate 50/burst 50


async def test_weighted_distribution(make_client, flush_redis):
    c = await make_client()  # 默认 config.yaml
    counts = {}
    for _ in range(40):
        r = await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=HI)
        assert r.status_code == 200
        b = r.json()["backend"]
        counts[b] = counts.get(b, 0) + 1

    # 权重 3:1 → 40 次约 30/10。SWRR 确定性，给小容差。
    assert counts.get("gpt-a", 0) + counts.get("gpt-b", 0) == 40
    assert 27 <= counts.get("gpt-a", 0) <= 33
    assert 7 <= counts.get("gpt-b", 0) <= 13


async def test_no_backend_503(make_client, flush_redis):
    c = await make_client()
    # svc:search 允许 local，但 local 唯一后端不健康
    r = await c.post("/v1/infer", json={"model": "local", "input": "x"}, headers=HI)
    assert r.status_code == 503
    assert r.json()["reason"] == "no_backend"
