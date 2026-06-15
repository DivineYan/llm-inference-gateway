"""M3-T3：工具注册表 + 只读工具 + GET /v1/tools。

验证：工具读出真实 telemetry/config/health；get_config 不泄露凭证；
未知工具/坏参数有清晰错误；/v1/tools 列出 schema。
"""
import pytest

from app.agent.tools import (
    ToolContext,
    ToolError,
    ToolNotFound,
    build_default_registry,
)
from app.telemetry import TelemetryStore

HI = {"Authorization": "Bearer key-search-machine"}


async def _registry(app):
    return app.state.tools


async def test_list_tools_endpoint(client):
    r = await client.get("/v1/tools")
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["tools"]}
    assert {"query_metrics", "query_traces", "get_backend_health",
            "query_usage", "get_config", "render_report"} <= names
    # schema 形状对齐 LLM tool 定义
    qm = next(t for t in r.json()["tools"] if t["name"] == "query_metrics")
    assert qm["parameters"]["type"] == "object"


async def test_get_config_hides_credentials(client):
    reg = client._transport.app.state.tools
    callers = await reg.execute("get_config", {"section": "callers"})
    assert callers and all("credential" not in c for c in callers)


async def test_query_metrics_reads_real_telemetry(make_client, flush_redis):
    c = await make_client()
    for _ in range(4):
        await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=HI)
    reg = c._transport.app.state.tools
    agg = await reg.execute("query_metrics", {"window_seconds": 60})
    assert agg["_all"]["count"] == 4
    assert agg["_all"]["outcomes"]["ok"] == 4


async def test_get_backend_health_tool(client):
    reg = client._transport.app.state.tools
    health = await reg.execute("get_backend_health", {})
    names = {b["name"] for b in health["backends"]}
    assert "gpt-a" in names
    assert "mode" in health["water_level"]


async def test_unknown_tool_and_bad_args(client):
    reg = client._transport.app.state.tools
    with pytest.raises(ToolNotFound):
        await reg.execute("nope", {})
    with pytest.raises(ToolError):
        await reg.execute("query_metrics", {"bogus_arg": 1})


async def test_render_report_markdown(client):
    reg = client._transport.app.state.tools
    md = await reg.execute("render_report", {
        "title": "周报", "sections": [{"heading": "用量", "body": "svc:search 4 次"}],
    })
    assert md.startswith("# 周报")
    assert "## 用量" in md
