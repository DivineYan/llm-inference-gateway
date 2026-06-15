"""M3 编排层对外接口 —— M3 §6。

- GET  /v1/tools              列出可用工具
- POST /v1/agent              提交一次 ReAct 诊断
- GET  /v1/skills             列出可用 skill
- POST /v1/skills/{name}/run  跑一个 skill（报表/建议）
- GET  /v1/tasks/{id}         查任务状态机 + 中间结果
"""
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from app.agent.model_gateway import ModelRateLimited, NoModelBackend
from app.agent.skills import SKILLS, build_skill
from app.agent.change_control import ChangeDeps, approve, propose, reject
from app.api import _extract_credential

agent_router = APIRouter()


def _change_deps(request: Request) -> ChangeDeps:
    s = request.app.state
    return ChangeDeps(config=s.config, overrides=s.overrides, telemetry=s.telemetry,
                      redis=s.redis, store=s.proposals)


def _resolve_caller(request: Request, authorization: str | None):
    """鉴权并归属调用方（agent/skill 的模型用量算在该调用方头上，H14）。"""
    credential = _extract_credential(authorization)
    profile = request.app.state.config.caller_by_credential(credential) if credential else None
    if profile is None:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return profile


class AgentRequest(BaseModel):
    goal: str
    task_id: str | None = None
    max_steps: int | None = None


class SkillRunRequest(BaseModel):
    task_id: str | None = None
    params: dict = {}


class ChangeRequest(BaseModel):
    field: str
    value: Any = None
    rationale: str = ""


def _task_id(given: str | None) -> str:
    return given or uuid.uuid4().hex


@agent_router.get("/v1/tools")
async def list_tools(request: Request):
    return {"tools": [s.model_dump() for s in request.app.state.tools.list_schemas()]}


@agent_router.post("/v1/agent")
async def run_agent(req: AgentRequest, request: Request,
                    authorization: str | None = Header(default=None)):
    """ReAct 诊断：给目标，返回 task_id + 结论（或超界 failed）。"""
    caller = _resolve_caller(request, authorization)
    try:
        return await request.app.state.react.run(
            _task_id(req.task_id), req.goal, max_steps=req.max_steps, caller=caller
        )
    except ModelRateLimited as exc:
        raise HTTPException(status_code=429, detail=f"agent 模型调用被限流: {exc}")
    except NoModelBackend as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@agent_router.get("/v1/skills")
async def list_skills():
    return {"skills": [{"name": n, "description": b().description} for n, b in SKILLS.items()]}


@agent_router.post("/v1/skills/{name}/run")
async def run_skill(name: str, req: SkillRunRequest, request: Request,
                    authorization: str | None = Header(default=None)):
    """跑一个 skill（报表/建议）。同 task_id 重提交触发断点续跑。"""
    caller = _resolve_caller(request, authorization)
    skill = build_skill(name, **req.params)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"未知 skill: {name}")
    try:
        return await request.app.state.workflow.run(_task_id(req.task_id), skill, caller=caller)
    except NoModelBackend as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@agent_router.post("/v1/autopilot/run")
async def run_autopilot(request: Request, authorization: str | None = Header(default=None)):
    """跑一轮自治巡检（自监测→窄白名单自批→自动回滚/升级）。可挂调度定时触发。"""
    _resolve_caller(request, authorization)
    return await request.app.state.autopilot.run_cycle()


# ── Tier4 写操作护栏：propose → approve → apply ──────────


@agent_router.post("/v1/changes")
async def create_change(req: ChangeRequest, request: Request,
                        authorization: str | None = Header(default=None)):
    """提交配置变更提案（不直接生效）。归属到发起调用方。"""
    caller = _resolve_caller(request, authorization)
    p = await propose(_change_deps(request), req.field, req.value, req.rationale,
                      proposer=caller.caller_id)
    return p.model_dump()


@agent_router.get("/v1/changes")
async def list_changes(request: Request):
    return {"changes": [p.model_dump() for p in await request.app.state.proposals.list()]}


@agent_router.get("/v1/changes/{pid}")
async def get_change(pid: str, request: Request):
    p = await request.app.state.proposals.get(pid)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    return p.model_dump()


@agent_router.post("/v1/changes/{pid}/approve")
async def approve_change(pid: str, request: Request,
                         authorization: str | None = Header(default=None)):
    """人工批准 → 应用到运行时覆盖层（幂等）。"""
    caller = _resolve_caller(request, authorization)
    try:
        p = await approve(_change_deps(request), pid, approver=caller.caller_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return p.model_dump()


@agent_router.post("/v1/changes/{pid}/reject")
async def reject_change(pid: str, request: Request,
                        authorization: str | None = Header(default=None)):
    caller = _resolve_caller(request, authorization)
    try:
        p = await reject(_change_deps(request), pid, approver=caller.caller_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return p.model_dump()


@agent_router.get("/v1/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    """状态机当前态 + 中间结果（agent：轨迹+结论；workflow：各步输出）。"""
    tasks = request.app.state.tasks
    meta = await tasks.get_meta(task_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="task not found")
    out = {"task_id": task_id, **meta}
    if meta.get("type") == "agent":
        out["trajectory"] = await tasks.get_trajectory(task_id)
        out["conclusion"] = await tasks.get_step(task_id, "conclusion")
    else:
        out["steps"] = await tasks.list_steps(task_id)
    return out
