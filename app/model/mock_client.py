"""MockModelClient —— M3 §4.1，模型后端的 mock 实现。

两件事：
1. 复用 BackendConfig.behavior 模拟故障（5xx/超时/参数错），让 M2 保障层
   在模型调用路径上仍可验证熔断/重试/降级——故障语义与 M1/M2 一致。
2. 脚本化返回 content / tool_calls：测试时注入确定的响应序列，驱动
   ReAct/Workflow 走可预期的路径；无脚本时退化为回显最后一条消息。

接真模型时，换成 AnthropicModelClient/OpenAIModelClient 实现同一 call 即可。
"""
import asyncio

from app.config_models import BackendConfig
from app.mock_backend import BackendBadRequest, BackendServerError, BackendTimeout
from app.model.contract import ModelRequest, ModelResponse


def _estimate_usage(req: ModelRequest, output: str) -> dict[str, int]:
    """token 占位估算（≈4 字符/token）。接真模型后由 provider 返回真值。"""
    input_tokens = sum(len(m.content) for m in req.messages) // 4
    return {"input_tokens": input_tokens, "output_tokens": len(output) // 4}


class MockModelClient:
    """脚本化模型客户端。script 按顺序消费，故障由 backend.behavior 触发。"""

    def __init__(self, script: list[ModelResponse] | None = None):
        self._script = list(script or [])
        self._cursor = 0

    async def call(self, backend: BackendConfig, req: ModelRequest) -> ModelResponse:
        # 1) 故障模拟（先于脚本）：与 M1/M2 mock_backend 同语义
        if backend.behavior in ("slow", "timeout") and backend.delay_ms:
            await asyncio.sleep(backend.delay_ms / 1000)
        if backend.behavior == "failure":
            raise BackendServerError(f"{backend.name} 模拟 5xx 失败")
        if backend.behavior == "timeout":
            raise BackendTimeout(f"{backend.name} 模拟超时")
        if backend.behavior == "bad_request":
            raise BackendBadRequest(f"{backend.name} 模拟参数错误")

        # 2) 脚本化响应：测试用，确定性驱动 agent/workflow
        if self._cursor < len(self._script):
            resp = self._script[self._cursor]
            self._cursor += 1
            if not resp.usage:
                resp.usage = _estimate_usage(req, resp.content or "")
            return resp

        # 3) 默认：回显最后一条消息作为最终 content
        last = req.messages[-1].content if req.messages else ""
        output = f"[{backend.name}] {last}"
        return ModelResponse(content=output, usage=_estimate_usage(req, output))
