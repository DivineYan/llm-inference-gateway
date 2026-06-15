"""可观测（M1 §4.5 最小集）。

- 每个请求贯穿 trace_id，完成时打一条结构化日志：经过哪些闸门、各段耗时、
  放行/拒绝原因。
- TraceStore：内存环形缓存，按 trace_id 留存最近若干请求的上下文，供
  /debug/trace 自测查询（M1 §6 内部决策查询）。

完整链路追踪 / Prometheus 留到 M4。
"""
import logging
from collections import OrderedDict

from app.context import RequestContext

logger = logging.getLogger("gateway.request")


class TraceStore:
    """最近请求上下文的内存留存，超出容量按 FIFO 淘汰。"""

    def __init__(self, capacity: int = 1000):
        self.capacity = capacity
        self._store: "OrderedDict[str, RequestContext]" = OrderedDict()

    def add(self, ctx: RequestContext) -> None:
        self._store[ctx.trace_id] = ctx
        self._store.move_to_end(ctx.trace_id)
        while len(self._store) > self.capacity:
            self._store.popitem(last=False)

    def get(self, trace_id: str) -> RequestContext | None:
        return self._store.get(trace_id)


def log_request(ctx: RequestContext) -> None:
    decisions = " ".join(
        f"{d.gate}:{'ok' if d.allowed else d.reason}:{d.elapsed_ms}ms"
        for d in ctx.decision_log
    )
    total = round(sum(d.elapsed_ms for d in ctx.decision_log), 3)
    logger.info(
        "trace=%s caller=%s model=%s backend=%s total=%sms | %s",
        ctx.trace_id,
        ctx.caller_profile.caller_id if ctx.caller_profile else None,
        ctx.requested_model,
        ctx.chosen_backend.name if ctx.chosen_backend else None,
        total,
        decisions,
    )
