"""M3-T11（Tier4 Step A1）：运行时配置覆盖层。

验证：有覆盖用覆盖、无覆盖用 fallback；set 同实例立即可见；delete 即回滚；
TTL 快照在窗口内不重复打 Redis；跨实例在 TTL 内收敛。
"""
import pytest_asyncio

from app.overrides import OverrideStore


@pytest_asyncio.fixture
async def store(flush_redis):
    return OverrideStore(flush_redis, ttl=1.0)


async def test_fallback_when_no_override(store):
    assert await store.get("backend:gpt-a:weight", 3) == 3


async def test_set_then_get_visible_same_instance(store):
    await store.set("backend:gpt-a:weight", 1)
    assert await store.get("backend:gpt-a:weight", 3) == 1


async def test_delete_rolls_back(store):
    await store.set("backend:gpt-a:weight", 1)
    await store.delete("backend:gpt-a:weight")
    assert await store.get("backend:gpt-a:weight", 3) == 3


async def test_all_returns_active_overrides(store):
    await store.set("backend:gpt-a:healthy", False)
    await store.set("thresholds:high_watermark", 15)
    allv = await store.all()
    assert allv["backend:gpt-a:healthy"] is False
    assert allv["thresholds:high_watermark"] == 15


async def test_ttl_snapshot_avoids_refetch(flush_redis):
    # 固定时钟：TTL 窗口内只 HGETALL 一次（外部直接改 Redis 不会立刻反映）
    clock = {"t": 100.0}
    store = OverrideStore(flush_redis, ttl=1.0, now=lambda: clock["t"])
    await store.get("x", None)                       # 首次拉取快照
    await flush_redis.hset("config:overrides", "x", "42")  # 绕过 store 直接改
    assert await store.get("x", None) is None        # 窗口内仍是旧快照
    clock["t"] += 1.5                                 # TTL 过期
    assert await store.get("x", None) == 42           # 刷新后可见


async def test_cross_instance_converges_within_ttl(flush_redis):
    a = OverrideStore(flush_redis, ttl=1.0)
    b = OverrideStore(flush_redis, ttl=0.0)  # b 每次都刷新（模拟 TTL 到期）
    await a.set("backend:gpt-a:weight", 1)   # 实例 a 改
    assert await b.get("backend:gpt-a:weight", 3) == 1  # 实例 b 读到
