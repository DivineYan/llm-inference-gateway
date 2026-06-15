"""Workflow Runner —— M3 §4.5（Tier 2/3）。

执行声明式 skill（有序步骤模板）：tool 步走工具注册表，model 步走 ModelGateway。
步骤间传上下文：args/prompt 里的 {{step_id}} 在执行前用前序步骤输出替换。

幂等/断点续跑（TD-4）：每步成功即写检查点；重跑时有检查点的步骤直接读取
跳过，只补跑未完成的。任一步失败 → 任务转 partial、停止，返回已完成部分。
"""
import json
import re
from dataclasses import dataclass, field

from app.agent.state import PARTIAL, SUCCESS, TaskStore
from app.model.contract import Message, ModelRequest

_REF = re.compile(r"\{\{(\w+)\}\}")


def _resolve(value, results: dict):
    """递归解析 {{step_id}} 引用：整串精确匹配→替换为对象；内联→替换为 JSON 串。"""
    if isinstance(value, str):
        exact = _REF.fullmatch(value.strip())
        if exact:
            return results.get(exact.group(1))
        return _REF.sub(
            lambda m: json.dumps(results.get(m.group(1)), ensure_ascii=False), value
        )
    if isinstance(value, dict):
        return {k: _resolve(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, results) for v in value]
    return value


@dataclass
class Step:
    id: str
    type: str               # "tool" | "model"
    tool: str | None = None
    args: dict | None = None
    prompt: str | None = None
    model: str = "gpt"      # model 步用哪个模型


@dataclass
class Skill:
    name: str
    description: str
    steps: list[Step]
    params: dict = field(default_factory=dict)


class WorkflowRunner:
    def __init__(self, registry, gateway, tasks: TaskStore):
        self.registry = registry
        self.gateway = gateway
        self.tasks = tasks

    async def run(self, task_id: str, skill: Skill, caller=None) -> dict:
        await self.tasks.create(task_id, "workflow", {"skill": skill.name})
        results: dict = {}
        decision_log: list[dict] = []

        for step in skill.steps:
            if await self.tasks.has_step(task_id, step.id):       # 续跑：命中检查点跳过
                results[step.id] = await self.tasks.get_step(task_id, step.id)
                decision_log.append({"step": step.id, "outcome": "checkpoint_skip"})
                continue
            try:
                out = await self._run_step(step, results, caller)
            except Exception as exc:                              # 任一步失败 → partial
                await self.tasks.set_status(task_id, PARTIAL)
                decision_log.append({"step": step.id, "outcome": "failed", "error": str(exc)})
                return {"task_id": task_id, "status": PARTIAL, "failed_step": step.id,
                        "results": results, "decision_log": decision_log}
            results[step.id] = out
            await self.tasks.save_step(task_id, step.id, out)
            decision_log.append({"step": step.id, "outcome": "ok"})

        await self.tasks.set_status(task_id, SUCCESS)
        return {"task_id": task_id, "status": SUCCESS, "results": results,
                "report": results.get(skill.steps[-1].id), "decision_log": decision_log}

    async def _run_step(self, step: Step, results: dict, caller=None):
        if step.type == "tool":
            return await self.registry.execute(step.tool, _resolve(step.args or {}, results))
        if step.type == "model":
            prompt = _resolve(step.prompt, results)
            text = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
            req = ModelRequest(messages=[Message(role="user", content=text)])
            resp, _ = await self.gateway.call(step.model, req, caller=caller)
            return resp.content
        raise ValueError(f"未知步骤类型: {step.type}")
