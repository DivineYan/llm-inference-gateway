"""M2-T2：Retrier —— 指数退避 + 抖动 + 最大次数 + 错误分类（M2 §4.2）。

用注入的 fake sleep（记录退避时长、不真等）和固定 rng 让用例确定化。
"""
import pytest

from app.config_models import RetryPolicy
from app.mock_backend import BackendBadRequest, BackendServerError, BackendTimeout
from app.safeguard.retry import Retrier


class _Sleeps:
    """记录每次退避秒数的 fake sleep。"""

    def __init__(self):
        self.calls = []

    async def __call__(self, seconds: float):
        self.calls.append(seconds)


def _retrier(max_attempts=3, base=0, cap=1000, rng_value=1.0):
    sleeps = _Sleeps()
    policy = RetryPolicy(max_attempts=max_attempts, base_backoff_ms=base, max_backoff_ms=cap)
    return Retrier(policy, sleep=sleeps, rng=lambda: rng_value), sleeps


def _always_raise(exc_factory):
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise exc_factory()

    return op, calls


async def test_retries_retryable_until_max_then_raises():
    r, sleeps = _retrier(max_attempts=3)
    op, calls = _always_raise(lambda: BackendTimeout("t"))
    with pytest.raises(BackendTimeout):
        await r.run(op)
    assert calls["n"] == 3          # 首次 + 2 次重试 = 3 次尝试
    assert len(sleeps.calls) == 2   # 两次重试之间退避两次


async def test_non_retryable_not_retried():
    r, sleeps = _retrier(max_attempts=5)
    op, calls = _always_raise(lambda: BackendBadRequest("bad"))
    with pytest.raises(BackendBadRequest):
        await r.run(op)
    assert calls["n"] == 1          # 不可重试：只试一次
    assert sleeps.calls == []


async def test_succeeds_after_transient_failures():
    r, _ = _retrier(max_attempts=5)
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise BackendServerError("5xx")
        return {"ok": True}

    assert await r.run(op) == {"ok": True}
    assert calls["n"] == 3


async def test_backoff_is_exponential_and_capped():
    # base=100ms, cap=400ms, rng=1.0（取满抖动）→ 退避序列可预测
    r, sleeps = _retrier(max_attempts=6, base=100, cap=400, rng_value=1.0)
    op, _ = _always_raise(lambda: BackendTimeout("t"))
    with pytest.raises(BackendTimeout):
        await r.run(op)
    # 100,200,400,400,400 (ms) → 秒；第 3 次起被 cap 封顶
    assert sleeps.calls == pytest.approx([0.1, 0.2, 0.4, 0.4, 0.4])


async def test_jitter_stays_within_bound():
    # rng=0.0 → 退避取下界 0，证明抖动作用在 [0, capped)
    r, sleeps = _retrier(max_attempts=3, base=100, cap=400, rng_value=0.0)
    op, _ = _always_raise(lambda: BackendTimeout("t"))
    with pytest.raises(BackendTimeout):
        await r.run(op)
    assert sleeps.calls == [0.0, 0.0]
