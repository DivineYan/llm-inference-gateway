"""M2-T6：保障层端到端接入网关（M2 §3）。

经 /v1/infer 走完整流水线 ①→⑤（⑤ 为保障执行器），验证降级/failover/兜底
在真实请求路径上的表现与响应标记。circuit min_samples 调高，避免误跳闸干扰。
"""
CFG = "tests/configs/safeguard.yaml"
H = {"Authorization": "Bearer m"}


async def test_normal_success_not_degraded(make_client, flush_redis):
    c = await make_client(CFG)
    r = await c.post("/v1/infer", json={"model": "claude", "input": "x"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "claude-ok"
    assert body["degraded"] is False


async def test_failover_within_same_model(make_client, flush_redis):
    # ha-a 故障 → 同模型顶到 ha-b，成功但不算降级
    c = await make_client(CFG)
    r = await c.post("/v1/infer", json={"model": "ha", "input": "x"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "ha-b"
    assert body["degraded"] is False


async def test_degrade_to_backup_model(make_client, flush_redis):
    # gpt 唯一后端故障 → 降级到备用模型 claude
    c = await make_client(CFG)
    r = await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True
    assert body["fallback_model"] == "claude"
    assert body["backend"] == "claude-ok"


async def test_canned_fallback_503(make_client, flush_redis):
    # down 唯一后端故障且无备用 → 503 served_fallback + 兜底文本
    c = await make_client(CFG)
    r = await c.post("/v1/infer", json={"model": "down", "input": "x"}, headers=H)
    assert r.status_code == 503
    body = r.json()
    assert body["reason"] == "served_fallback"
    assert body["degraded"] is True
    assert body["output"] == "兜底响应"
    assert body["backend"] is None
