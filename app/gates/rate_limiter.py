"""② RateLimiter —— 分层令牌桶限流（M1 §4.2）。

按调用方画像的 caller_id 作为限流维度：机器为 svc:xxx、人类为 user:xxx，
天然分层。状态在 Redis，取/回填/扣减由 Lua 原子完成。
"""
import time

from app.context import RequestContext
from app.errors import RATE_LIMITED, GatewayError


class RateLimiter:
    name = "rate_limiter"

    def __init__(self, token_bucket_script, overrides):
        self.script = token_bucket_script
        self.overrides = overrides

    async def check(self, ctx: RequestContext) -> None:
        profile = ctx.caller_profile
        # caller_id 已编码维度：svc:search / user:zhangsan
        key = f"ratelimit:{profile.caller_id}"
        rl = profile.rate_limit
        # Tier4：rate/burst 取有效值（覆盖优先于文件值）
        rate = await self.overrides.get(f"caller:{profile.caller_id}:rate_per_sec", rl.rate_per_sec)
        burst = await self.overrides.get(f"caller:{profile.caller_id}:burst", rl.burst)

        allowed, retry_after_ms = await self.script(
            keys=[key],
            args=[rate, burst, time.time(), 1],
        )

        if not allowed:
            retry_after_s = max(1, round(retry_after_ms / 1000))
            raise GatewayError(
                429,
                RATE_LIMITED,
                "超过限流阈值",
                headers={"Retry-After": str(retry_after_s)},
            )
