"""① CircuitBreaker —— 熔断器三态机（M2 §4.1 / TD-2）。

对每个后端独立维护 Closed / Open / Half-Open，状态存 Redis（多实例全局一致，
FR-4.4 / NFR-7）。两个有副作用的判定各做成一段 Lua 原子执行：
- allow(backend)  打后端前判定：闭合放行 / 打开快速失败 / 半开发放探针。
- record(backend) 打后端后记账：更新窗口失败率，触发 Closed↔Open↔Half-Open 转移。

now 可注入，便于测试确定化冷却/窗口而不真等（同 Retrier 注入 sleep）。
"""
import time
from typing import Callable

from app.config_models import CircuitConfig


class CircuitBreaker:
    def __init__(
        self,
        allow_script,
        record_script,
        config: CircuitConfig,
        now: Callable[[], float] = time.time,
    ):
        self._allow = allow_script
        self._record = record_script
        self.cfg = config
        self._now = now
        # 空闲后自动忘记旧失败：窗口与冷却都过去一截就让 key 过期
        self._ttl = int(config.window_seconds + config.cooldown_seconds + 60)

    @staticmethod
    def key(backend_name: str) -> str:
        return f"circuit:{backend_name}"

    async def allow(self, backend_name: str) -> tuple[bool, bool]:
        """返回 (是否放行, 是否为半开探针)。"""
        allowed, is_probe, _state = await self._allow(
            keys=[self.key(backend_name)],
            args=[self._now(), self.cfg.cooldown_seconds, self.cfg.half_open_probes, self._ttl],
        )
        return bool(allowed), bool(is_probe)

    async def record(self, backend_name: str, success: bool) -> str:
        """记一次调用结果，返回转移后的新状态。"""
        return await self._record(
            keys=[self.key(backend_name)],
            args=[
                self._now(),
                1 if success else 0,
                self.cfg.window_seconds,
                self.cfg.failure_rate,
                self.cfg.min_samples,
                self._ttl,
            ],
        )

    async def state(self, redis, backend_name: str) -> str:
        """只读当前状态，供 /health 展示（无记录 = closed）。"""
        return await redis.hget(self.key(backend_name), "state") or "closed"
