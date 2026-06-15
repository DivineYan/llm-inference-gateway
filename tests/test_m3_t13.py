"""M3-T13（Tier4 Step B）：写操作护栏端到端。

propose → validate → 人工 approve → apply（生效到运行时覆盖层）→ audit。
覆盖：生效、幂等、非法提案被拦、鉴权、驳回、冷却、reset_circuit、propose_change 工具。
"""
HI = {"Authorization": "Bearer key-search-machine"}
M = {"Authorization": "Bearer m"}


def _app(c):
    return c._transport.app


async def test_propose_approve_applies_and_idempotent(make_client, flush_redis):
    c = await make_client()
    r = await c.post("/v1/changes", headers=HI,
                     json={"field": "backend:gpt-a:weight", "value": 1, "rationale": "降权"})
    assert r.status_code == 200
    p = r.json()
    assert p["valid"] is True and p["status"] == "proposed" and p["current"] == 3

    # 批准前不生效
    assert (await c.get("/health")).json()["backends"][0]["weight"] == 3
    a = await c.post(f"/v1/changes/{p['id']}/approve", headers=HI)
    assert a.json()["status"] == "applied"
    # 生效到运行时覆盖层
    by_name = {b["name"]: b for b in (await c.get("/health")).json()["backends"]}
    assert by_name["gpt-a"]["weight"] == 1
    # 幂等：再次 approve 仍 applied
    a2 = await c.post(f"/v1/changes/{p['id']}/approve", headers=HI)
    assert a2.json()["status"] == "applied"
    # 审计留痕
    assert [h["event"] for h in a2.json()["history"]] == ["proposed", "approved+applied"]


async def test_invalid_proposal_blocked(make_client, flush_redis):
    c = await make_client()
    # 先把 gpt-b 摘掉（合法，gpt-a 仍在）
    r1 = await c.post("/v1/changes", headers=HI,
                      json={"field": "backend:gpt-b:healthy", "value": False})
    await c.post(f"/v1/changes/{r1.json()['id']}/approve", headers=HI)
    # 再摘 gpt-a → 会搞死 gpt 模型 → 非法
    r2 = await c.post("/v1/changes", headers=HI,
                      json={"field": "backend:gpt-a:healthy", "value": False})
    assert r2.json()["valid"] is False
    assert any("无可用后端" in e for e in r2.json()["errors"])
    # 非法提案不能批准
    a = await c.post(f"/v1/changes/{r2.json()['id']}/approve", headers=HI)
    assert a.status_code == 409


async def test_change_requires_auth(make_client, flush_redis):
    c = await make_client()
    assert (await c.post("/v1/changes",
            json={"field": "backend:gpt-a:weight", "value": 1})).status_code == 401


async def test_reject_then_cannot_approve(make_client, flush_redis):
    c = await make_client()
    p = (await c.post("/v1/changes", headers=HI,
         json={"field": "thresholds:high_watermark", "value": 20})).json()
    rj = await c.post(f"/v1/changes/{p['id']}/reject", headers=HI)
    assert rj.json()["status"] == "rejected"
    assert (await c.post(f"/v1/changes/{p['id']}/approve", headers=HI)).status_code == 409


async def test_cooldown_blocks_repeat(make_client, flush_redis):
    c = await make_client()
    p = (await c.post("/v1/changes", headers=HI,
         json={"field": "backend:gpt-a:weight", "value": 1})).json()
    await c.post(f"/v1/changes/{p['id']}/approve", headers=HI)
    # 同字段冷却期内再提案 → valid False
    p2 = (await c.post("/v1/changes", headers=HI,
          json={"field": "backend:gpt-a:weight", "value": 2})).json()
    assert p2["valid"] is False
    assert any("冷却" in e for e in p2["errors"])


async def test_reset_circuit_clears_state(make_client, flush_redis):
    c = await make_client("tests/configs/diag.yaml")
    for _ in range(8):  # gpt-a 失败 → 熔断打开
        await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=M)
    states = {b["name"]: b["circuit"] for b in (await c.get("/health")).json()["backends"]}
    assert states["gpt-a"] == "open"

    p = (await c.post("/v1/changes", headers=M,
         json={"field": "reset_circuit:gpt-a", "rationale": "确认已恢复"})).json()
    await c.post(f"/v1/changes/{p['id']}/approve", headers=M)
    states = {b["name"]: b["circuit"] for b in (await c.get("/health")).json()["backends"]}
    assert states["gpt-a"] == "closed"   # 熔断态被清


async def test_propose_change_tool(make_client, flush_redis):
    c = await make_client()
    out = await _app(c).state.tools.execute(
        "propose_change", {"field": "backend:gpt-a:weight", "value": 1, "rationale": "agent 建议"})
    assert out["valid"] is True and out["status"] == "proposed"
    # 工具只提案，不生效
    assert (await c.get("/health")).json()["backends"][0]["weight"] == 3
