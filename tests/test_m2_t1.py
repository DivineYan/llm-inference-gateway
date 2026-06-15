"""M2-T1：后端错误模型 —— mock 按类型抛错 + retryable 分类（M2 §4.4）。

success 返回结果；failure/timeout/bad_request 抛对应类型异常，
每个异常自带 retryable 标识，供 Retrier 决策。
"""
import pytest

from app.config_models import BackendConfig
from app.mock_backend import (
    BackendBadRequest,
    BackendError,
    BackendServerError,
    BackendTimeout,
    MockBackend,
)


def _backend(behavior="success", delay_ms=0):
    return BackendConfig(
        name="b", model="gpt", address="mock://b",
        behavior=behavior, delay_ms=delay_ms,
    )


async def test_success_returns_result():
    out = await MockBackend().call(_backend("success"), "hi")
    assert out == {"backend": "b", "model": "gpt", "output": "[b] hi"}


async def test_failure_raises_retryable_server_error():
    with pytest.raises(BackendServerError) as ei:
        await MockBackend().call(_backend("failure"), "x")
    assert ei.value.retryable is True


async def test_timeout_raises_retryable_timeout():
    with pytest.raises(BackendTimeout) as ei:
        await MockBackend().call(_backend("timeout"), "x")
    assert ei.value.retryable is True


async def test_bad_request_raises_non_retryable():
    with pytest.raises(BackendBadRequest) as ei:
        await MockBackend().call(_backend("bad_request"), "x")
    assert ei.value.retryable is False


def test_all_backend_errors_share_base():
    # 统一基类，便于上层 except BackendError 兜底
    for cls in (BackendTimeout, BackendServerError, BackendBadRequest):
        assert issubclass(cls, BackendError)
