"""T9：/health + /debug/trace 自测接口。"""
HI = {"Authorization": "Bearer key-search-machine"}


async def test_health_reports_backends_and_water_level(make_client, flush_redis):
    c = await make_client()
    r = await c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"

    names = {b["name"]: b["healthy"] for b in body["backends"]}
    assert names["gpt-a"] is True
    assert names["local-a"] is False  # 不健康后端如实呈现

    wl = body["water_level"]
    assert wl["inflight"] == 0
    assert wl["mode"] == "normal"
    assert wl["high_watermark"] == 10


async def test_debug_trace_returns_decision_log(make_client, flush_redis):
    c = await make_client()
    r = await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=HI)
    tid = r.json()["trace_id"]

    d = await c.get(f"/debug/trace/{tid}")
    assert d.status_code == 200
    body = d.json()
    gates = [e["gate"] for e in body["decision_log"]]
    assert gates == ["authenticator", "rate_limiter", "scheduler", "router", "dispatcher"]
    assert body["backend"] in {"gpt-a", "gpt-b"}


async def test_debug_trace_unknown_404(make_client, flush_redis):
    c = await make_client()
    r = await c.get("/debug/trace/nope")
    assert r.status_code == 404
