"""② Retrier —— 重试与退避（M2 §4.2 / TD-6）。

对一次后端调用：失败时判断"该不该再试"，可重试就退避后再试，限定最大次数。
- 只重试 BackendError 中标了 retryable 的（超时 / 5xx）；不可重试错误立即抛出。
- 非 BackendError（意外异常）不重试，直接上抛——重试 bug 没意义。
- 退避 = 指数增长（base·2^n）封顶 max，叠加随机抖动，削平重试尖峰防风暴。

Retrier 只管"同一个后端的重复尝试"；换后端 / 降级是执行器（T5）的职责。
sleep/rng 可注入，便于测试确定化（base=0 时退避≈0，用例秒过）。
"""
import asyncio
import random
from typing import Awaitable, Callable, TypeVar

from app.config_models import RetryPolicy
from app.mock_backend import BackendError

T = TypeVar("T")


class Retrier:
    def __init__(
        self,
        policy: RetryPolicy,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ):
        self.policy = policy
        self._sleep = sleep
        self._rng = rng

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        """执行 operation，按策略重试。返回成功结果或抛出最后一次错误。"""
        attempt = 0
        while True:
            try:
                return await operation()
            except BackendError as exc:
                attempt += 1
                if not exc.retryable or attempt >= self.policy.max_attempts:
                    raise
                await self._sleep(self._backoff_seconds(attempt))

    def _backoff_seconds(self, attempt: int) -> float:
        """第 attempt 次失败后的等待（attempt 从 1 起）：指数封顶 + 全抖动。"""
        exp = self.policy.base_backoff_ms * (2 ** (attempt - 1))
        capped = min(exp, self.policy.max_backoff_ms)
        # full jitter：在 [0, capped) 间随机取，把同批重试时刻打散（TD-6）
        return self._rng() * capped / 1000
