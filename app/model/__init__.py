"""模型调用层 —— M3 §4.1。

模型后端的可替换抽象：mock 与真实模型实现同一 ModelClient 契约，
编排层只依赖契约、不依赖具体实现。接真模型只新增适配器。
"""
from app.model.contract import (
    Message,
    ModelClient,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolSchema,
)
from app.model.dispatch import DispatchingModelClient
from app.model.mock_client import MockModelClient
from app.model.openai_client import OpenAICompatibleClient

__all__ = [
    "Message",
    "ModelClient",
    "ModelRequest",
    "ModelResponse",
    "ToolCall",
    "ToolSchema",
    "MockModelClient",
    "OpenAICompatibleClient",
    "DispatchingModelClient",
]
