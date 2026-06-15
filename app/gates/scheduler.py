"""③ PriorityScheduler —— 优先级调度（M1 §4.3，本期亮点）。

读全局容量水位（在途请求数），按迟滞状态机判定系统是否"紧张"：
- 正常：全部放行。
- 紧张：高优先级放行，低优先级拒绝（429 preempted）。

判定开销极小：一次 Redis 脚本（清理过期 + ZCARD + 比大小），不成为瓶颈。
拒绝原因码用独立的 preempted，与限流的 rate_limited 区分（M1 §6）。
"""
import time

from app.config_models import Thresholds
from app.context import RequestContext
from app.errors import PREEMPTED, GatewayError
from app.inflight import INFLIGHT_KEY, MAX_REQUEST_TTL


class Scheduler:
    name = "scheduler"
    MODE_KEY = "scheduler:mode"

    def __init__(self, watermark_script, thresholds: Thresholds, overrides):
        self.script = watermark_script
        self.high = thresholds.high_watermark
        self.low = thresholds.low_watermark
        self.overrides = overrides

    async def check(self, ctx: RequestContext) -> None:
        stale_before = time.time() - MAX_REQUEST_TTL
        # Tier4：水位线取有效值（覆盖优先于文件值）
        high = await self.overrides.get("thresholds:high_watermark", self.high)
        low = await self.overrides.get("thresholds:low_watermark", self.low)
        inflight, mode = await self.script(
            keys=[INFLIGHT_KEY, self.MODE_KEY],
            args=[high, low, stale_before],
        )

        if mode == "tense" and ctx.caller_profile.priority == "low":
            raise GatewayError(
                429, PREEMPTED, f"系统繁忙（在途 {inflight}），低优先级被抢占"
            )
