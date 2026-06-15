"""M3-T4：任务状态机 + 检查点 + 续跑判定。

验证：create 首次 True、再次 False（续跑识别）；状态机读写；步骤检查点
save/get/has；轨迹 append/replay。
"""
import pytest_asyncio

from app.agent.state import FAILED, RUNNING, SUCCESS, TaskStore


@pytest_asyncio.fixture
async def ts(flush_redis):
    return TaskStore(flush_redis)


async def test_create_detects_resume(ts):
    assert await ts.create("task1", "workflow", {"skill": "report"}) is True
    # 再次 create 同 id → False（=续跑），不覆盖状态
    assert await ts.create("task1", "workflow") is False
    meta = await ts.get_meta("task1")
    assert meta["status"] == RUNNING
    assert meta["skill"] == "report"


async def test_status_transitions(ts):
    await ts.create("t", "workflow")
    await ts.set_status("t", SUCCESS)
    assert (await ts.get_meta("t"))["status"] == SUCCESS
    await ts.set_status("t", FAILED)
    assert (await ts.get_meta("t"))["status"] == FAILED


async def test_step_checkpoint(ts):
    await ts.create("t", "workflow")
    assert await ts.has_step("t", "s1") is False
    await ts.save_step("t", "s1", {"output": "42"})
    assert await ts.has_step("t", "s1") is True
    assert (await ts.get_step("t", "s1"))["output"] == "42"


async def test_trajectory_append_and_replay(ts):
    await ts.create("t", "agent")
    await ts.append_trajectory("t", {"action": "query_metrics", "observation": {"count": 3}})
    await ts.append_trajectory("t", {"action": "get_backend_health", "observation": {"ok": 1}})
    traj = await ts.get_trajectory("t")
    assert len(traj) == 2
    assert traj[0]["action"] == "query_metrics"
    assert traj[1]["observation"]["ok"] == 1


async def test_get_meta_missing_returns_none(ts):
    assert await ts.get_meta("nope") is None
