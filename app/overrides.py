"""运行时配置覆盖层 —— M3 Tier4（M3_TIER4.md §2）。

让配置能被运行时修改而不重启（NFR-6 的热更新版）：覆盖存 Redis hash，
网关组件读取时"有覆盖用覆盖、否则用文件值"。

热路径考量：组件每请求都要取有效值，但不能每次打 Redis。做法——每实例维护
内存快照，按短 TTL 懒刷新（HGETALL 一次）；覆盖很少变（人审过的），滞后 ≤TTL
完全可接受，多实例各自轮询、TTL 内收敛。回滚 = 删除覆盖键。

字段命名约定：
  backend:{name}:weight / backend:{name}:healthy
  caller:{caller_id}:rate_per_sec / caller:{caller_id}:burst
  thresholds:high_watermark / thresholds:low_watermark
"""
import json
import time
from typing import Callable

OVERRIDE_KEY = "config:overrides"


class OverrideStore:
    def __init__(self, redis, ttl: float = 1.0, now: Callable[[], float] = time.time):
        self.redis = redis
        self.ttl = ttl
        self._now = now
        self._snapshot: dict[str, str] = {}
        self._fetched_at = -1.0

    async def _maybe_refresh(self) -> None:
        if self._now() - self._fetched_at >= self.ttl:
            self._snapshot = await self.redis.hgetall(OVERRIDE_KEY)
            self._fetched_at = self._now()

    async def get(self, field: str, fallback):
        """取有效值：有覆盖返回覆盖，否则返回 fallback（文件值）。"""
        await self._maybe_refresh()
        raw = self._snapshot.get(field)
        return json.loads(raw) if raw is not None else fallback

    async def all(self) -> dict:
        """当前全部覆盖（供 /health 与审计展示）。"""
        await self._maybe_refresh()
        return {k: json.loads(v) for k, v in self._snapshot.items()}

    async def set(self, field: str, value) -> None:
        """写覆盖（Tier4 apply）。强制下次读刷新，保证同实例立即可见。"""
        await self.redis.hset(OVERRIDE_KEY, field, json.dumps(value))
        self._fetched_at = -1.0

    async def delete(self, field: str) -> None:
        """删覆盖（回滚）。"""
        await self.redis.hdel(OVERRIDE_KEY, field)
        self._fetched_at = -1.0
