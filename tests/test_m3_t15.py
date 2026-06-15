"""M3-T15：Autopilot 后台周期巡检 —— 领导锁 + 循环驱动。

- 领导锁：无主则抢、是自己则续租、他人持锁则让出（多实例只一个 tick）。
- 循环：周期性调用 run_cycle；cancel 干净退出。
- 多实例：两个循环共享 Redis，只有 leader 真正跑 cycle。
"""
import asyncio

from app.agent.autopilot import LEADER_KEY, _acquire_leader, autopilot_loop


class _FakeAutopilot:
    """只数 run_cycle 调了几次。"""

    def __init__(self):
        self.cycles = 0

    async def run_cycle(self):
        self.cycles += 1
        return {}


async def test_leader_lock_single_holder(flush_redis):
    r = flush_redis
    assert await _acquire_leader(r, "A", ttl=5) is True    # A 抢到
    assert await _acquire_leader(r, "B", ttl=5) is False   # B 让出
    assert await _acquire_leader(r, "A", ttl=5) is True    # A 续租
    assert await r.get(LEADER_KEY) == "A"


async def test_loop_runs_cycles_then_cancels(flush_redis):
    ap = _FakeAutopilot()
    task = asyncio.create_task(autopilot_loop(ap, flush_redis, interval_s=0.01, instance_id="A"))
    await asyncio.sleep(0.06)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert ap.cycles >= 2  # 周期性跑了多轮


async def test_only_leader_runs_cycle(flush_redis):
    # 预占领导锁：模拟"另一个实例 B 已是 leader"
    await flush_redis.set(LEADER_KEY, "B", ex=5)
    ap = _FakeAutopilot()
    task = asyncio.create_task(autopilot_loop(ap, flush_redis, interval_s=0.01, instance_id="A"))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert ap.cycles == 0  # A 抢不到锁 → 不 tick
