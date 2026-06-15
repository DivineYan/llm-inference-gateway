"""M3-T1：模型调用契约 + MockModelClient。

验证：脚本化返回 content/tool_calls；故障模拟与 M1/M2 同语义；默认回显；
contract 的 wants_tools 语义。既有 70 测试的绿由全量回归保证（本步未碰 M1/M2）。
"""
import pytest

from app.config_models import BackendConfig
from app.mock_backend import BackendServerError
from app.model import (
    Message,
    ModelClient,
    ModelRequest,
    ModelResponse,
    MockModelClient,
    ToolCall,
)


def _backend(behavior: str = "success", **kw) -> BackendConfig:
    return BackendConfig(name="m", model="gpt", address="mock://m", behavior=behavior, **kw)


def _req(text: str = "hi") -> ModelRequest:
    return ModelRequest(messages=[Message(role="user", content=text)])


async def test_default_echoes_last_message_as_content():
    resp = await MockModelClient().call(_backend(), _req("hello"))
    assert resp.content == "[m] hello"
    assert resp.wants_tools is False
    assert resp.usage["output_tokens"] >= 0


async def test_script_returns_responses_in_order():
    script = [
        ModelResponse(tool_calls=[ToolCall(name="query_traces", arguments={"window": "1h"})]),
        ModelResponse(content="结论：gpt-a 熔断"),
    ]
    client = MockModelClient(script=script)

    first = await client.call(_backend(), _req())
    assert first.wants_tools is True
    assert first.tool_calls[0].name == "query_traces"

    second = await client.call(_backend(), _req())
    assert second.content == "结论：gpt-a 熔断"
    assert second.wants_tools is False


async def test_behavior_failure_raises_backend_error():
    with pytest.raises(BackendServerError):
        await MockModelClient().call(_backend("failure"), _req())


def test_mock_client_satisfies_protocol():
    assert isinstance(MockModelClient(), ModelClient)
