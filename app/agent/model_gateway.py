"""ModelGateway —— 编排层的模型调用入口（M3 §4.1 衔接 M1/M2）。

让 ReAct/Workflow 的每次模型调用都：
- 经 M2 保障（熔断 + 重试，复用同一 CircuitBreaker/Retrier 类）；
- 经 M1 治理（H14）：按发起任务的调用方做限流 + 把用量记进遥测（归属到该调用方），
  避免 agent 绕过网关无限消耗后端、且用量不可见。

与 SafeguardedExecutor 同源但面向 ModelRequest/ModelResponse 契约；不做跨模型降级——
编排层在任务层处理"模型调用最终失败"。caller 为 None 时不施加 M1 治理（便于单测）。
"""
import time
import uuid

from app.config_models import BackendConfig, CallerProfile
from app.mock_backend import BackendError
from app.model.contract import ModelClient, ModelRequest, ModelResponse


class NoModelBackend(Exception):
    """该模型无健康后端，或候选全部熔断/失败。"""


class ModelRateLimited(Exception):
    """调用方在 agent 模型调用维度上超过限流阈值（H14）。"""


class ModelGateway:
    def __init__(self, client: ModelClient, circuit, retrier, candidates_for,
                 *, limiter=None, telemetry=None):
        self.client = client
        self.circuit = circuit
        self.retrier = retrier
        self.candidates_for = candidates_for
        self.limiter = limiter        # async (caller_id, rate_limit) -> (allowed, retry_ms)
        self.telemetry = telemetry     # TelemetryStore（按调用方记 agent 用量）

    async def call(self, model: str, req: ModelRequest,
                   caller: CallerProfile | None = None) -> tuple[ModelResponse, BackendConfig]:
        """返回 (响应, 命中后端)。候选全不可用 → NoModelBackend；限流 → ModelRateLimited。"""
        # H14：M1 治理——按调用方限流（agent 用量也受网关约束）
        if caller is not None and self.limiter is not None:
            allowed, _ = await self.limiter(caller.caller_id, caller.rate_limit)
            if not allowed:
                raise ModelRateLimited(caller.caller_id)

        candidates = self.candidates_for(model)
        if not candidates:
            raise NoModelBackend(f"模型 {model} 无健康后端")

        last_exc: Exception | None = None
        for be in candidates:
            allowed, is_probe = await self.circuit.allow(be.name)
            if not allowed:
                continue

            async def op() -> ModelResponse:
                return await self.client.call(be, req)

            try:
                resp = await (op() if is_probe else self.retrier.run(op))
            except BackendError as exc:
                await self.circuit.record(be.name, success=False)
                last_exc = exc
                continue
            await self.circuit.record(be.name, success=True)
            await self._record_usage(caller, model, be, resp)  # H14：用量归属
            return resp, be

        raise NoModelBackend(f"模型 {model} 候选全部不可用") from last_exc

    async def _record_usage(self, caller, model, be, resp) -> None:
        """把 agent 的模型调用记进遥测，归属到调用方（source=agent），best-effort。"""
        if caller is None or self.telemetry is None:
            return
        try:
            await self.telemetry.record({
                "trace_id": uuid.uuid4().hex,
                "ts": time.time(),
                "caller": caller.caller_id,
                "model": model,
                "backend": be.name,
                "outcome": "ok",
                "total_ms": 0,
                "degraded": False,
                "served_fallback": False,
                "input_tokens": resp.usage.get("input_tokens", 0),
                "output_tokens": resp.usage.get("output_tokens", 0),
                "source": "agent",
            })
        except Exception:
            pass
