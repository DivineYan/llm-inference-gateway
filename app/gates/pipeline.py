"""流水线编排 —— M1 §3。

闸门 ①②③④ 顺序执行：放行则改写 ctx 并继续，拦截则抛 GatewayError。
⑤ Dispatcher 单独放在 inflight guard 内执行，保证"分发前 +1、结束 -1"——
这也是 ③ Scheduler 读到的全局水位来源（M1 §4.3）。

pipeline 统一为每段计时并写 decision_log，观测（T8）天然落地。
"""
import time
from typing import Protocol

from app.context import RequestContext
from app.errors import GatewayError
from app.inflight import inflight_guard


class Gate(Protocol):
    name: str

    async def check(self, ctx: RequestContext) -> None:
        """放行则返回（可改写 ctx）；拦截则抛 GatewayError。"""
        ...


class Pipeline:
    def __init__(self, gates: list[Gate], dispatcher: Gate, redis):

        """
        gates = [
            Authenticator(config),
            RateLimiter(token_bucket, overrides),
            Scheduler(watermark, config.thresholds, overrides),
            Router(config, overrides),
    ]
        """
        self.gates = gates
        self.dispatcher = dispatcher
        self.redis = redis

    async def run(self, ctx: RequestContext) -> None:
        for gate in self.gates:
            await self._run_gate(gate, ctx)

        # 分发在 inflight guard 内：进入 +1，无论成功失败结束 -1
        async with inflight_guard(self.redis, ctx.trace_id):
            await self._run_gate(self.dispatcher, ctx)

    async def _run_gate(self, gate: Gate, ctx: RequestContext) -> None:
        start = time.perf_counter()
        try:
            await gate.check(ctx)
        except GatewayError as exc:
            ctx.record(gate.name, False, exc.reason, (time.perf_counter() - start) * 1000)
            raise
        ctx.record(gate.name, True, None, (time.perf_counter() - start) * 1000)
