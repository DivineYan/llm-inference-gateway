"""M3-T14（Tier5 渐进自治）：Autopilot 自监测→窄白名单自批→自动回滚/升级。

- 主路径：持续熔断的后端被自动摘除（healthy=false），需连续 sustained 周期。
- 不搞死模型：单后端模型熔断 → 提案非法 → 不自批 → 升级给人。
- 防反复：已摘除的后端跳过。
- 自动回滚/保留：apply 后 error_rate 变差 → 回滚；改善/持平 → 保留。
- 端点需鉴权。
"""
import itertools

from app.agent.autopilot import Autopilot
from app.agent.change_control import ChangeDeps

M = {"Authorization": "Bearer m"}
_seq = itertools.count()


def _deps(app):
    return ChangeDeps(config=app.state.config, overrides=app.state.overrides,
                     telemetry=app.state.telemetry, redis=app.state.redis,
                     store=app.state.proposals)


def _row(ts, outcome, model="gpt"):
    return {"trace_id": f"t{next(_seq)}", "ts": ts, "caller": "svc:test", "model": model,
            "backend": "x", "outcome": outcome, "total_ms": 1.0, "degraded": False,
            "served_fallback": outcome == "served_fallback", "input_tokens": 1, "output_tokens": 1}


async def _open_circuit(c, model, headers, n=8):
    for _ in range(n):
        await c.post("/v1/infer", json={"model": model, "input": "x"}, headers=headers)


# ── 主路径：自动摘除持续熔断的坏后端 ─────────────────────
async def test_autopilot_drains_sustained_open_backend(make_client, flush_redis):
    c = await make_client("tests/configs/diag.yaml")  # gpt-a 必失败、gpt-b 正常
    await _open_circuit(c, "gpt", M)                   # gpt-a 熔断打开

    r1 = (await c.post("/v1/autopilot/run", headers=M)).json()
    assert r1["auto_applied"] == []                    # streak=1 < sustained(2)，先不动
    r2 = (await c.post("/v1/autopilot/run", headers=M)).json()
    assert any(a["backend"] == "gpt-a" for a in r2["auto_applied"])  # 持续 → 自动摘除

    health = {b["name"]: b for b in (await c.get("/health")).json()["backends"]}
    assert health["gpt-a"]["healthy"] is False         # 已摘除（生效）
    assert health["gpt-b"]["healthy"] is True


async def test_autopilot_skips_already_drained(make_client, flush_redis):
    c = await make_client("tests/configs/diag.yaml")
    app = c._transport.app
    await app.state.overrides.set("backend:gpt-a:healthy", False)  # 已摘除
    await _open_circuit(c, "gpt", M)
    rep = await app.state.autopilot.run_cycle()
    assert "gpt-a" not in rep["detected"]              # 跳过，不再 remediate


# ── 不搞死模型：单后端熔断 → 升级给人 ───────────────────
async def test_autopilot_escalates_when_would_strand(make_client, flush_redis):
    c = await make_client("tests/configs/circuit_low.yaml")  # 仅 solo-bad 一个后端
    await _open_circuit(c, "solo", M, n=5)
    await c.post("/v1/autopilot/run", headers=M)
    r2 = (await c.post("/v1/autopilot/run", headers=M)).json()
    assert r2["auto_applied"] == []                    # 不自批
    assert any(e["backend"] == "solo-bad" for e in r2["escalated"])
    # 未被摘除，留给人审
    assert (await c.get("/health")).json()["backends"][0]["healthy"] is True


# ── 自动回滚 / 保留 ──────────────────────────────────────
def _autopilot(app, **kw):
    return Autopilot(_deps(app), app.state.circuit, app.state.telemetry, **kw)


async def _seed_watch(app, baseline):
    import json
    await app.state.overrides.set("backend:gpt-a:healthy", False)
    await app.state.redis.set("autopilot:watch:backend:gpt-a:healthy", json.dumps(
        {"model": "gpt", "field": "backend:gpt-a:healthy", "baseline": baseline, "deadline": 999}))


async def test_autopilot_rollback_on_regression(make_client, flush_redis):
    c = await make_client()
    app = c._transport.app
    ap = _autopilot(app, window_s=0, now=lambda: 1000.0)
    await _seed_watch(app, baseline=0.0)
    for _ in range(5):                                  # apply 后 error_rate 变差
        await app.state.telemetry.record(_row(1000.0, "served_fallback"))
    rep = await ap.run_cycle()
    assert rep["rolled_back"] and rep["rolled_back"][0]["field"] == "backend:gpt-a:healthy"
    assert await app.state.overrides.get("backend:gpt-a:healthy", True) is True  # 已回滚


async def test_autopilot_promote_when_no_regression(make_client, flush_redis):
    c = await make_client()
    app = c._transport.app
    ap = _autopilot(app, window_s=0, now=lambda: 1000.0)
    await _seed_watch(app, baseline=1.0)                # 基线很高
    for _ in range(5):                                  # 现在全 ok → 改善
        await app.state.telemetry.record(_row(1000.0, "ok"))
    rep = await ap.run_cycle()
    assert rep["promoted"] and rep["promoted"][0]["field"] == "backend:gpt-a:healthy"
    assert await app.state.overrides.get("backend:gpt-a:healthy", True) is False  # 保留摘除


async def test_autopilot_requires_auth(make_client, flush_redis):
    c = await make_client()
    assert (await c.post("/v1/autopilot/run")).status_code == 401
