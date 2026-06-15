"""M3-T10：agent harness 护栏（H12a/H13/H15/H16/H17）。

- H13 工具超时 → 观测为 error，不挂死。
- H12a 超大观测注入前被截断。
- H16 同一调用重复超限 → stuck_repeating。
- H17/H15 连续错误观测超 max_repair → repair_exhausted；坏参数被 schema 校验拦下。
"""
import asyncio

import pytest

from app.agent.model_gateway import ModelGateway
from app.agent.react import ReActAgent
from app.agent.tools.registry import Tool, ToolContext, ToolError, ToolRegistry
from app.model import MockModelClient, ModelResponse, ToolCall
from app.safeguard.retry import Retrier

H = {"Authorization": "Bearer m"}


def _gw(app, client):
    return ModelGateway(client, app.state.circuit, Retrier(app.state.config.safeguard.retry),
                        app.state.config.healthy_backends_for_model)


# ── H13 工具超时 ─────────────────────────────────────────
async def test_tool_timeout_becomes_error_observation(make_client, flush_redis):
    c = await make_client("tests/configs/diag.yaml")
    app = c._transport.app

    async def slow(ctx):
        await asyncio.sleep(1)
        return {"ok": True}

    reg = ToolRegistry(ToolContext(None, None, None, None))
    reg.register(Tool("slow", "", {}, slow))
    script = [ModelResponse(tool_calls=[ToolCall(name="slow", arguments={})]),
              ModelResponse(content="结论")]
    agent = ReActAgent(_gw(app, MockModelClient(script)), reg, app.state.tasks,
                       tool_timeout_s=0.05)
    res = await agent.run("to1", "诊断")

    assert res["status"] == "success"
    traj = await app.state.tasks.get_trajectory("to1")
    assert "超时" in traj[0]["observation"]["error"]


async def test_model_timeout_uses_separate_budget(make_client, flush_redis):
    """H13：模型调用走 model_timeout_s（独立于工具超时），慢模型被硬中止。"""
    c = await make_client("tests/configs/diag.yaml")
    app = c._transport.app

    class _SlowModel:
        async def call(self, backend, req):
            await asyncio.sleep(1)
            return ModelResponse(content="late")

    agent = ReActAgent(_gw(app, _SlowModel()), app.state.tools, app.state.tasks,
                       model_timeout_s=0.05)
    res = await agent.run("mt1", "诊断")
    assert res["status"] == "failed" and res["reason"] == "model_timeout"


# ── H12a 观测截断 ────────────────────────────────────────
async def test_observation_truncation(make_client, flush_redis):
    c = await make_client()
    app = c._transport.app
    agent = ReActAgent(_gw(app, MockModelClient()), app.state.tools, app.state.tasks,
                       max_obs_chars=50)
    text = agent._observe({"big": "x" * 5000})
    assert len(text) <= 50 + len(" …(truncated)")
    assert text.endswith("…(truncated)")


# ── H16 重复检测 ─────────────────────────────────────────
async def test_repeat_detection_stops_stuck_agent(make_client, flush_redis):
    c = await make_client()
    app = c._transport.app
    # 一直用相同 (name,args) 调同一工具 → 卡死
    script = [ModelResponse(tool_calls=[ToolCall(name="get_backend_health", arguments={})])
              for _ in range(10)]
    agent = ReActAgent(_gw(app, MockModelClient(script)), app.state.tools, app.state.tasks,
                       max_repeat=2)
    res = await agent.run("rep1", "诊断")
    assert res["status"] == "failed"
    assert res["reason"] == "stuck_repeating"


# ── H17/H15 修复有界 ─────────────────────────────────────
async def test_repair_exhausted_on_repeated_errors(make_client, flush_redis):
    c = await make_client()
    app = c._transport.app
    # 一直调未知工具（参数各异避开重复检测）→ 连续 error 观测 → 修复耗尽
    script = [ModelResponse(tool_calls=[ToolCall(name="nope", arguments={"i": i})])
              for i in range(10)]
    agent = ReActAgent(_gw(app, MockModelClient(script)), app.state.tools, app.state.tasks,
                       max_repair=2)
    res = await agent.run("rep2", "诊断")
    assert res["status"] == "failed"
    assert res["reason"] == "repair_exhausted"


# ── H15 参数 schema 校验 ─────────────────────────────────
async def test_schema_validation_rejects_bad_args(client):
    reg = client._transport.app.state.tools
    with pytest.raises(ToolError):  # 缺必填
        await reg.execute("render_report", {"title": "x"})
    with pytest.raises(ToolError):  # 类型错
        await reg.execute("query_metrics", {"window_seconds": "abc"})
    with pytest.raises(ToolError):  # enum 越界
        await reg.execute("query_metrics", {"group_by": "zzz"})
    with pytest.raises(ToolError):  # 未知参数
        await reg.execute("get_backend_health", {"oops": 1})
