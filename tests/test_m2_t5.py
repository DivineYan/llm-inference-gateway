"""M2-T5：保障执行器 —— 候选迭代 × 熔断 × 重试 × 降级（M2 §3）。

用真 CircuitBreaker（db 15 + 可控时钟）、真 Retrier（base=0 不真等）、真 Degrader、
真 MockBackend（按 behavior 抛错），candidates_for 用 dict 注入。
"""
from app.config_models import BackendConfig, CircuitConfig, DegradeConfig, RetryPolicy
from app.context import RequestContext
from app.mock_backend import MockBackend
from app.redis_client import load_script
from app.safeguard.circuit import CircuitBreaker
from app.safeguard.degrade import Degrader
from app.safeguard.executor import SafeguardedExecutor
from app.safeguard.retry import Retrier


def _be(name, model, behavior="success"):
    return BackendConfig(name=name, model=model, address=f"mock://{name}", behavior=behavior)


def _executor(redis, candidates: dict, fallback_model=None):
    circuit = CircuitBreaker(
        load_script(redis, "circuit_allow.lua"),
        load_script(redis, "circuit_record.lua"),
        CircuitConfig(window_seconds=100, failure_rate=0.5, min_samples=3,
                      cooldown_seconds=100, half_open_probes=1),
    )
    retrier = Retrier(RetryPolicy(max_attempts=3, base_backoff_ms=0))
    degrader = Degrader(DegradeConfig(fallback_model=fallback_model or {}))
    return SafeguardedExecutor(circuit, retrier, degrader, MockBackend(), candidates.get), circuit


def _ctx(model, candidates):
    c = RequestContext(requested_model=model, input="hi", credential="x")
    c.candidates = candidates
    return c


async def test_first_candidate_success(flush_redis):
    a = _be("gpt-a", "gpt", "success")
    ex, _ = _executor(flush_redis, {"gpt": [a]})
    ctx = _ctx("gpt", [a])
    await ex.check(ctx)
    assert ctx.result["backend"] == "gpt-a"
    assert ctx.degraded is False
    assert ctx.attempts == [{"backend": "gpt-a", "outcome": "ok"}]


async def test_failover_to_second_candidate_same_model(flush_redis):
    a, b = _be("gpt-a", "gpt", "failure"), _be("gpt-b", "gpt", "success")
    ex, _ = _executor(flush_redis, {"gpt": [a, b]})
    ctx = _ctx("gpt", [a, b])
    await ex.check(ctx)
    assert ctx.result["backend"] == "gpt-b"   # 换了后端但同模型 → 不算降级
    assert ctx.degraded is False
    assert ctx.attempts[0] == {"backend": "gpt-a", "outcome": "failed", "error": "BackendServerError"}
    assert ctx.attempts[1] == {"backend": "gpt-b", "outcome": "ok"}


async def test_skips_open_circuit(flush_redis):
    a, b = _be("gpt-a", "gpt", "success"), _be("gpt-b", "gpt", "success")
    ex, circuit = _executor(flush_redis, {"gpt": [a, b]})
    for _ in range(3):                         # 预先把 gpt-a 熔断打开
        await circuit.record("gpt-a", success=False)
    ctx = _ctx("gpt", [a, b])
    await ex.check(ctx)
    assert ctx.result["backend"] == "gpt-b"
    assert ctx.attempts[0] == {"backend": "gpt-a", "outcome": "circuit_open"}


async def test_degrade_to_backup_model(flush_redis):
    a, local = _be("gpt-a", "gpt", "failure"), _be("local-a", "local", "success")
    ex, _ = _executor(
        flush_redis, {"gpt": [a], "local": [local]}, fallback_model={"gpt": "local"}
    )
    ctx = _ctx("gpt", [a])
    await ex.check(ctx)
    assert ctx.result["backend"] == "local-a"
    assert ctx.degraded is True
    assert ctx.fallback_model == "local"
    assert {"backend": None, "outcome": "degrade_to", "model": "local"} in ctx.attempts


async def test_canned_fallback_when_all_down(flush_redis):
    a = _be("gpt-a", "gpt", "failure")
    ex, _ = _executor(flush_redis, {"gpt": [a]})   # 无备用模型
    ctx = _ctx("gpt", [a])
    await ex.check(ctx)
    assert ctx.served_fallback is True
    assert ctx.degraded is True
    assert ctx.result["output"] == "服务繁忙，请稍后重试"
    assert ctx.result["backend"] is None


async def test_backup_also_down_falls_back_to_canned(flush_redis):
    a, local = _be("gpt-a", "gpt", "failure"), _be("local-a", "local", "failure")
    ex, _ = _executor(
        flush_redis, {"gpt": [a], "local": [local]}, fallback_model={"gpt": "local"}
    )
    ctx = _ctx("gpt", [a])
    await ex.check(ctx)
    assert ctx.served_fallback is True
    assert ctx.result["output"] == "服务繁忙，请稍后重试"
