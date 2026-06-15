"""多实例全局一致性 —— M1 §9 最后一条（关键证据）。

构造两个独立的 app 实例（各自独立的进程内状态：TraceStore、SWRR 等），
但共享同一个 Redis。若限流/水位状态真的在 Redis 而非进程内存，
那么对同一调用方的额度应当"合并计算"：在 A 用掉的令牌，B 也看得到。

这正是用两个独立实例（而非两次请求同一实例）来做区分性证明的原因。
真正的"两个 uvicorn 进程"版本见 scripts/multi_instance_demo.py。
"""
from app.inflight import INFLIGHT_KEY

CFG = "tests/configs/small_limits.yaml"  # burst=2
H = {"Authorization": "Bearer m"}
BODY = {"model": "gpt", "input": "x"}


async def test_rate_limit_shared_across_instances(make_client, flush_redis):
    a = await make_client(CFG)
    b = await make_client(CFG)

    # 两实例交替各用 1 个令牌（合计 2 = burst）
    assert (await a.post("/v1/infer", json=BODY, headers=H)).status_code == 200
    assert (await b.post("/v1/infer", json=BODY, headers=H)).status_code == 200

    # 第 3 次无论打哪个实例都应超限 —— 证明额度在 Redis 全局合并
    assert (await a.post("/v1/infer", json=BODY, headers=H)).status_code == 429
    assert (await b.post("/v1/infer", json=BODY, headers=H)).status_code == 429


async def test_inflight_visible_across_instances(make_client, flush_redis):
    a = await make_client(CFG)
    b = await make_client(CFG)

    # 在 Redis 注入在途条目，两实例的 /health 应看到同一水位
    import time
    await flush_redis.zadd(INFLIGHT_KEY, {f"d{i}": time.time() for i in range(4)})

    ha = (await a.get("/health")).json()["water_level"]["inflight"]
    hb = (await b.get("/health")).json()["water_level"]["inflight"]
    assert ha == hb == 4
