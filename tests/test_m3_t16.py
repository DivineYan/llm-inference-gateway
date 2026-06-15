"""M3-R6：真实模型适配器（OpenAI 兼容）—— 用 httpx MockTransport 确定性单测。

不连网、不花钱：验证请求映射（messages/tools/原生 FC 回合）、响应解析
（content / tool_calls）、错误→BackendError 映射、按 provider 分流。
"""
import json
import os

import httpx
import pytest

from app.config_models import BackendConfig
from app.mock_backend import BackendBadRequest, BackendServerError, BackendTimeout
from app.model import DispatchingModelClient, MockModelClient, OpenAICompatibleClient
from app.model.contract import Message, ModelRequest, ToolCall, ToolSchema


def _be(**kw) -> BackendConfig:
    return BackendConfig(name="real", model="gpt", address="x", provider="openai_compatible",
                         base_url="https://api.test/v1", api_key_env="TEST_KEY",
                         real_model="test-model", **kw)


def _client(handler) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _ok(body: dict) -> httpx.Response:
    return httpx.Response(200, json=body)


@pytest.fixture
def key():
    os.environ["TEST_KEY"] = "sk-test"
    yield
    os.environ.pop("TEST_KEY", None)


async def test_text_response_and_payload(key):
    cap = {}

    def handler(request):
        cap["payload"] = json.loads(request.content)
        cap["auth"] = request.headers.get("authorization")
        return _ok({"choices": [{"message": {"content": "你好"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

    resp = await _client(handler).call(_be(), ModelRequest(messages=[Message(role="user", content="hi")]))
    assert resp.content == "你好" and resp.wants_tools is False
    assert resp.usage == {"input_tokens": 10, "output_tokens": 3}
    assert cap["payload"]["model"] == "test-model"
    assert cap["auth"] == "Bearer sk-test"
    assert cap["payload"]["messages"] == [{"role": "user", "content": "hi"}]


async def test_tool_call_response_and_tools_sent(key):
    cap = {}

    def handler(request):
        cap["payload"] = json.loads(request.content)
        return _ok({"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "get_backend_health", "arguments": "{\"w\": 60}"}}]}}], "usage": {}})

    req = ModelRequest(messages=[Message(role="user", content="diagnose")],
                       tools=[ToolSchema(name="get_backend_health", description="d",
                                         parameters={"type": "object"})])
    resp = await _client(handler).call(_be(), req)
    assert resp.wants_tools is True
    tc = resp.tool_calls[0]
    assert (tc.id, tc.name, tc.arguments) == ("call_1", "get_backend_health", {"w": 60})
    assert cap["payload"]["tools"][0]["function"]["name"] == "get_backend_health"


async def test_native_fc_message_mapping(key):
    cap = {}

    def handler(request):
        cap["msgs"] = json.loads(request.content)["messages"]
        return _ok({"choices": [{"message": {"content": "ok"}}], "usage": {}})

    req = ModelRequest(messages=[
        Message(role="user", content="q"),
        Message(role="assistant", tool_calls=[ToolCall(id="c1", name="f", arguments={"a": 1})]),
        Message(role="tool", tool_call_id="c1", content="result"),
    ])
    await _client(handler).call(_be(), req)
    m = cap["msgs"]
    assert m[1]["tool_calls"][0]["id"] == "c1"
    assert json.loads(m[1]["tool_calls"][0]["function"]["arguments"]) == {"a": 1}
    assert m[2] == {"role": "tool", "tool_call_id": "c1", "content": "result"}


@pytest.mark.parametrize("status,exc", [
    (500, BackendServerError), (429, BackendServerError),
    (400, BackendBadRequest), (404, BackendBadRequest)])
async def test_http_error_mapping(key, status, exc):
    client = _client(lambda r: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(exc):
        await client.call(_be(), ModelRequest(messages=[Message(role="user", content="x")]))


async def test_timeout_maps_to_backend_timeout(key):
    def handler(request):
        raise httpx.TimeoutException("t")
    with pytest.raises(BackendTimeout):
        await _client(handler).call(_be(), ModelRequest(messages=[Message(role="user", content="x")]))


async def test_missing_key_is_non_retryable():
    os.environ.pop("TEST_KEY", None)
    client = _client(lambda r: _ok({}))
    with pytest.raises(BackendBadRequest):
        await client.call(_be(), ModelRequest(messages=[Message(role="user", content="x")]))


async def test_dispatch_routes_by_provider(key):
    disp = DispatchingModelClient(clients={
        "mock": MockModelClient(),
        "openai_compatible": _client(lambda r: _ok({"choices": [{"message": {"content": "real"}}], "usage": {}})),
    })
    real = await disp.call(_be(), ModelRequest(messages=[Message(role="user", content="x")]))
    assert real.content == "real"
    mock_be = BackendConfig(name="m", model="gpt", address="x")  # provider 默认 mock
    m = await disp.call(mock_be, ModelRequest(messages=[Message(role="user", content="hi")]))
    assert m.content == "[m] hi"
