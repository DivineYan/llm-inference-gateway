"""Autopilot 自治闭环演示 —— 直接观察"检测→摘除→观察→保留/回滚"。

用法（本机 Redis 必须可达）：
    D:\\Python\\envs\\agent_project\\python.exe scripts/autopilot_demo.py

用 Redis db 13（与测试 15、多实例演示 14 隔离）。两幕：
  幕1 实跑：gpt-a 持续熔断 → 连续两轮后自动摘除 → 观察窗到期 → 错误未变差 → 保留。
  幕2 构造：埋一个低基线观察哨 + 注入回归流量 → 触发自动回滚。
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文不乱码
os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/13"

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.agent.autopilot import Autopilot  # noqa: E402
from app.agent.change_control import ChangeDeps  # noqa: E402
from app.main import create_app  # noqa: E402
from app.redis_client import create_redis  # noqa: E402

M = {"Authorization": "Bearer m"}  # diag.yaml 的机器调用方


def _autopilot(app, **kw):
    deps = ChangeDeps(config=app.state.config, overrides=app.state.overrides,
                      telemetry=app.state.telemetry, redis=app.state.redis,
                      store=app.state.proposals)
    return Autopilot(deps, app.state.circuit, app.state.telemetry, **kw)


async def _health(c):
    bs = (await c.get("/health")).json()["backends"]
    return {b["name"]: f"{'healthy' if b['healthy'] else 'DRAINED'}/{b['circuit']}" for b in bs}


async def _streak(app, name):
    v = await app.state.redis.get(f"autopilot:streak:{name}")
    return int(v) if v else 0


def _show(title, report, health, extra=""):
    print(f"\n── {title} {extra}")
    print(f"   report: detected={report['detected']} auto_applied={[a['backend'] for a in report['auto_applied']]} "
          f"escalated={[e['backend'] for e in report['escalated']]} "
          f"rolled_back={[r['field'] for r in report['rolled_back']]} promoted={[p['field'] for p in report['promoted']]}")
    print(f"   health: {health}")


async def act1_drain_and_promote():
    print("\n========== 幕1：检测 → 摘除 → 观察 → 保留（实跑）==========")
    r = create_redis(); await r.flushdb(); await r.aclose()
    app = create_app("tests/configs/diag.yaml")
    ap = _autopilot(app, sustained=2, window_s=2, metric_window_s=300)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # 1) 打流量,把 gpt-a 熔断打开(gpt-a 必失败,gpt-b 兜住 → 请求仍成功)
        for _ in range(8):
            await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=M)
        print(f"\n打了 8 次请求后 health: {await _health(c)}")

        # 2) 第一轮:gpt-a open 但 streak=1 < sustained(2) → 先不动
        rep1 = await ap.run_cycle()
        _show("第1轮", rep1, await _health(c), extra=f"(gpt-a streak={await _streak(app,'gpt-a')})")

        # 3) 第二轮:streak=2 达阈值 → 自动摘除 gpt-a + 埋观察哨
        rep2 = await ap.run_cycle()
        _show("第2轮", rep2, await _health(c), extra=f"(gpt-a streak={await _streak(app,'gpt-a')}) → 摘除并埋观察哨")

        # 4) 摘除后继续打流量(只剩 gpt-b,全成功),等观察窗到期
        for _ in range(5):
            await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=M)
        await asyncio.sleep(2.2)  # 过观察窗 deadline

        # 5) 第三轮:观察哨结算 → 错误率没变差 → 保留摘除
        rep3 = await ap.run_cycle()
        _show("第3轮", rep3, await _health(c), extra="→ 观察窗到期,错误未变差,保留(promoted)")

    await app.state.redis.aclose()


async def act2_rollback():
    print("\n\n========== 幕2：构造回归 → 自动回滚 ==========")
    r = create_redis(); await r.flushdb(); await r.aclose()
    app = create_app("tests/configs/diag.yaml")
    ap = _autopilot(app, window_s=0, metric_window_s=300)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # 构造:gpt-a 已摘除 + 一个低基线(0.0)、已到期的观察哨
        await app.state.overrides.set("backend:gpt-a:healthy", False)
        await app.state.redis.set("autopilot:watch:backend:gpt-a:healthy", json.dumps(
            {"model": "gpt", "field": "backend:gpt-a:healthy", "baseline": 0.0,
             "deadline": time.time() - 1}))
        # 注入"摘除后反而变差"的流量(全兜底)
        for i in range(5):
            await app.state.telemetry.record({
                "trace_id": f"x{i}", "ts": time.time(), "caller": "svc:test", "model": "gpt",
                "backend": "gpt-b", "outcome": "served_fallback", "total_ms": 1.0,
                "degraded": True, "served_fallback": True, "input_tokens": 1, "output_tokens": 1})
        print(f"\n摘除 gpt-a + 基线 error_rate=0.0；注入 5 条兜底(error_rate↑) health: {await _health(c)}")

        rep = await ap.run_cycle()
        _show("结算观察哨", rep, await _health(c), extra="→ 错误率高于基线 → 自动回滚(gpt-a 放回)")

    await app.state.redis.aclose()


async def main():
    await act1_drain_and_promote()
    await act2_rollback()
    print("\n演示结束。")


if __name__ == "__main__":
    asyncio.run(main())
