"""M3-T5：Workflow Runner + 报表 skill + 断点续跑。

- end_to_end：usage_report skill 取数→分析→出报表，状态 success。
- resume：某步失败 → partial；同 task_id 重跑 → 已完成步骤命中检查点跳过
  （计数验证未重复执行），续跑成功。
"""
import pytest_asyncio

from app.agent.skills.reports import usage_report
from app.agent.state import PARTIAL, SUCCESS, TaskStore
from app.agent.tools.registry import Tool, ToolContext, ToolError, ToolRegistry
from app.agent.workflow import Skill, Step, WorkflowRunner

HI = {"Authorization": "Bearer key-search-machine"}


async def test_usage_report_end_to_end(make_client, flush_redis):
    c = await make_client()
    for _ in range(3):
        await c.post("/v1/infer", json={"model": "gpt", "input": "hi"}, headers=HI)
    app = c._transport.app

    res = await app.state.workflow.run("rep1", usage_report(window_seconds=300))
    assert res["status"] == SUCCESS
    assert res["report"].startswith("# 用量/SLA 报表")
    assert "## 分析" in res["report"]
    # 末步是 render_report，决策链应记录 4 步全 ok
    assert [d["outcome"] for d in res["decision_log"]] == ["ok"] * 4


@pytest_asyncio.fixture
async def tasks(flush_redis):
    return TaskStore(flush_redis)


async def test_resume_skips_completed_steps(tasks):
    counter = {"a": 0}
    flaky = {"calls": 0}

    async def count_a(ctx):
        counter["a"] += 1
        return {"n": counter["a"]}

    async def flaky_b(ctx):
        flaky["calls"] += 1
        if flaky["calls"] == 1:
            raise ToolError("first call boom")
        return {"ok": True}

    reg = ToolRegistry(ToolContext(None, None, None, None))
    reg.register(Tool("count_a", "", {}, count_a))
    reg.register(Tool("flaky_b", "", {}, flaky_b))
    runner = WorkflowRunner(reg, gateway=None, tasks=tasks)
    skill = Skill("resume_test", "", [
        Step("a", "tool", tool="count_a"),
        Step("b", "tool", tool="flaky_b"),
    ])

    r1 = await runner.run("tk", skill)
    assert r1["status"] == PARTIAL and r1["failed_step"] == "b"

    r2 = await runner.run("tk", skill)            # 续跑
    assert r2["status"] == SUCCESS
    assert counter["a"] == 1                       # a 未重跑（命中检查点）
    assert r2["decision_log"][0]["outcome"] == "checkpoint_skip"
