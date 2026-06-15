"""配置变更控制（change control）—— Tier4 写操作护栏，详见 M3_TIER4.md。
管线：propose → validate → approve → apply → audit。

agent 只**提案**（propose），改动不立即生效；人工 approve 才 apply 到运行时覆盖层
（Step A）。全程审计、幂等、白名单、冷却、尊重 M2 状态。

可写白名单（H18）：
  backend:{name}:weight / backend:{name}:healthy
  caller:{cid}:rate_per_sec / caller:{cid}:burst
  thresholds:high_watermark / thresholds:low_watermark
  reset_circuit:{name}   （特殊动作：清后端熔断态）
"""
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

# ── 提案模型与存储 ───────────────────────────────────────


class ChangeProposal(BaseModel):
    id: str
    created_ts: float
    field: str               # 覆盖键 或 reset_circuit:{name}
    value: Any = None        # 提案值（reset_circuit 为 None）
    rationale: str = ""
    proposer: str = "agent"
    current: Any = None       # 变更前的有效值
    valid: bool = True
    errors: list[str] = Field(default_factory=list)
    predicted_effect: dict = Field(default_factory=dict)
    status: str = "proposed"  # proposed | applied | rejected
    history: list[dict] = Field(default_factory=list)  # 审计


class ProposalStore:
    INDEX = "change:index"

    def __init__(self, redis, ttl: int = 7 * 86400):
        self.redis = redis
        self.ttl = ttl

    def _key(self, pid: str) -> str:
        return f"change:{pid}"

    async def save(self, p: ChangeProposal) -> None:
        await self.redis.set(self._key(p.id), p.model_dump_json(), ex=self.ttl)
        await self.redis.zadd(self.INDEX, {p.id: p.created_ts})

    async def get(self, pid: str) -> ChangeProposal | None:
        v = await self.redis.get(self._key(pid))
        return ChangeProposal.model_validate_json(v) if v else None

    async def list(self) -> list[ChangeProposal]:
        ids = await self.redis.zrange(self.INDEX, 0, -1)
        out = []
        for i in ids:
            p = await self.get(i)
            if p:
                out.append(p)
        return out


# ── 依赖包 ───────────────────────────────────────────────


@dataclass
class ChangeDeps:
    config: Any
    overrides: Any
    telemetry: Any
    redis: Any
    store: ProposalStore
    cooldown_s: float = 60


# ── 字段解析 ─────────────────────────────────────────────


@dataclass
class _Field:
    kind: str            # backend | caller | thresholds | reset_circuit
    attr: str = ""
    name: str = ""       # backend 名
    cid: str = ""        # caller id（含冒号）


def _parse(field: str) -> _Field | None:
    if field.startswith("reset_circuit:"):
        return _Field("reset_circuit", name=field[len("reset_circuit:"):])
    if field.startswith("backend:"):
        rest = field[len("backend:"):]
        name, _, attr = rest.rpartition(":")
        return _Field("backend", attr=attr, name=name) if name else None
    if field.startswith("caller:"):
        rest = field[len("caller:"):]
        cid, _, attr = rest.rpartition(":")
        return _Field("caller", attr=attr, cid=cid) if cid else None
    if field.startswith("thresholds:"):
        return _Field("thresholds", attr=field[len("thresholds:"):])
    return None


def _find_backend(config, name):
    return next((b for b in config.backends if b.name == name), None)


def _find_caller(config, cid):
    return next((c for c in config.callers if c.caller_id == cid), None)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ── 校验（静态 + 不搞死模型）────────────────────────────


async def _other_usable(deps: ChangeDeps, model: str, exclude: str) -> int:
    """该模型除 exclude 外、有效可用(healthy 且 weight>0)的后端数。"""
    n = 0
    for b in deps.config.backends:
        if b.model != model or b.name == exclude:
            continue
        healthy = await deps.overrides.get(f"backend:{b.name}:healthy", b.healthy)
        weight = await deps.overrides.get(f"backend:{b.name}:weight", b.weight)
        if healthy and weight > 0:
            n += 1
    return n


async def _validate(deps: ChangeDeps, field: str, value) -> list[str]:
    p = _parse(field)
    if p is None:
        return ["不可写字段（白名单外）"]

    if p.kind == "backend":
        b = _find_backend(deps.config, p.name)
        if b is None:
            return [f"后端 {p.name} 不存在"]
        if p.attr == "weight":
            if not _is_int(value) or value < 0:
                return ["weight 应为非负整数"]
            if value == 0 and await _other_usable(deps, b.model, p.name) == 0:
                return [f"会使模型 {b.model} 无可用后端"]
        elif p.attr == "healthy":
            if not isinstance(value, bool):
                return ["healthy 应为布尔值"]
            if value is False and await _other_usable(deps, b.model, p.name) == 0:
                return [f"会使模型 {b.model} 无可用后端"]
        else:
            return ["后端仅允许改 weight / healthy"]

    elif p.kind == "caller":
        if _find_caller(deps.config, p.cid) is None:
            return [f"调用方 {p.cid} 不存在"]
        if p.attr == "rate_per_sec":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                return ["rate_per_sec 应为正数"]
        elif p.attr == "burst":
            if not _is_int(value) or value <= 0:
                return ["burst 应为正整数"]
        else:
            return ["调用方仅允许改 rate_per_sec / burst"]

    elif p.kind == "thresholds":
        if p.attr not in ("high_watermark", "low_watermark"):
            return ["仅允许改 high_watermark / low_watermark"]
        if not _is_int(value) or value <= 0:
            return ["watermark 应为正整数"]
        high = value if p.attr == "high_watermark" else await deps.overrides.get(
            "thresholds:high_watermark", deps.config.thresholds.high_watermark)
        low = value if p.attr == "low_watermark" else await deps.overrides.get(
            "thresholds:low_watermark", deps.config.thresholds.low_watermark)
        if high <= low:
            return ["需满足 high_watermark > low_watermark"]

    elif p.kind == "reset_circuit":
        if _find_backend(deps.config, p.name) is None:
            return [f"后端 {p.name} 不存在"]

    return []


# ── what-if 预估（best-effort，用真实遥测）───────────────


async def _predict(deps: ChangeDeps, field: str, value) -> dict:
    p = _parse(field)
    if p is None:
        return {}
    if p.kind == "backend" and p.attr == "weight":
        agg = await deps.telemetry.aggregate(300, group_by="backend")
        return {"recent_backend_hits": {k: v["count"] for k, v in agg.items()},
                "note": f"{p.name} 权重→{value}，同模型流量按新权重比重新分配"}
    if p.kind == "caller":
        agg = await deps.telemetry.aggregate(300, group_by="caller")
        return {"recent_calls": agg.get(p.cid, {}).get("count", 0),
                "note": f"{p.cid} {p.attr}→{value}"}
    return {}


# ── 当前有效值 / 应用 ────────────────────────────────────


async def _current(deps: ChangeDeps, field: str):
    p = _parse(field)
    if p is None:
        return None
    if p.kind == "reset_circuit":
        return None
    if p.kind == "backend":
        b = _find_backend(deps.config, p.name)
        fallback = getattr(b, p.attr, None) if b else None
        return await deps.overrides.get(field, fallback)
    if p.kind == "caller":
        c = _find_caller(deps.config, p.cid)
        fallback = getattr(c.rate_limit, p.attr, None) if c else None
        return await deps.overrides.get(field, fallback)
    if p.kind == "thresholds":
        fallback = getattr(deps.config.thresholds, p.attr, None)
        return await deps.overrides.get(field, fallback)
    return None


async def _apply(deps: ChangeDeps, field: str, value) -> None:
    if field.startswith("reset_circuit:"):
        await deps.redis.delete(f"circuit:{field[len('reset_circuit:'):]}")
    else:
        await deps.overrides.set(field, value)


def _cooldown_key(field: str) -> str:
    return f"change:cooldown:{field}"


# ── 管线动作 ─────────────────────────────────────────────


async def propose(deps: ChangeDeps, field: str, value=None,
                  rationale: str = "", proposer: str = "agent") -> ChangeProposal:
    errors = await _validate(deps, field, value)
    if await deps.redis.exists(_cooldown_key(field)):
        errors.append("该目标处于变更冷却期，暂不可再次变更")
    p = ChangeProposal(
        id=uuid.uuid4().hex[:12], created_ts=time.time(), field=field, value=value,
        rationale=rationale, proposer=proposer, current=await _current(deps, field),
        valid=not errors, errors=errors,
        predicted_effect=await _predict(deps, field, value),
        status="proposed",
        history=[{"ts": time.time(), "event": "proposed", "by": proposer}],
    )
    await deps.store.save(p)
    return p


async def approve(deps: ChangeDeps, pid: str, approver: str) -> ChangeProposal:
    p = await deps.store.get(pid)
    if p is None:
        raise KeyError(pid)
    if p.status == "applied":          # 幂等：已生效直接返回
        return p
    if p.status == "rejected":
        raise ValueError("提案已被驳回，不能批准")
    if not p.valid:
        raise ValueError(f"提案未通过校验，不能批准：{p.errors}")
    await _apply(deps, p.field, p.value)
    await deps.redis.set(_cooldown_key(p.field), "1", ex=int(deps.cooldown_s))  # 冷却
    p.status = "applied"
    p.history.append({"ts": time.time(), "event": "approved+applied", "by": approver})
    await deps.store.save(p)
    return p


async def reject(deps: ChangeDeps, pid: str, approver: str) -> ChangeProposal:
    p = await deps.store.get(pid)
    if p is None:
        raise KeyError(pid)
    if p.status == "applied":
        raise ValueError("提案已生效，不能驳回（如需撤销请新建回滚提案）")
    p.status = "rejected"
    p.history.append({"ts": time.time(), "event": "rejected", "by": approver})
    await deps.store.save(p)
    return p
