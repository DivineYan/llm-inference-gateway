"""网关流量生成器 —— 压测 SLA + 攒真实遥测。

按真实配比（多模型/多调用方/含会失败·限流·抢占的请求）打 /v1/infer，输出
吞吐、延迟分位、reason 分布。两种模式：
  --inproc（默认）  免起服务，进程内打 config.loadtest.yaml（turnkey 攒遥测）。
  --url <addr>      压运行中的 uvicorn（真 HTTP，真 SLA 数字）。

用法（PowerShell）：
  # 进程内（最简单，攒遥测/看 reason 分布）
  D:\\Python\\envs\\agent_project\\python.exe scripts/load_gen.py --concurrency 30 --duration 12
  # 真 HTTP（先用 config.loadtest.yaml 起网关）
  $env:CONFIG_PATH="config.loadtest.yaml"; uvicorn app.main:app --port 8000
  D:\\Python\\envs\\agent_project\\python.exe scripts/load_gen.py --url http://127.0.0.1:8000 -c 50 -d 20
"""
import argparse
import asyncio
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LOADTEST_CONFIG = "config.loadtest.yaml"

# 请求配比：(权重, 凭证, 模型) —— 覆盖 ok/限流/抢占/无后端/兜底
PROFILE = [
    (5, "k-svc", "gpt"),     # 正常成功（主流量）
    (3, "k-svc", "slow"),    # 慢响应，撑高在途 → 触发抢占
    (1, "k-svc", "bad"),     # 全失败 → served_fallback
    (1, "k-svc", "local"),   # 无健康后端 → no_backend
    (3, "k-user", "gpt"),    # 低优 + 低限流 → rate_limited / preempted
]


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _pick() -> tuple[str, str]:
    total = sum(w for w, _, _ in PROFILE)
    r = random.uniform(0, total)
    acc = 0.0
    for w, cred, model in PROFILE:
        acc += w
        if r <= acc:
            return cred, model
    return PROFILE[-1][1], PROFILE[-1][2]


async def _worker(post, deadline: float, out: list):
    while time.perf_counter() < deadline:
        cred, model = _pick()
        t0 = time.perf_counter()
        status, reason = await post(cred, model)
        out.append((( time.perf_counter() - t0) * 1000, status, reason))


def _report(records: list, elapsed: float) -> None:
    n = len(records)
    lats = [r[0] for r in records]
    reasons: dict[str, int] = {}
    for _, _, reason in records:
        reasons[reason] = reasons.get(reason, 0) + 1
    print(f"\n=== 结果 ===  请求 {n}  时长 {elapsed:.1f}s  吞吐 {n/elapsed:.0f} req/s")
    print(f"延迟(ms): p50={_percentile(lats,.5):.1f}  p95={_percentile(lats,.95):.1f}  "
          f"p99={_percentile(lats,.99):.1f}  max={max(lats):.1f}")
    print("reason 分布:")
    for reason, c in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {reason:18s} {c:6d}  ({c/n*100:5.1f}%)")


async def _run(post, concurrency: int, duration: float):
    records: list = []
    deadline = time.perf_counter() + duration
    t0 = time.perf_counter()
    await asyncio.gather(*[_worker(post, deadline, records) for _ in range(concurrency)])
    _report(records, time.perf_counter() - t0)


def _parse_resp(status: int, body: dict) -> tuple[int, str]:
    if status == 200:
        return status, "degraded" if body.get("degraded") else "ok"
    return status, body.get("reason", f"http_{status}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None, help="压运行中的网关；省略则进程内")
    ap.add_argument("-c", "--concurrency", type=int, default=30)
    ap.add_argument("-d", "--duration", type=float, default=12)
    args = ap.parse_args()

    from httpx import ASGITransport, AsyncClient, Limits

    if args.url:
        print(f"== 模式: HTTP  目标 {args.url}  并发 {args.concurrency}  时长 {args.duration}s ==")
        # 客户端连接池上限必须 > 并发数，否则池耗尽 → 请求干等到超时（伪瓶颈）
        limits = Limits(max_connections=max(args.concurrency * 2, 256),
                        max_keepalive_connections=max(args.concurrency, 128))
        client = AsyncClient(base_url=args.url, timeout=30, limits=limits)
    else:
        os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/11")
        from app.main import create_app
        from app.redis_client import create_redis
        r = create_redis(); await r.flushdb(); await r.aclose()
        print(f"== 模式: 进程内({LOADTEST_CONFIG})  并发 {args.concurrency}  时长 {args.duration}s ==")
        app = create_app(LOADTEST_CONFIG)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=30)

    async def post(cred: str, model: str) -> tuple[int, str]:
        try:
            resp = await client.post("/v1/infer", json={"model": model, "input": "x"},
                                     headers={"Authorization": f"Bearer {cred}"})
            return _parse_resp(resp.status_code, resp.json())
        except Exception as exc:  # 网络/超时也计入
            return 0, f"client_error:{type(exc).__name__}"

    try:
        await _run(post, args.concurrency, args.duration)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
