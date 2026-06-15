"""M2-T8：熔断状态多实例全局一致 —— PRD §11 / M2 §10 关键证据。

两个独立 app 实例（各自独立进程内状态）共享同一 Redis。实例 A 把某后端熔断
打开后，实例 B 应立刻也看到 open 并对该后端快速失败（不打后端）——证明熔断
状态在 Redis 而非进程内存，多实例一致（FR-4.4 / NFR-7）。

真·两进程版本同理可经 scripts/multi_instance_demo.py 扩展。
"""
CIRCUIT_LOW = "tests/configs/circuit_low.yaml"
H = {"Authorization": "Bearer m"}
BODY = {"model": "solo", "input": "x"}


async def test_circuit_open_visible_across_instances(make_client, flush_redis):
    a = await make_client(CIRCUIT_LOW)
    b = await make_client(CIRCUIT_LOW)

    for _ in range(3):                     # A 上 3 次失败 → 在 Redis 跳闸
        await a.post("/v1/infer", json=BODY, headers=H)

    # B 是独立实例，仅共享 Redis，应立刻看到 solo-bad 已 open
    states = {x["name"]: x["circuit"] for x in (await b.get("/health")).json()["backends"]}
    assert states["solo-bad"] == "open"


async def test_open_circuit_fast_fails_on_other_instance(make_client, flush_redis):
    a = await make_client(CIRCUIT_LOW)
    b = await make_client(CIRCUIT_LOW)

    for _ in range(3):
        await a.post("/v1/infer", json=BODY, headers=H)

    # B 的下一个请求快速失败：attempts 标 circuit_open，证明 B 没有打后端
    r = await b.post("/v1/infer", json=BODY, headers=H)
    assert r.status_code == 503
    t = await b.get(f"/debug/trace/{r.json()['trace_id']}")
    assert [x["outcome"] for x in t.json()["attempts"]] == ["circuit_open"]
