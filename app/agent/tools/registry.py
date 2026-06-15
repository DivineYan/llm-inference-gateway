"""工具注册表 —— M3 §4.2。

注册/发现/执行工具。handler 统一签名 `async (context, **arguments)`：context 是
平台句柄（telemetry/config/redis/circuit），arguments 是模型/skill 传入的参数。

参数校验 M3 先做最小化（缺/多参 → ToolError，给模型可读反馈）；完整 JSON-schema
校验列为 harness 增强项（见 §harness）。
"""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.model.contract import ToolSchema


@dataclass
class ToolContext:
    """工具运行所需的平台句柄。读工具用前四个；Tier4 写提案用 overrides/proposals。"""

    telemetry: Any
    config: Any
    redis: Any
    circuit: Any
    overrides: Any = None    # Tier4 运行时覆盖层
    proposals: Any = None    # Tier4 提案存储


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema
    handler: Callable[..., Awaitable[Any]]

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters=self.parameters
        )


class ToolNotFound(Exception):
    """请求了未注册的工具。"""


class ToolError(Exception):
    """工具执行失败（含参数不合法）。消息可回填给模型。"""


# JSON-schema type → Python 类型（H15 轻量校验，不引第三方库）
_JSON_TYPES = {
    "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "array": list, "object": dict,
}


def _validate_args(schema: dict, args: dict) -> None:
    """按工具 schema 校验参数：必填/未知/类型/枚举。不合法 → ToolError（回填模型）。"""
    props = schema.get("properties", {})
    for r in schema.get("required", []):
        if r not in args:
            raise ToolError(f"缺少必填参数: {r}")
    for k, v in args.items():
        if k not in props:
            raise ToolError(f"未知参数: {k}")
        spec = props[k]
        t = spec.get("type")
        if t in ("integer", "number") and isinstance(v, bool):
            raise ToolError(f"参数 {k} 类型应为 {t}")  # bool 是 int 子类，单独挡
        if t in _JSON_TYPES and not isinstance(v, _JSON_TYPES[t]):
            raise ToolError(f"参数 {k} 类型应为 {t}")
        if "enum" in spec and v not in spec["enum"]:
            raise ToolError(f"参数 {k} 取值应在 {spec['enum']}")


class ToolRegistry:
    def __init__(self, context: ToolContext):
        self.context = context
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def list_schemas(self) -> list[ToolSchema]:
        return [t.schema() for t in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFound(name)
        args = arguments or {}
        _validate_args(tool.parameters, args)  # H15：调用前校验参数
        try:
            return await tool.handler(self.context, **args)
        except (ToolNotFound, ToolError):
            raise
        except TypeError as exc:  # 参数名不匹配 → 给模型可读反馈
            raise ToolError(f"{name} 参数不合法: {exc}") from exc
