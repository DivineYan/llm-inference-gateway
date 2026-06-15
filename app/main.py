"""网关应用入口 —— FastAPI app 工厂。

启动时同步加载配置、组装流水线到 app.state（M1 不做热更新）。资源在
create_app 内同步创建，是为了让基于 ASGITransport 的测试无需跑 lifespan
也能拿到配置/流水线；后续 Redis 客户端也按同样方式惰性创建。
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agent.api import agent_router
from app.agent.model_gateway import ModelGateway
from app.agent.react import ReActAgent
from app.agent.state import TaskStore
from app.agent.autopilot import Autopilot, autopilot_loop
from app.agent.change_control import ChangeDeps, ProposalStore
from app.agent.tools import ToolContext, build_default_registry
from app.agent.workflow import WorkflowRunner
from app.model import DispatchingModelClient
from app.api import router
from app.config_loader import LoadedConfig, load_config
from app.errors import GatewayError
from app.gates.authenticator import Authenticator
from app.gates.pipeline import Pipeline
from app.gates.rate_limiter import RateLimiter
from app.gates.router import Router
from app.gates.scheduler import Scheduler
from app.mock_backend import MockBackend
from app.observability import TraceStore
from app.overrides import OverrideStore
from app.redis_client import create_redis, load_script
from app.safeguard.circuit import CircuitBreaker
from app.safeguard.degrade import Degrader
from app.safeguard.executor import SafeguardedExecutor
from app.safeguard.retry import Retrier
from app.telemetry import TelemetryStore

logger = logging.getLogger("gateway")

DEFAULT_CONFIG_PATH = "config.yaml"


def build_pipeline(config: LoadedConfig, redis, overrides) -> Pipeline:
    """组装闸门流水线：①②③④ + ⑤ 保障执行器（在 inflight guard 内执行）。

    M2：⑤ 由保障执行器替代裸 Dispatcher，内部组合熔断/重试/降级（M2 §2）。
    Tier4：①②③④ 中 weight/healthy/rate/watermark 走运行时覆盖层（overrides）。
    """
    token_bucket = load_script(redis, "token_bucket.lua")
    watermark = load_script(redis, "watermark.lua")
    gates = [
        Authenticator(config),
        RateLimiter(token_bucket, overrides),
        Scheduler(watermark, config.thresholds, overrides),
        Router(config, overrides),
    ]
    circuit = CircuitBreaker(
        load_script(redis, "circuit_allow.lua"),
        load_script(redis, "circuit_record.lua"),
        config.safeguard.circuit,
    )
    executor = SafeguardedExecutor(
        circuit=circuit,
        retrier=Retrier(config.safeguard.retry),
        degrader=Degrader(config.safeguard.degrade),
        backend=MockBackend(),
        candidates_for=config.healthy_backends_for_model,  # 备用模型的候选（按配置序）
    )
    return Pipeline(gates, executor, redis)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """真启动（uvicorn）时按配置拉起 autopilot 后台巡检；测试用 ASGITransport
    不触发 lifespan，故循环不会在测试中启动。"""
    task = None
    ap_cfg = app.state.config.agent.autopilot
    if ap_cfg.enabled:
        task = asyncio.create_task(
            autopilot_loop(app.state.autopilot, app.state.redis, ap_cfg.interval_s)
        )
    try:
        yield
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def create_app(config_path: str | None = None) -> FastAPI:
    app = FastAPI(title="AI 推理调度平台 - 网关 M1", lifespan=_lifespan)

    path = config_path or os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
    config: LoadedConfig = load_config(path)
    app.state.config = config
    app.state.redis = create_redis()
    app.state.overrides = OverrideStore(app.state.redis)  # Tier4 运行时覆盖层
    app.state.pipeline = build_pipeline(config, app.state.redis, app.state.overrides) # 鉴权/限流/调度/路由/保障执行
    app.state.circuit = app.state.pipeline.dispatcher.circuit  # 熔断器 供 /health 读熔断状态
    app.state.traces = TraceStore() # 进程内环形缓存,留最近 ~1000 条请求上下文
    app.state.telemetry = TelemetryStore(app.state.redis)  # M3 Tier0 遥测底座
    # Tier4：提案存储
    app.state.proposals = ProposalStore(app.state.redis)
    # M3：工具注册表（只读工具 + Tier4 写提案工具）
    app.state.tools = build_default_registry(
        ToolContext(
            telemetry=app.state.telemetry,
            config=config,
            redis=app.state.redis,
            circuit=app.state.circuit,
            overrides=app.state.overrides,
            proposals=app.state.proposals,
        )
    )
    # M3：模型调用入口（复用 M2 熔断+重试，并经 M1 限流治理 + 用量归属，H14）
    app.state.tasks = TaskStore(app.state.redis)
    import time as _time
    _token_bucket = load_script(app.state.redis, "token_bucket.lua")

    async def _agent_limiter(caller_id, rate_limit):
        # Tier4：agent 用量也走有效限流（覆盖优先），与网关一致
        rate = await app.state.overrides.get(f"caller:{caller_id}:rate_per_sec", rate_limit.rate_per_sec)
        burst = await app.state.overrides.get(f"caller:{caller_id}:burst", rate_limit.burst)
        return await _token_bucket(
            keys=[f"ratelimit:{caller_id}"],
            args=[rate, burst, _time.time(), 1],
        )

    app.state.model_gateway = ModelGateway(
        client=DispatchingModelClient(),  # 按 backend.provider 分流 mock / 真实模型
        circuit=app.state.circuit,
        retrier=Retrier(config.safeguard.retry),
        candidates_for=config.healthy_backends_for_model,
        limiter=_agent_limiter,
        telemetry=app.state.telemetry,
    )
    app.state.workflow = WorkflowRunner(
        app.state.tools, app.state.model_gateway, app.state.tasks
    )
    # Tier5：渐进自治闭环（自监测→窄白名单自批→自动回滚/人工升级）
    ap_cfg = config.agent.autopilot
    app.state.autopilot = Autopilot(
        ChangeDeps(config=config, overrides=app.state.overrides, telemetry=app.state.telemetry,
                   redis=app.state.redis, store=app.state.proposals),
        circuit=app.state.circuit, telemetry=app.state.telemetry,
        sustained=ap_cfg.sustained, window_s=ap_cfg.window_s,
        metric_window_s=ap_cfg.metric_window_s, regression_eps=ap_cfg.regression_eps,
    )
    app.state.react = ReActAgent(
        app.state.model_gateway, app.state.tools, app.state.tasks,
        max_steps=config.agent.max_steps,
        model_timeout_s=config.agent.model_timeout_s,
        tool_timeout_s=config.agent.tool_timeout_s,
        max_obs_chars=config.agent.max_obs_chars,
        max_repair=config.agent.max_repair,
        max_repeat=config.agent.max_repeat,
    )
    logger.info(
        "配置已加载: callers=%d backends=%d high=%d low=%d",
        config.caller_count,
        len(config.backends),
        config.thresholds.high_watermark,
        config.thresholds.low_watermark,
    )

    @app.exception_handler(GatewayError)
    async def _gateway_error_handler(request: Request, exc: GatewayError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "reason": exc.reason,
                "message": exc.message,
                "trace_id": getattr(request.state, "trace_id", None),
            },
            headers=exc.headers,
        )

    app.include_router(router)
    app.include_router(agent_router)
    return app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
