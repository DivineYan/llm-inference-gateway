"""在途请求跟踪 —— M1 §4.3。

用 Redis sorted set 记录在途请求：成员=trace_id，分值=入场时间戳。
- 多实例全局可见。
- 进入分发前加入、结束后移除（guard 的 finally 保证一定移除）。
- 即便某实例崩溃没来得及移除，过期条目也会在 watermark 判定时按时间被清掉，
  不会让水位永久虚高（泄漏兜底，无需额外清扫进程）。
"""
import time
from contextlib import asynccontextmanager

INFLIGHT_KEY = "inflight:reqs"
# 单个请求最长存活时间；超过即视为泄漏并清除（M1 mock 慢响应远小于此）
MAX_REQUEST_TTL = 30  # TTL 至少大于 P99，后面需要调整


@asynccontextmanager
async def inflight_guard(redis, trace_id: str):
    """包裹分发：进入 +1（ZADD），结束无论如何 -1（ZREM）。"""
    await redis.zadd(INFLIGHT_KEY, {trace_id: time.time()})
    try:
        yield
    finally:
        await redis.zrem(INFLIGHT_KEY, trace_id)
