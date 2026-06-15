"""OpenAICompatibleClient —— 真实模型适配器（M3 §4.1 / T9）。

OpenAI / DeepSeek / Qwen(DashScope 兼容模式) / Kimi(Moonshot) 都用同一套
`/chat/completions` 协议，故一个适配器按 backend 的 base_url + key + real_model
全覆盖。用 httpx 直连（provider 中立、不引 SDK）。

要点：
- 原生 function-calling：tools/tool_calls 双向映射，回合带 id 配对。
- 错误映射到 BackendError，让 M2 熔断/重试/降级在真后端上照常生效：
  超时→BackendTimeout(可重试)，5xx/429→BackendServerError(可重试)，
  4xx/缺 key→BackendBadRequest(不可重试)。
- API key 从环境变量读（backend.api_key_env），绝不进配置文件。
"""
import json
import os

import httpx

from app.config_models import BackendConfig
from app.mock_backend import BackendBadRequest, BackendServerError, BackendTimeout
from app.model.contract import Message, ModelRequest, ModelResponse, ToolCall


def _to_openai_messages(messages: list[Message]) -> list[dict]:
    out = []
    for m in messages:
        d: dict = {"role": m.role}
        if m.role == "tool":
            d["tool_call_id"] = m.tool_call_id or ""
            d["content"] = m.content
        elif m.role == "assistant" and m.tool_calls:
            d["content"] = m.content or None
            d["tool_calls"] = [
                {"id": tc.id or f"call_{i}", "type": "function",
                 "function": {"name": tc.name,
                              "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                for i, tc in enumerate(m.tool_calls)
            ]
        else:
            d["content"] = m.content
        out.append(d)
    return out


def _to_openai_tools(req: ModelRequest) -> list[dict] | None:
    if not req.tools:
        return None
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": t.parameters or {"type": "object", "properties": {}}}}
            for t in req.tools]


def _parse_choice(data: dict) -> ModelResponse:
    msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
    usage = data.get("usage", {}) or {}
    norm_usage = {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)}
    raw_calls = msg.get("tool_calls") or []
    if raw_calls:
        calls = []
        for tc in raw_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args))
        return ModelResponse(content=msg.get("content"), tool_calls=calls, usage=norm_usage)
    return ModelResponse(content=msg.get("content") or "", usage=norm_usage)


class OpenAICompatibleClient:
    """ModelClient 实现，对接任意 OpenAI 兼容后端。"""

    def __init__(self, timeout_s: float = 60.0, client: httpx.AsyncClient | None = None):
        # client 可注入（测试用 MockTransport）；否则惰性创建复用
        self._client = client
        self._owns = client is None
        self._timeout = timeout_s

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def call(self, backend: BackendConfig, req: ModelRequest) -> ModelResponse:
        key = os.environ.get(backend.api_key_env or "")
        if not key:
            raise BackendBadRequest(f"缺少 API key 环境变量 {backend.api_key_env}")

        payload: dict = {"model": backend.real_model,
                         "messages": _to_openai_messages(req.messages)}
        tools = _to_openai_tools(req)
        if tools:
            payload["tools"] = tools
        payload.update(req.params)

        url = f"{(backend.base_url or '').rstrip('/')}/chat/completions"
        try:
            resp = await self._http().post(
                url, json=payload, headers={"Authorization": f"Bearer {key}"})
        except httpx.TimeoutException as exc:
            raise BackendTimeout(f"{backend.name} 超时") from exc
        except httpx.RequestError as exc:  # 连接/网络类 → 瞬时，可重试
            raise BackendServerError(f"{backend.name} 网络错误: {exc}") from exc

        if resp.status_code >= 500 or resp.status_code == 429:
            raise BackendServerError(f"{backend.name} HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise BackendBadRequest(f"{backend.name} HTTP {resp.status_code}: {resp.text[:200]}")
        return _parse_choice(resp.json())
