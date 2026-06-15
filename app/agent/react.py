"""ReAct 诊断 Agent —— M3 §4.4（Tier 1，招牌）。

有界 reason→act→observe 循环：模型自主决定调哪个工具（FR-2.2），观测后继续，
直至给出结论或达到 max_steps（防不收敛）。

幂等/续跑（TD-4）：
- 每步 (action, observation) 写轨迹检查点；最终结论也落检查点。
- 续跑时：已 SUCCESS 的任务直接返回缓存结论（零模型调用）；未完成的从已记录
  轨迹重建上下文再继续。Tier1 工具全只读，即使重执行也无副作用——双重安全。

harness 约束（见 M3_HARNESS.md）：
- H13 超时：工具/模型调用包 asyncio.wait_for，挂住即硬中止。
- H12a 观测截断：超大观测注入上下文前截断（防爆窗口/成本）。
- H12b 上下文压缩：_maybe_compress 钩子（阈值触发，暂未启用）。
- H16 重复检测：同一 (name,args) 调用超限 → 先提示、再失败（防卡死循环）。
- H15/H17 修复有界：连续错误观测（坏参数/坏调用）超 max_repair → 判失败。
"""
import asyncio
import json

from app.agent.state import FAILED, SUCCESS, TaskStore
from app.agent.tools.registry import ToolError, ToolNotFound
from app.model.contract import Message, ModelRequest, ToolCall

SYSTEM_PROMPT = (
    "你是企业AI Provider的诊断助手。可调用只读工具调查平台运行状况"
    "（指标/调用链/后端健康/配置）。一步步排查，定位根因后给出简洁中文结论。"
)


class ReActAgent:
    def __init__(self, gateway, registry, tasks: TaskStore, max_steps: int = 6,
                 model: str = "gpt", *, model_timeout_s: float = 60, tool_timeout_s: float = 5,
                 max_obs_chars: int = 2000, max_repair: int = 2, max_repeat: int = 2):
        self.gateway = gateway
        self.registry = registry
        self.tasks = tasks
        self.max_steps = max_steps
        self.model = model
        self.model_timeout_s = model_timeout_s
        self.tool_timeout_s = tool_timeout_s
        self.max_obs_chars = max_obs_chars
        self.max_repair = max_repair
        self.max_repeat = max_repeat

    async def run(self, task_id: str, goal: str, max_steps: int | None = None,
                  caller=None) -> dict:
        max_steps = max_steps or self.max_steps
        fresh = await self.tasks.create(task_id, "agent", {"goal": goal})
        if not fresh:  # 续跑：已完成则返回缓存结论（零模型调用）
            cached = await self.tasks.get_step(task_id, "conclusion")
            if cached is not None:
                return {"task_id": task_id, "status": SUCCESS, "conclusion": cached, "resumed": True}

        messages = self._rebuild(goal, await self.tasks.get_trajectory(task_id))
        tools = self.registry.list_schemas()
        last_sig: tuple | None = None   # H16：上一次调用签名
        repeat_run = 0                  # H16：连续相同调用次数（换调用即重置）
        error_streak = 0                # H15/H17：连续错误观测计数

        for _ in range(max_steps):
            self._maybe_compress(messages)  # H12b 钩子（暂为 no-op）
            try:  # H13：模型调用超时硬中止（真 LLM 用更宽的 model_timeout_s）
                resp, _be = await asyncio.wait_for(
                    self.gateway.call(self.model, ModelRequest(messages=messages, tools=tools),
                                      caller=caller),
                    timeout=self.model_timeout_s,
                )
            except asyncio.TimeoutError:
                return await self._fail(task_id, "model_timeout")

            if not resp.wants_tools:  # 给出最终结论
                await self.tasks.save_step(task_id, "conclusion", resp.content)
                await self.tasks.set_status(task_id, SUCCESS)
                return {"task_id": task_id, "status": SUCCESS, "conclusion": resp.content,
                        "steps": len(await self.tasks.get_trajectory(task_id))}

            # 原生 FC：先回放模型发起工具调用的 assistant 回合（含全部 tool_calls）
            messages.append(Message(role="assistant", content=resp.content or "",
                                    tool_calls=resp.tool_calls))
            for call in resp.tool_calls:
                sig = (call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=False))
                repeat_run = repeat_run + 1 if sig == last_sig else 1  # H16：连续相同才累计
                last_sig = sig
                if repeat_run > self.max_repeat:  # H16：提示后仍连续重复 → 卡死，失败
                    return await self._fail(task_id, "stuck_repeating")
                if repeat_run == self.max_repeat:  # H16：到阈值 → 用工具结果回填提示，不执行
                    messages.append(Message(role="tool", tool_call_id=call.id,
                        content="已多次调用且参数相同，请换思路或直接给出结论。"))
                    continue

                obs = await self._exec(call.name, call.arguments)  # 含 H13 工具超时 + H15 校验
                error_streak = error_streak + 1 if _is_error(obs) else 0
                if error_streak > self.max_repair:  # H15/H17：修复耗尽 → 失败
                    return await self._fail(task_id, "repair_exhausted")

                # tool 结果必须配对到对应 tool_call_id（原生 FC 协议要求每个调用都被回答）
                messages.append(Message(role="tool", tool_call_id=call.id,
                                        content=self._observe(obs)))  # H12a 截断
                await self.tasks.append_trajectory(
                    task_id, {"action": call.name, "arguments": call.arguments,
                              "observation": obs, "id": call.id})

        return await self._fail(task_id, "max_steps_exceeded")

    def _rebuild(self, goal: str, trajectory: list[dict]) -> list[Message]:
        """从已记录轨迹重建对话上下文（续跑：不重执行工具，重放观测）。

        每条轨迹重建为一组 assistant(tool_call)+tool(result)，id 配对一致（原生 FC）。
        """
        msgs = [Message(role="system", content=SYSTEM_PROMPT),
                Message(role="user", content=goal)]
        for i, e in enumerate(trajectory):
            cid = e.get("id") or f"call_{i}"
            msgs.append(Message(role="assistant", tool_calls=[
                ToolCall(id=cid, name=e["action"], arguments=e.get("arguments", {}))]))
            msgs.append(Message(role="tool", tool_call_id=cid,
                                content=self._observe(e["observation"])))
        return msgs

    async def _exec(self, name: str, arguments: dict):
        """执行工具：超时硬中止(H13)、错误回填模型而非崩溃(H15/H17)。"""
        try:
            return await asyncio.wait_for(
                self.registry.execute(name, arguments), timeout=self.tool_timeout_s)
        except asyncio.TimeoutError:
            return {"error": f"工具 {name} 超时（>{self.tool_timeout_s}s）"}
        except (ToolNotFound, ToolError) as exc:
            return {"error": str(exc)}

    def _observe(self, obs) -> str:
        """H12a：观测序列化并截断到上限，避免爆上下文。"""
        text = json.dumps(obs, ensure_ascii=False, default=str)
        if len(text) > self.max_obs_chars:
            return text[:self.max_obs_chars] + " …(truncated)"
        return text

    def _maybe_compress(self, messages: list[Message]) -> None:
        """H12b 钩子：累积上下文超阈值时压缩老轮次。阈值触发，暂未启用。"""
        return  # TODO(H12b): 累积 token 超阈值 → 摘要最旧若干轮，替换为一条 summary

    async def _fail(self, task_id: str, reason: str) -> dict:
        await self.tasks.set_status(task_id, FAILED)
        return {"task_id": task_id, "status": FAILED, "reason": reason,
                "steps": len(await self.tasks.get_trajectory(task_id))}


def _is_error(obs) -> bool:
    return isinstance(obs, dict) and "error" in obs
