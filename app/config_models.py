"""配置数据模型 —— M1 §4.5 配置中心。

用 pydantic 描述三类配置：调用方画像、后端列表、阈值。
对应 NFR-6：改阈值改配置、不改代码。
"""
from typing import Literal

from pydantic import BaseModel, Field


class RateLimit(BaseModel):
    """令牌桶参数：回填速率 + 桶容量（应对突发）。"""

    rate_per_sec: float  # 每秒回填多少令牌
    burst: int           # 桶容量上限


class CallerProfile(BaseModel):
    """调用方画像 —— M1 §4.1。注入 RequestContext 供后续闸门读取。"""

    caller_id: str                      # 如 svc:search / user:zhangsan
    type: Literal["machine", "human"]
    owner: str
    priority: Literal["high", "low"]
    rate_limit: RateLimit
    allowed_models: list[str]


class CallerEntry(CallerProfile):
    """配置文件中的一条调用方记录：画像 + 其凭证。"""

    credential: str  # API Key（机器）/ Token（人类），M1 统一为字符串


class BackendConfig(BaseModel):
    """后端配置 —— M1 §4.4。provider=mock 为进程内 mock；openai_compatible 为真实模型。"""

    name: str
    model: str          # 平台对外的模型名（路由用）
    address: str        # mock 标识 / 备注
    weight: int = 1
    healthy: bool = True
    # mock 行为（T10 / M2 §4.4 错误模型）：成功/5xx失败/超时/慢响应/参数错
    # failure→可重试 5xx，timeout→可重试超时，bad_request→不可重试 4xx
    behavior: Literal["success", "failure", "timeout", "slow", "bad_request"] = "success"
    delay_ms: int = 0   # slow/timeout 的延迟
    # 真实模型（OpenAI/DeepSeek/Qwen/Kimi 等均为 OpenAI 兼容协议）
    provider: Literal["mock", "openai_compatible"] = "mock"
    base_url: str | None = None      # 如 https://api.deepseek.com/v1
    api_key_env: str | None = None   # 存放 API key 的环境变量名（绝不把 key 写进配置）
    real_model: str | None = None    # provider 侧真实模型 id，如 deepseek-chat / gpt-4o


class Thresholds(BaseModel):
    """全局容量水位线 —— M1 §4.3。用在途请求数作代理指标。"""

    high_watermark: int  # 警戒线：在途 ≥ 它 → 紧张
    low_watermark: int   # 解除线：在途 ≤ 它 → 恢复正常（两线留迟滞）


class RetryPolicy(BaseModel):
    """重试与退避参数 —— M2 §4.2。指数退避 + 抖动 + 最大次数。"""

    max_attempts: int = 3        # 单后端最大尝试次数（含首次）
    base_backoff_ms: int = 50    # 退避基数
    max_backoff_ms: int = 1000   # 退避上限（封顶，防止越退越久）


class CircuitConfig(BaseModel):
    """熔断参数 —— M2 §4.1。滚动时间窗 + 失败率 + 最小样本 + 半开探针。"""

    window_seconds: float = 10      # 失败率统计窗口（到期重置计数，轻量实现）
    failure_rate: float = 0.5       # 跳闸失败率阈值（0~1）
    min_samples: int = 5            # 最小样本数，不足不跳闸（防 1/1 误判）
    cooldown_seconds: float = 5     # Open → Half-Open 冷却时间
    half_open_probes: int = 1       # 半开态放行的探针数


class DegradeConfig(BaseModel):
    """降级参数 —— M2 §4.3。主模型不可用时切备用模型；全挂返回兜底文本。"""

    fallback_model: dict[str, str] = Field(default_factory=dict)  # 模型 → 备用模型
    fallback_response: str = "服务繁忙，请稍后重试"               # 全挂兜底文本


class SafeguardConfig(BaseModel):
    """保障层全局参数 —— M2 §5。两类调用方共用（PRD §2.2）。"""

    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    circuit: CircuitConfig = Field(default_factory=CircuitConfig)
    degrade: DegradeConfig = Field(default_factory=DegradeConfig)


class AutopilotConfig(BaseModel):
    """Tier5 自治后台巡检 —— M3_TIER4.md §5b。默认关闭，按环境开启。

    测试用 ASGITransport 不跑 lifespan，故后台循环不会在测试中启动；
    `uvicorn app.main:app` 真启动时才按 enabled 启动。多实例下用 Redis 领导锁，
    只有持锁实例 tick（与 M1/M2"状态放 Redis、全局一致"一脉相承）。
    """

    enabled: bool = False         # 是否启动后台周期巡检
    interval_s: float = 30        # 巡检间隔（应 ≥ window_s，保证上轮观察哨到期可结算）
    sustained: int = 2            # 连续 N 轮熔断才算"持续"（streak 门控）
    window_s: float = 60          # 摘除后观察窗（到期判保留/回滚）
    metric_window_s: float = 300  # 评估 error_rate 的遥测窗口
    regression_eps: float = 0.0   # 回归判定容差（current > baseline+eps 才回滚）


class AgentConfig(BaseModel):
    """M3 Agent 编排层护栏参数（harness）—— M3_HARNESS.md。改参数改这里。"""

    max_steps: int = 10           # H1  ReAct 循环步数上界
    # H13 超时硬中止：模型调用（真 LLM 秒级）与工具调用（本地读毫秒级）延迟差异大，分开设
    model_timeout_s: float = 60  # H13 单次模型调用超时（真实模型可能数十秒）
    tool_timeout_s: float = 5    # H13 单次工具调用超时
    max_obs_chars: int = 2000    # H12a 单条观测注入上下文前的截断长度
    max_repair: int = 2          # H15/H17 连续错误观测（坏参数/坏调用）修复上限
    max_repeat: int = 2          # H16 同一工具调用(name+args)重复上限
    autopilot: AutopilotConfig = Field(default_factory=AutopilotConfig)


class GatewayConfig(BaseModel):
    """配置文件根结构。"""

    callers: list[CallerEntry]
    backends: list[BackendConfig]
    thresholds: Thresholds
    safeguard: SafeguardConfig = Field(default_factory=SafeguardConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
