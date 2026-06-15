"""保障执行器 —— 把 ①熔断 ②重试 ③降级 编排成一次稳健的分发（M2 §3）。

替换 M1 的 ⑤ Dispatcher（保留 name="dispatcher"，pipeline 骨架与 decision_log
口径不变）。三层"换路"粒度不同：
- 重试（Retrier）：同一后端的瞬时故障，原地再试。
- 候选迭代（本类的 for 循环）：同模型换一个后端（熔断/重试耗尽时）。
- 降级（Degrader）：换一个备用模型；最终兜底，保证永远有返回（NFR-3）。

熔断记账规则：执行器对每个候选记一次结果（成功/失败），重试的多次尝试合并为
一次"该后端这次行不行"。后端错误在执行器层统一按失败处理，可重试/不可重试的
区分在 Retrier 内（M2 §4.2）。
"""
from typing import Awaitable, Callable

from app.config_models import BackendConfig
from app.context import RequestContext
from app.mock_backend import BackendError, MockBackend
from app.safeguard.circuit import CircuitBreaker
from app.safeguard.degrade import Degrader
from app.safeguard.retry import Retrier


class SafeguardedExecutor:
    name = "dispatcher"

    def __init__(
        self,
        circuit: CircuitBreaker,
        retrier: Retrier,
        degrader: Degrader,
        backend: MockBackend,
        candidates_for: Callable[[str], list[BackendConfig]],
    ):
        self.circuit = circuit
        self.retrier = retrier
        self.degrader = degrader
        self.backend = backend
        self.candidates_for = candidates_for

    async def check(self, ctx: RequestContext) -> None:
        ctx.result = await self._serve(ctx, ctx.requested_model, ctx.candidates, depth=0)

    async def _serve(
        self, ctx: RequestContext, model: str, candidates: list[BackendConfig], depth: int
    ) -> dict:
        for be in candidates:
            allowed, is_probe = await self.circuit.allow(be.name)
            if not allowed:
                ctx.attempts.append({"backend": be.name, "outcome": "circuit_open"})
                continue
            try:
                result = await self._call(be, ctx.input, is_probe)
            except BackendError as exc:
                await self.circuit.record(be.name, success=False)
                ctx.attempts.append(
                    {"backend": be.name, "outcome": "failed", "error": type(exc).__name__}
                )
                continue
            await self.circuit.record(be.name, success=True)
            ctx.chosen_backend = be
            ctx.attempts.append({"backend": be.name, "outcome": "ok"})
            if depth > 0:  # 来自备用模型 → 标记降级
                ctx.degraded = True
                ctx.fallback_model = model
            return result

        # 候选耗尽：降一级到备用模型（只一级，防级联绕圈）
        if depth == 0:
            backup = self.degrader.fallback_for(model)
            if backup:
                ctx.attempts.append({"backend": None, "outcome": "degrade_to", "model": backup})
                return await self._serve(ctx, backup, self.candidates_for(backup), depth=1)

        # 备用也不可用 / 没配备用 → 预设兜底响应（兜底永远有返回）
        ctx.chosen_backend = None  # 无后端服务，清掉临时默认值
        ctx.degraded = True
        ctx.served_fallback = True
        return self.degrader.fallback_response(ctx.requested_model)

    async def _call(self, be: BackendConfig, input_text: str, is_probe: bool) -> dict:
        async def op() -> dict:
            return await self.backend.call(be, input_text)

        # 半开探针只试一次，不重试：避免重试在半开态反复扰动状态机
        return await op() if is_probe else await self.retrier.run(op)
