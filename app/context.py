"""请求上下文 RequestContext —— M1 §5。

贯穿所有闸门的"随行包"：避免组件间反复传参，并集中承载观测信息
（trace_id、每道闸门的放行/拒绝决策及耗时）。
"""
import uuid
from dataclasses import dataclass, field

from app.config_models import BackendConfig, CallerProfile


@dataclass
class DecisionEntry:
    """一道闸门的决策记录（含该段耗时）。"""

    gate: str
    allowed: bool
    reason: str | None
    elapsed_ms: float


@dataclass
class RequestContext:
    requested_model: str
    input: str
    credential: str | None
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    caller_profile: CallerProfile | None = None
    chosen_backend: BackendConfig | None = None
    candidates: list[BackendConfig] = field(default_factory=list)  # ④ 路由产出的有序候选（M2）
    result: dict | None = None
    decision_log: list[DecisionEntry] = field(default_factory=list)
    # 保障层观测（M2 §6）：每次尝试、是否降级、用了哪个备用模型、是否兜底
    attempts: list[dict] = field(default_factory=list)
    degraded: bool = False
    fallback_model: str | None = None
    served_fallback: bool = False

    def record(self, gate: str, allowed: bool, reason: str | None, elapsed_ms: float) -> None:
        self.decision_log.append(
            DecisionEntry(gate, allowed, reason, round(elapsed_ms, 3))
        )
