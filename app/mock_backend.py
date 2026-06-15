"""进程内 mock 后端 —— M1 §4.4 / T10，M2 §4.4 错误模型。

M1 不接真实模型：选中后端后构造模拟响应返回。按后端配置的 behavior
模拟 成功/失败/超时/慢响应，作为 M2 验证熔断/重试/降级的基础设施。

M2 把后端故障建模成带类型的异常，每个异常自带 `retryable` 分类（M2 §4.2）：
- 超时 / 5xx 是瞬时/服务端故障，可重试；
- 参数类错误（bad_request）重试也是同样的错，不可重试。
Retrier 直接读 `exc.retryable` 决策，无需额外分类函数（单一事实来源）。
慢响应（slow）仍返回成功，仅用于制造高在途、验证 ③ 优先级调度。
"""
import asyncio

from app.config_models import BackendConfig


class BackendError(Exception):
    """后端调用失败基类。retryable 标识是否可重试（M2 §4.4）。"""

    retryable = False


class BackendTimeout(BackendError):
    """后端超时。瞬时故障，可重试；重试耗尽最终映射 504。"""

    retryable = True


class BackendServerError(BackendError):
    """后端 5xx 服务端错误。可重试；耗尽触发降级。"""

    retryable = True


class BackendBadRequest(BackendError):
    """参数/请求错误（4xx）。重试无意义，不可重试。"""

    retryable = False


class MockBackend:
    async def call(self, backend: BackendConfig, input_text: str) -> dict:
        if backend.behavior in ("slow", "timeout") and backend.delay_ms:
            await asyncio.sleep(backend.delay_ms / 1000)

        if backend.behavior == "failure":
            raise BackendServerError(f"{backend.name} 模拟 5xx 失败")
        if backend.behavior == "timeout":
            raise BackendTimeout(f"{backend.name} 模拟超时")
        if backend.behavior == "bad_request":
            raise BackendBadRequest(f"{backend.name} 模拟参数错误")

        return {
            "backend": backend.name,
            "model": backend.model,
            "output": f"[{backend.name}] {input_text}",
        }
