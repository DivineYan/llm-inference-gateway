"""T10：mock 后端多模式 + inflight 在异常路径下仍释放。"""
import time

import pytest

from app.config_models import BackendConfig
from app.inflight import INFLIGHT_KEY, inflight_guard
from app.mock_backend import BackendError, MockBackend


def _backend(behavior="success", delay_ms=0):
    return BackendConfig(
        name="b", model="gpt", address="mock://b",
        behavior=behavior, delay_ms=delay_ms,
    )


async def test_mock_success():
    out = await MockBackend().call(_backend("success"), "hi")
    assert out["backend"] == "b"
    assert out["output"] == "[b] hi"


async def test_mock_slow_delays_then_succeeds():
    start = time.perf_counter()
    out = await MockBackend().call(_backend("slow", delay_ms=50), "hi")
    # 只需证明"有延迟"，留 10% 容差吸收 Windows 定时器精度抖动（sleep 偶尔早返回几 ms）
    assert (time.perf_counter() - start) >= 0.045
    assert out["output"] == "[b] hi"


async def test_mock_failure_raises():
    with pytest.raises(BackendError):
        await MockBackend().call(_backend("failure"), "hi")


async def test_mock_timeout_raises():
    with pytest.raises(BackendError):
        await MockBackend().call(_backend("timeout", delay_ms=10), "hi")


async def test_inflight_released_on_dispatch_failure(flush_redis):
    # guard 的 finally 保证：分发抛异常也一定 -1，不泄漏水位（M1 §3）
    await flush_redis.delete(INFLIGHT_KEY)
    with pytest.raises(BackendError):
        async with inflight_guard(flush_redis, "t"):
            assert await flush_redis.zcard(INFLIGHT_KEY) == 1
            raise BackendError("boom")
    assert await flush_redis.zcard(INFLIGHT_KEY) == 0
