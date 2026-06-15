"""M2-T3：CircuitBreaker 三态机（M2 §4.1）。

直接打 Redis（db 15，flush_redis 夹具），用注入的可控时钟让冷却/窗口
确定化、不真等。覆盖：跳闸、最小样本保护、半开探针、恢复、重新打开、按后端隔离。
"""
from app.config_models import CircuitConfig
from app.redis_client import load_script
from app.safeguard.circuit import CircuitBreaker


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _breaker(redis, clock, **over):
    cfg = CircuitConfig(
        window_seconds=100, failure_rate=0.5, min_samples=5,
        cooldown_seconds=10, half_open_probes=1,
    ).model_copy(update=over)
    allow = load_script(redis, "circuit_allow.lua")
    record = load_script(redis, "circuit_record.lua")
    return CircuitBreaker(allow, record, cfg, now=clock)


async def test_closed_allows_by_default(flush_redis):
    cb = _breaker(flush_redis, Clock())
    assert await cb.allow("b") == (True, False)


async def test_trips_open_after_failure_rate(flush_redis):
    cb = _breaker(flush_redis, Clock())
    for _ in range(5):                       # 5 次全失败：rate=1.0≥0.5 且 样本≥5
        await cb.record("b", success=False)
    assert await cb.allow("b") == (False, False)   # 打开 → 快速失败


async def test_min_samples_guards_premature_trip(flush_redis):
    cb = _breaker(flush_redis, Clock())
    for _ in range(4):                       # 100% 失败但样本不足 5
        await cb.record("b", success=False)
    assert (await cb.allow("b"))[0] is True  # 不跳闸


async def test_open_then_half_open_probe_then_close(flush_redis):
    clock = Clock()
    cb = _breaker(flush_redis, clock)
    for _ in range(5):
        await cb.record("b", success=False)
    assert await cb.allow("b") == (False, False)   # 冷却中
    clock.t += 11                                  # 冷却到点（>10）
    assert await cb.allow("b") == (True, True)     # 转半开，放探针
    assert await cb.allow("b") == (False, False)   # 探针在飞，名额已满，不再放
    await cb.record("b", success=True)             # 探针成功 → 恢复闭合
    assert await cb.allow("b") == (True, False)


async def test_half_open_probe_failure_reopens(flush_redis):
    clock = Clock()
    cb = _breaker(flush_redis, clock)
    for _ in range(5):
        await cb.record("b", success=False)
    clock.t += 11
    assert await cb.allow("b") == (True, True)     # 半开探针
    await cb.record("b", success=False)            # 探针失败 → 重新打开
    assert await cb.allow("b") == (False, False)
    clock.t += 11                                  # 冷却重新计时后再放探针
    assert await cb.allow("b") == (True, True)


async def test_circuits_are_per_backend(flush_redis):
    cb = _breaker(flush_redis, Clock())
    for _ in range(5):
        await cb.record("a", success=False)
    assert (await cb.allow("a"))[0] is False       # a 熔断
    assert await cb.allow("b") == (True, False)    # b 不受影响


async def test_state_read_for_health(flush_redis):
    cb = _breaker(flush_redis, Clock())
    assert await cb.state(flush_redis, "b") == "closed"   # 无记录默认闭合
    for _ in range(5):
        await cb.record("b", success=False)
    assert await cb.state(flush_redis, "b") == "open"
