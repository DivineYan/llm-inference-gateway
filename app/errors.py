"""统一错误与拒绝原因码 —— M1 §6。

每道闸门拦截时抛 GatewayError，由统一异常处理器转成
{reason, message, trace_id} 响应。reason 是稳定的机器可读码，
其中 rate_limited 与 preempted 都是 429 但 reason 不同（有意区分）。
"""

# 拒绝原因码
UNAUTHENTICATED = "unauthenticated"      # 401 凭证无效            来自 ①
MODEL_NOT_ALLOWED = "model_not_allowed"  # 403 请求了无权访问的模型 来自 ①
RATE_LIMITED = "rate_limited"            # 429 自身超过限流阈值     来自 ②
PREEMPTED = "preempted"                  # 429 系统紧张，低优先级被抢占 来自 ③
NO_BACKEND = "no_backend"                # 503 无可用后端          来自 ④
SERVED_FALLBACK = "served_fallback"      # 503 全挂，返回兜底响应   来自 ③ Degrader（M2）


class GatewayError(Exception):
    """闸门拦截异常。携带 HTTP 状态码、原因码、可读信息。"""

    def __init__(
        self,
        status_code: int,
        reason: str,
        message: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.reason = reason
        self.message = message or reason
        self.headers = headers or {}
        super().__init__(self.message)
