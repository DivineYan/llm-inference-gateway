"""LLM 网关专用基准 —— 测对的东西。

LLM 网关后端调用耗时数秒，网关自身的 ms 级开销可忽略；瓶颈不是 QPS，而是
"单实例能同时持有多少在飞的慢请求"。所以测两件事：

[1] 网关额外开销：低并发下 e2e 延迟 - 后端延迟 = 网关纯逻辑开销（应为个位数 ms）。
[2] 并发容量：用慢 mock 模拟 2s 的 LLM，并发从低到高爬坡，看吞吐(应随并发线性，
    Little 定律 throughput≈concurrency/latency)、p99、错误率何时崩 → 单实例并发上限。

进程内测量（不含网络；LLM 延迟本就主导，故捕捉的正是相关开销）。db 10 隔离。
"""
import asyncio
import os
import sys
import tempfile
import time

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/10"

H = {"Authorization": "Bearer k-bench"}


def _pct(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo = int(k)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (k - lo)


def _cfg(delay_ms):
    return {
        "callers": [{"credential": "k-bench", "caller_id": "svc:bench", "type": "machine",
                     "owner": "bench", "priority": "high",
                     "rate_limit": {"rate_per_sec": 10**9, "burst": 10**9},
                     "allowed_models": ["gpt"]}],
        "backends": [{"name": "be", "model": "gpt", "address": "mock://be",
                      "behavior": "slow", "delay_ms": delay_ms}],
        "thresholds": {"high_watermark": 10**9, "low_watermark": 1},  # 关抢占，测裸容量
        "safeguard": {"retry": {"max_attempts": 1, "base_backoff_ms": 1, "max_backoff_ms": 10},
                      "circuit": {"window_seconds": 30, "failure_rate": 0.99, "min_samples": 10**9,
                                  "cooldown_seconds": 10, "half_open_probes": 1},
                      "degrade": {"fallback_model": {}, "fallback_response": "x"}},
    }


async def _make_app(delay_ms):
    from app.main import create_app
    from app.redis_client import create_redis
    r = create_redis(); await r.flushdb(); await r.aclose()
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.safe_dump(_cfg(delay_ms), f, allow_unicode=True)
        path = f.name
    app = create_app(path)
    os.unlink(path)
    return app


async def _post(client):
    t0 = time.perf_counter()
    r = await client.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=H)
    return (time.perf_counter() - t0) * 1000, r.status_code


async def overhead_test():
    from httpx import ASGITransport, AsyncClient
    delay = 200
    app = await _make_app(delay)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=60) as c:
        await _post(c)  # 预热
        lats = [(await _post(c))[0] for _ in range(50)]
    p50 = _pct(lats, .5)
    print("\n[1] 网关额外开销（后端固定 200ms，并发 1）")
    print(f"    e2e p50 = {p50:.1f}ms   后端 = {delay}ms   →  网关纯开销 ≈ {p50 - delay:.1f}ms")


async def _run_level(client, conc, duration, delay_ms):
    records = []
    deadline = time.perf_counter() + duration

    async def worker():
        while time.perf_counter() < deadline:
            records.append(await _post(client))

    t0 = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(conc)])
    elapsed = time.perf_counter() - t0
    lats = [r[0] for r in records]
    errs = sum(1 for _, s in records if s != 200)
    return {"conc": conc, "n": len(records), "qps": len(records) / elapsed,
            "p50": _pct(lats, .5), "p99": _pct(lats, .99), "errs": errs}


async def capacity_test():
    from httpx import ASGITransport, AsyncClient
    delay = 2000  # 模拟 2s 的 LLM
    app = await _make_app(delay)
    print(f"\n[2] 并发容量（后端模拟 {delay}ms 的 LLM；理想吞吐≈并发/{delay/1000:.0f}s）")
    print(f"    {'并发':>5} {'吞吐req/s':>10} {'p50ms':>9} {'p99ms':>9} {'错误':>6}")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=60) as c:
        for conc in [50, 200, 500, 1000, 2000]:
            m = await _run_level(c, conc, 5, delay)
            ideal = conc / (delay / 1000)
            print(f"    {m['conc']:>5} {m['qps']:>10.0f} {m['p50']:>9.0f} {m['p99']:>9.0f} "
                  f"{m['errs']:>6}   (理想吞吐≈{ideal:.0f})")
    await app.state.redis.aclose()


async def main():
    await overhead_test()
    await capacity_test()
    print("\n基准完成。")


if __name__ == "__main__":
    asyncio.run(main())
