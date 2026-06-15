"""M3-T12（Tier4 Step A2-A4）：网关读取走运行时覆盖层。

A2 Router：weight 覆盖改变命中分布；healthy 覆盖摘除后端；全摘 → 503。
A3 RateLimiter：rate 覆盖即时收紧/放宽。
A4 Scheduler：watermark 覆盖即时改变抢占线。
"""
HI = {"Authorization": "Bearer key-search-machine"}


def _app(c):
    return c._transport.app


async def _hits(c, model, n):
    counts = {}
    for _ in range(n):
        r = await c.post("/v1/infer", json={"model": model, "input": "x"}, headers=HI)
        if r.status_code == 200:
            b = r.json()["backend"]
            counts[b] = counts.get(b, 0) + 1
    return counts


# ── A2 Router 权重覆盖 ───────────────────────────────────
async def test_weight_override_changes_distribution(make_client, flush_redis):
    c = await make_client()  # 默认 gpt-a:gpt-b = 3:1
    await _app(c).state.overrides.set("backend:gpt-a:weight", 1)  # 改成 1:1
    counts = await _hits(c, "gpt", 40)
    assert counts.get("gpt-a", 0) + counts.get("gpt-b", 0) == 40
    assert 15 <= counts.get("gpt-a", 0) <= 25   # 不再是 3:1
    assert 15 <= counts.get("gpt-b", 0) <= 25


async def test_healthy_override_removes_backend(make_client, flush_redis):
    c = await make_client()
    await _app(c).state.overrides.set("backend:gpt-a:healthy", False)
    counts = await _hits(c, "gpt", 12)
    assert counts == {"gpt-b": 12}   # gpt-a 被摘除，全打 gpt-b


async def test_all_backends_unhealthy_503(make_client, flush_redis):
    c = await make_client()
    await _app(c).state.overrides.set("backend:gpt-a:healthy", False)
    await _app(c).state.overrides.set("backend:gpt-b:healthy", False)
    r = await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=HI)
    assert r.status_code == 503
    assert r.json()["reason"] == "no_backend"


# ── A3 RateLimiter 限流覆盖 ──────────────────────────────
async def test_ratelimit_override_tightens(make_client, flush_redis):
    c = await make_client()  # svc:search 默认 rate 50 / burst 50
    await _app(c).state.overrides.set("caller:svc:search:rate_per_sec", 1)
    await _app(c).state.overrides.set("caller:svc:search:burst", 2)
    # 收紧到 burst 2 → 连发很快触发 429
    codes = [(await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=HI)).status_code
             for _ in range(6)]
    assert 429 in codes


# ── A4 Scheduler 水位覆盖 + /health ──────────────────────
async def test_health_surfaces_overrides(make_client, flush_redis):
    c = await make_client()
    await _app(c).state.overrides.set("backend:gpt-a:weight", 1)
    await _app(c).state.overrides.set("thresholds:high_watermark", 15)
    h = (await c.get("/health")).json()
    by_name = {b["name"]: b for b in h["backends"]}
    assert by_name["gpt-a"]["weight"] == 1                  # 有效值
    assert h["water_level"]["high_watermark"] == 15
    assert h["overrides"]["thresholds:high_watermark"] == 15  # 活跃覆盖列表
