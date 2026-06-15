"""Autopilot —— 渐进自治闭环（Tier5，最小可跑版）。

自监测 → 自批（窄白名单）→ 生效 → 自动回滚 / 人工升级。当前白名单**只批一种**：
"排除持续熔断的坏后端"（healthy=false），因为它最安全、最可逆、信号最明确。

护栏（防失控、防与 M2 快环打架）：
- 只对"持续"熔断动手：连续 sustained 个巡检周期都 open 才算持续（用 streak 计数）。
- 已摘除的后端跳过（防反复）；同字段冷却由 Tier4 propose 兜底。
- 超白名单/会搞死模型/校验不过 → 不自批，留 proposed 升级给人。
- apply 后开"回滚监控"：盯模型 error_rate 一个窗口，没改善（变差）→ 自动回滚 + 升级。

注：真正的 sandbox/金丝雀需要流量分流（未具备），这里用"可逆 apply + 监控回滚"替代。
now 可注入便于测试确定化窗口。
"""
import asyncio
import json
import logging
import os
import socket
import time
from typing import Callable

from app.agent.change_control import ChangeDeps, approve, propose

LEADER_KEY = "autopilot:leader"
_log = logging.getLogger("gateway.autopilot")


class Autopilot:
    def __init__(self, deps: ChangeDeps, circuit, telemetry, *, sustained: int = 2,
                 window_s: float = 30, regression_eps: float = 0.0,
                 metric_window_s: float = 300, now: Callable[[], float] = time.time):
        self.deps = deps
        self.circuit = circuit
        self.telemetry = telemetry
        self.sustained = sustained
        self.window_s = window_s
        self.eps = regression_eps
        self.metric_window_s = metric_window_s
        self._now = now

    async def run_cycle(self) -> dict:
        report = {"detected": [], "auto_applied": [], "escalated": [],
                  "rolled_back": [], "promoted": []}
        await self._evaluate_watches(report)        # 先结算到期的回滚监控
        for b in self.deps.config.backends:
            # 已被摘除的后端跳过（防反复 remediate）
            if not await self.deps.overrides.get(f"backend:{b.name}:healthy", b.healthy):
                await self._reset_streak(b.name)
                continue
            is_open = await self.circuit.state(self.deps.redis, b.name) == "open"
            #  streak:某后端"连续被检测到熔断 open"的次数计数器。
            streak = await self._streak(b.name, is_open)
            if is_open and streak >= self.sustained:
                report["detected"].append(b.name)
                await self._remediate(b, report)
        return report

    async def _remediate(self, b, report) -> None:
        field = f"backend:{b.name}:healthy"
        p = await propose(self.deps, field, False,
                          f"{b.name} 熔断持续 {self.sustained} 个周期，排除坏后端",
                          proposer="autopilot")
        if _auto_approvable(p):                     # 窄白名单 + valid
            await approve(self.deps, p.id, approver="autopilot")
            await self._watch(b.model, field, baseline=await self._error_rate(b.model))
            report["auto_applied"].append({"backend": b.name, "proposal": p.id})
        else:                                        # 升级给人（留 proposed）
            report["escalated"].append({"backend": b.name, "proposal": p.id, "errors": p.errors})

    # ── 回滚监控 ─────────────────────────────────────────
    async def _watch(self, model: str, field: str, baseline: float) -> None:
        await self.deps.redis.set(
            f"autopilot:watch:{field}",
            json.dumps({"model": model, "field": field, "baseline": baseline,
                        "deadline": self._now() + self.window_s}),
            ex=int(self.window_s) + 3600,
        )

    async def _evaluate_watches(self, report) -> None:
        async for key in self.deps.redis.scan_iter(match="autopilot:watch:*"):
            w = json.loads(await self.deps.redis.get(key))
            if self._now() < w["deadline"]:
                continue
            current = await self._error_rate(w["model"])
            if current > w["baseline"] + self.eps:   # 没改善（更差）→ 回滚 + 升级
                await self.deps.overrides.delete(w["field"])
                report["rolled_back"].append({"field": w["field"],
                                              "baseline": w["baseline"], "current": current})
            else:                                     # 改善/持平 → 保留变更
                report["promoted"].append({"field": w["field"]})
            await self.deps.redis.delete(key)

    # ── 小工具 ───────────────────────────────────────────
    async def _error_rate(self, model: str) -> float:
        # 透传 now：与注入时钟一致（生产为真实时钟）
        agg = await self.telemetry.aggregate(self.metric_window_s, group_by="model", now=self._now())
        return agg.get(model, {}).get("error_rate", 0.0)

    async def _streak(self, name: str, is_open: bool) -> int:
        key = f"autopilot:streak:{name}"
        if not is_open:
            await self.deps.redis.delete(key)
            return 0
        return await self.deps.redis.incr(key)

    async def _reset_streak(self, name: str) -> None:
        await self.deps.redis.delete(f"autopilot:streak:{name}")


def _auto_approvable(p) -> bool:
    """窄白名单：只自批"排除坏后端"——backend healthy=false 且提案合法。"""
    return (p.valid and p.field.startswith("backend:")
            and p.field.endswith(":healthy") and p.value is False)


# ── 后台周期驱动（多实例领导锁）─────────────────────────────
# autopilot 是"慢环"：watch 跨 cycle 结算，必须被周期性反复调用。由 lifespan
# 在真启动时拉起本循环；多实例下用 Redis 领导锁，只有持锁实例 tick，避免重复
# remediate。手动端点 /v1/autopilot/run 仍保留作按需/应急触发。


async def _acquire_leader(redis, instance_id: str, ttl: int) -> bool:
    """领导锁：无主则抢（NX），是自己则续租。返回是否为当前 leader。"""
    holder = await redis.get(LEADER_KEY)
    if holder is None:
        return bool(await redis.set(LEADER_KEY, instance_id, nx=True, ex=ttl))
    if holder == instance_id:
        await redis.expire(LEADER_KEY, ttl)  # 续租，维持租约直到本实例下线
        return True
    return False


async def autopilot_loop(autopilot: Autopilot, redis, interval_s: float,
                         instance_id: str | None = None) -> None:
    """周期巡检循环：抢到领导锁才 run_cycle，否则空转等待。cancel 即干净退出。"""
    instance_id = instance_id or f"{socket.gethostname()}:{os.getpid()}"
    ttl = max(2, int(interval_s * 3))  # 租约 > 间隔：跨 tick 保持，宕机后数 tick 内释放
    _log.info("autopilot 后台巡检启动 interval=%ss instance=%s", interval_s, instance_id)
    while True:
        try:
            if await _acquire_leader(redis, instance_id, ttl):
                await autopilot.run_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:  # 单轮失败不拖垮循环
            _log.warning("autopilot 巡检异常", exc_info=True)
        await asyncio.sleep(interval_s)
