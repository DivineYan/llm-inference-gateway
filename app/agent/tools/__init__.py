"""mini MCP 工具层 —— M3 §4.2。

工具 = name + description + JSON-schema 参数 + handler，schema 形状对齐真实 LLM
tool 定义，可直接喂模型。M3 工具全只读，读本平台 telemetry/config/health。
预留：未来接外部 MCP server 的工具注册进同一 ToolRegistry（适配口）。
"""
from app.agent.tools.registry import (
    Tool,
    ToolContext,
    ToolError,
    ToolNotFound,
    ToolRegistry,
)
from app.agent.tools.catalog import build_default_registry

__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolNotFound",
    "ToolRegistry",
    "build_default_registry",
]
