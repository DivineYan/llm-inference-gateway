"""T5：③ PriorityScheduler —— 水位迟滞 + 优先级抢占（429 preempted）。

直接往 inflight sorted set 注入在途条目模拟水位（分发 guard 在 T7 接入），
验证：紧张时低优被拒/高优放行、迟滞、回落到解除线后恢复。
"""
import time

from app.inflight import INFLIGHT_KEY

CFG = "tests/configs/watermarks.yaml"  # high=3 low=1
HI = {"Authorization": "Bearer m"}  # 机器，高优先级
LO = {"Authorization": "Bearer h"}  # 人类，低优先级
BODY = {"model": "gpt", "input": "x"}


async def _set_inflight(redis, n: int):
    await redis.delete(INFLIGHT_KEY)
    if n:
        await redis.zadd(INFLIGHT_KEY, {f"dummy{i}": time.time() for i in range(n)})


async def test_normal_passes_both(make_client, flush_redis):
    c = await make_client(CFG)
    await _set_inflight(flush_redis, 0)  # 水位 0，正常
    assert (await c.post("/v1/infer", json=BODY, headers=HI)).status_code == 200
    assert (await c.post("/v1/infer", json=BODY, headers=LO)).status_code == 200


async def test_tense_preempts_low_keeps_high(make_client, flush_redis):
    c = await make_client(CFG)
    await _set_inflight(flush_redis, 3)  # >= high=3 → 紧张

    # 高优先级仍放行
    assert (await c.post("/v1/infer", json=BODY, headers=HI)).status_code == 200
    # 低优先级被抢占
    r = await c.post("/v1/infer", json=BODY, headers=LO)
    assert r.status_code == 429
    assert r.json()["reason"] == "preempted"


async def test_hysteresis_then_recover(make_client, flush_redis):
    c = await make_client(CFG)

    # 升到警戒线以上 → 进入紧张，低优被拒
    await _set_inflight(flush_redis, 3)
    assert (await c.post("/v1/infer", json=BODY, headers=LO)).status_code == 429

    # 回落到 2（低于警戒线但高于解除线）→ 迟滞，仍紧张，低优仍被拒
    await _set_inflight(flush_redis, 2)
    assert (await c.post("/v1/infer", json=BODY, headers=LO)).status_code == 429

    # 回落到解除线以下（1 <= low=1）→ 恢复正常，低优放行
    await _set_inflight(flush_redis, 1)
    assert (await c.post("/v1/infer", json=BODY, headers=LO)).status_code == 200


async def test_preempted_distinct_from_rate_limited(make_client, flush_redis):
    # reason 必须能区分 preempted 与 rate_limited（M1 §6）
    c = await make_client(CFG)
    await _set_inflight(flush_redis, 3)
    r = await c.post("/v1/infer", json=BODY, headers=LO)
    assert r.json()["reason"] == "preempted"  # 不是 rate_limited
