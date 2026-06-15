"""对外接口 —— M1 §6。

- POST /v1/infer：发起一次推理，走完整流水线 ①→⑤。
- GET  /health：网关存活 + 各后端 healthy 状态 + 当前水位。
- GET  /debug/trace/{trace_id}：查某请求的 decision_log（自测用）。
"""
import logging
import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.context import RequestContext
from app.errors import SERVED_FALLBACK
from app.gates.scheduler import Scheduler
from app.inflight import INFLIGHT_KEY, MAX_REQUEST_TTL
from app.observability import log_request
from app.telemetry import summarize

router = APIRouter()


class InferRequest(BaseModel):
    model: str
    input: str


def _extract_credential(authorization: str | None) -> str | None:
    if not authorization:
        return None
    # 兼容 "Bearer <key>" 与裸 key
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer "):].strip()
    return authorization.strip()


@router.post("/v1/infer")
async def infer(
    req: InferRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    credential = _extract_credential(authorization)
    ctx = RequestContext(
        requested_model=req.model, input=req.input, credential=credential
    )
    request.state.trace_id = ctx.trace_id

    pipeline = request.app.state.pipeline
    try:
        await pipeline.run(ctx)
    finally:
        # 无论放行还是被拦截，都留存上下文并打日志（含完整 decision_log）
        request.app.state.traces.add(ctx)
        log_request(ctx)
        # M3：写遥测底座。best-effort——观测绝不能拖垮请求路径
        try:
            await request.app.state.telemetry.record(summarize(ctx))
        except Exception:
            logging.getLogger("gateway").warning("telemetry record failed", exc_info=True)

    # 走完整流水线，返回 ⑤ 保障执行器拿到的结果（成功 / 降级 / 兜底）
    body = {
        "trace_id": ctx.trace_id,
        "model": req.model,
        "caller": ctx.caller_profile.caller_id if ctx.caller_profile else None,
        "backend": ctx.chosen_backend.name if ctx.chosen_backend else None,
        "output": ctx.result["output"] if ctx.result else None,
        "degraded": ctx.degraded,                  # M2：是否被保障层降级
    }
    if ctx.degraded:
        body["fallback_model"] = ctx.fallback_model
    # 全挂兜底：结构正常但标 503 served_fallback（PRD §8：后端全不可用）
    if ctx.served_fallback:
        body["reason"] = SERVED_FALLBACK
        return JSONResponse(status_code=503, content=body)
    return body


@router.get("/health")
async def health(request: Request):
    cfg = request.app.state.config
    redis = request.app.state.redis
    circuit = request.app.state.circuit

    overrides = request.app.state.overrides

    # 清掉过期在途条目后读当前水位（只读，不改 mode）
    await redis.zremrangebyscore(INFLIGHT_KEY, 0, time.time() - MAX_REQUEST_TTL)
    inflight = await redis.zcard(INFLIGHT_KEY)
    mode = await redis.get(Scheduler.MODE_KEY) or "normal"

    return {
        "status": "ok",
        "backends": [
            {
                "name": b.name,
                "model": b.model,
                # Tier4：展示有效值（覆盖优先于文件值）
                "healthy": await overrides.get(f"backend:{b.name}:healthy", b.healthy),
                "weight": await overrides.get(f"backend:{b.name}:weight", b.weight),
                "circuit": await circuit.state(redis, b.name),  # M2：熔断状态
            }
            for b in cfg.backends
        ],
        "water_level": {
            "inflight": inflight,
            "high_watermark": await overrides.get("thresholds:high_watermark", cfg.thresholds.high_watermark),
            "low_watermark": await overrides.get("thresholds:low_watermark", cfg.thresholds.low_watermark),
            "mode": mode,
        },
        "overrides": await overrides.all(),  # Tier4：当前活跃的运行时覆盖
    }


@router.get("/debug/trace/{trace_id}")
async def debug_trace(trace_id: str, request: Request):
    ctx = request.app.state.traces.get(trace_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return {
        "trace_id": ctx.trace_id,
        "caller": ctx.caller_profile.caller_id if ctx.caller_profile else None,
        "model": ctx.requested_model,
        "backend": ctx.chosen_backend.name if ctx.chosen_backend else None,
        "decision_log": [
            {
                "gate": d.gate,
                "allowed": d.allowed,
                "reason": d.reason,
                "elapsed_ms": d.elapsed_ms,
            }
            for d in ctx.decision_log
        ],
        # M2 保障决策：每次后端尝试、是否降级、用了哪个备用模型、是否兜底
        "attempts": ctx.attempts,
        "degraded": ctx.degraded,
        "fallback_model": ctx.fallback_model,
        "served_fallback": ctx.served_fallback,
    }
