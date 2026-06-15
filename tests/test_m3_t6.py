"""M3-T6：ReAct 诊断 Agent（招牌）。

- diagnosis：注入 gpt-a 熔断故障 → agent 调工具，观测到真实根因信号
  （gpt-a circuit=open），给出结论；轨迹检查点记录每步。
- max_steps：模型一直要求调工具 → 超界 → failed，不死循环。
- resume：已完成任务重跑 → 返回缓存结论，零额外模型调用。
"""
from app.agent.model_gateway import ModelGateway
from app.agent.react import ReActAgent
from app.model import MockModelClient, ModelResponse, ToolCall
from app.safeguard.retry import Retrier

H = {"Authorization": "Bearer m"}


class _CountingClient:
    """包脚本客户端，统计实际模型调用次数（验证续跑零调用）。"""

    def __init__(self, script):
        self.inner = MockModelClient(script)
        self.calls = 0

    async def call(self, backend, req):
        self.calls += 1
        return await self.inner.call(backend, req)


def _agent(app, client, max_steps=6):
    gw = ModelGateway(
        client, app.state.circuit,
        Retrier(app.state.config.safeguard.retry),
        app.state.config.healthy_backends_for_model,
    )
    return ReActAgent(gw, app.state.tools, app.state.tasks, max_steps=max_steps)


async def test_diagnosis_reaches_conclusion(make_client, flush_redis):
    c = await make_client("tests/configs/diag.yaml")
    for _ in range(8):  # 制造失败流量 → gpt-a 熔断打开
        await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=H)
    app = c._transport.app

    script = [
        ModelResponse(tool_calls=[ToolCall(name="get_backend_health", arguments={})]),
        ModelResponse(content="根因：gpt-a 连续失败已熔断，流量由 gpt-b 承接"),
    ]
    agent = _agent(app, _CountingClient(script))
    res = await agent.run("diag1", "为什么部分 gpt 请求异常？")

    assert res["status"] == "success"
    assert "gpt-a" in res["conclusion"]
    assert res["steps"] == 1  # 一次工具调用后即给结论

    traj = await app.state.tasks.get_trajectory("diag1")
    assert traj[0]["action"] == "get_backend_health"
    circuits = {b["name"]: b["circuit"] for b in traj[0]["observation"]["backends"]}
    assert circuits["gpt-a"] == "open"  # agent 观测到真实根因信号


async def test_max_steps_bound(make_client, flush_redis):
    c = await make_client("tests/configs/diag.yaml")
    app = c._transport.app
    # 模型永远要求调工具且参数各异（避开重复检测）→ 仅由 max_steps 兜底
    loop_script = [ModelResponse(tool_calls=[ToolCall(name="query_metrics",
                   arguments={"window_seconds": float(i)})]) for i in range(1, 21)]
    agent = _agent(app, MockModelClient(loop_script), max_steps=3)
    res = await agent.run("loop1", "死循环测试")
    assert res["status"] == "failed"
    assert res["reason"] == "max_steps_exceeded"
    assert res["steps"] == 3


async def test_resume_returns_cached_conclusion(make_client, flush_redis):
    c = await make_client()  # 默认配置 gpt-a 正常 → 首个候选即成功
    app = c._transport.app
    script = [ModelResponse(content="结论：一切正常")]

    client1 = _CountingClient(script)
    r1 = await _agent(app, client1).run("d2", "诊断")
    assert r1["status"] == "success" and client1.calls == 1

    client2 = _CountingClient([ModelResponse(content="不应被调用")])
    r2 = await _agent(app, client2).run("d2", "诊断")  # 同 task_id 续跑
    assert r2["status"] == "success" and r2.get("resumed") is True
    assert r2["conclusion"] == "结论：一切正常"
    assert client2.calls == 0  # 零额外模型调用（命中缓存结论）
