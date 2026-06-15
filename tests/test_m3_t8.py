"""M3-T8：编排层对外接口端到端。

/v1/agent · /v1/skills · /v1/skills/{name}/run · /v1/tasks/{id} · /v1/tools。
默认 MockModelClient 回显，模型步即时给结论；重在验证接口与任务状态贯通。
"""
HI = {"Authorization": "Bearer key-search-machine"}


async def test_agent_endpoint_and_task_query(make_client, flush_redis):
    c = await make_client()
    r = await c.post("/v1/agent", json={"goal": "诊断最近的错误", "task_id": "diagA"}, headers=HI)
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == "diagA"
    assert body["status"] == "success"
    assert body["conclusion"]

    t = await c.get("/v1/tasks/diagA")
    assert t.status_code == 200
    tj = t.json()
    assert tj["type"] == "agent"
    assert tj["status"] == "success"
    assert "trajectory" in tj


async def test_skills_list_and_run(make_client, flush_redis):
    c = await make_client()
    for _ in range(3):
        await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=HI)

    skills = (await c.get("/v1/skills")).json()["skills"]
    assert any(s["name"] == "usage_report" for s in skills)

    r = await c.post("/v1/skills/usage_report/run",
                     json={"task_id": "rep1", "params": {"window_seconds": 300}}, headers=HI)
    assert r.status_code == 200
    assert r.json()["status"] == "success"
    assert r.json()["report"].startswith("# 用量/SLA 报表")

    t = (await c.get("/v1/tasks/rep1")).json()
    assert t["type"] == "workflow"
    assert "s_usage" in t["steps"] and "s_report" in t["steps"]


async def test_unknown_skill_and_task_404(make_client, flush_redis):
    c = await make_client()
    assert (await c.post("/v1/skills/nope/run", json={}, headers=HI)).status_code == 404
    assert (await c.get("/v1/tasks/ghost")).status_code == 404


async def test_skill_run_resume_via_http(make_client, flush_redis):
    c = await make_client()
    r1 = await c.post("/v1/skills/usage_report/run", json={"task_id": "rep2"}, headers=HI)
    assert r1.json()["status"] == "success"
    # 同 task_id 重提交 → 全部命中检查点跳过
    r2 = await c.post("/v1/skills/usage_report/run", json={"task_id": "rep2"}, headers=HI)
    assert r2.json()["status"] == "success"
    assert all(d["outcome"] == "checkpoint_skip" for d in r2.json()["decision_log"])
