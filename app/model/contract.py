"""模型调用契约 —— M3 §4.1。

对齐真实 LLM function-calling 的形状（messages + tools → content 或 tool_calls），
让 mock 与真实模型可互换：编排层与 agent 循环只面向这套契约编程。

ModelResponse 是"二选一"：要么模型给出最终 content，要么要求调用工具
（tool_calls）。agent/workflow 据此驱动下一步（M3 §3）。
"""
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.config_models import BackendConfig


class ToolCall(BaseModel):
    """模型发起的一次工具调用请求。id 用于原生 function-calling 回合配对。"""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    id: str = ""  # provider 侧 tool_call id（mock 为空，原生 FC 必填以配对 tool 结果）


class Message(BaseModel):
    """一条对话消息。

    - assistant 发起工具调用时带 tool_calls（原生 FC 协议）。
    - tool 角色回填工具结果，tool_call_id 配对到对应的 tool_call。
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class ToolSchema(BaseModel):
    """工具定义，形状对齐真实 LLM 的 tool/function 声明，可直接喂模型。"""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)  # JSON schema


class ModelRequest(BaseModel):
    messages: list[Message]
    tools: list[ToolSchema] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    """二选一：content（最终回答）或 tool_calls（要求调工具）。"""

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)  # token 计数（mock 为估算）

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class ModelClient(Protocol):
    """模型后端抽象。mock 与真实 provider 实现同一接口。

    backend 指明打哪个后端（含 provider 地址/行为），req 是统一请求。
    失败时抛 BackendError（供 M2 保障层熔断/重试/降级）。
    """

    async def call(self, backend: BackendConfig, req: ModelRequest) -> ModelResponse:
        ...
