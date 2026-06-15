"""任务状态与检查点 —— M3 §4.7（FR-2.3 / TD-4）。

状态机：pending → running → (success | failed | partial)。
检查点（断点续跑核心）：
- Workflow：每步输出存 task:{id}:step:{sid}，重跑时有则跳过。
- ReAct：轨迹 (action, observation) 存 task:{id}:trajectory，重跑时重放、不重执行工具。

幂等判定靠 create() 返回值区分"首次"与"续跑"，跳过靠 has_step / 轨迹长度。
所有 key 设 TTL，避免任务状态无界堆积。
"""
import json

# 状态机取值
PENDING = "pending"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"
PARTIAL = "partial"  # 部分步骤成功、某步失败

TASK_TTL = 86400  # 任务状态保留 1 天


class TaskStore:
    def __init__(self, redis):
        self.redis = redis

    @staticmethod
    def _meta(tid: str) -> str:
        return f"task:{tid}:meta"

    @staticmethod
    def _step(tid: str, sid: str) -> str:
        return f"task:{tid}:step:{sid}"

    @staticmethod
    def _traj(tid: str) -> str:
        return f"task:{tid}:trajectory"

    async def create(self, task_id: str, type_: str, meta: dict | None = None) -> bool:
        """创建任务元信息。已存在则返回 False（=续跑），不覆盖既有状态。"""
        key = self._meta(task_id)
        if await self.redis.exists(key):
            return False
        data = {"task_id": task_id, "type": type_, "status": RUNNING}
        if meta:
            data.update(meta)
        await self.redis.hset(key, mapping={k: json.dumps(v) for k, v in data.items()})
        await self.redis.expire(key, TASK_TTL)
        return True

    async def get_meta(self, task_id: str) -> dict | None:
        raw = await self.redis.hgetall(self._meta(task_id))
        if not raw:
            return None
        return {k: json.loads(v) for k, v in raw.items()}

    async def set_status(self, task_id: str, status: str) -> None:
        await self.redis.hset(self._meta(task_id), "status", json.dumps(status))

    # ── Workflow 步骤检查点 ──────────────────────────────
    async def save_step(self, task_id: str, step_id: str, result) -> None:
        key = self._step(task_id, step_id)
        await self.redis.set(key, json.dumps(result), ex=TASK_TTL)

    async def get_step(self, task_id: str, step_id: str):
        v = await self.redis.get(self._step(task_id, step_id))
        return json.loads(v) if v is not None else None

    async def has_step(self, task_id: str, step_id: str) -> bool:
        return bool(await self.redis.exists(self._step(task_id, step_id)))

    async def list_steps(self, task_id: str) -> dict:
        """收集该任务所有步骤检查点 {step_id: result}，供查询接口展示中间结果。"""
        prefix = self._step(task_id, "")
        out: dict = {}
        async for key in self.redis.scan_iter(match=f"{prefix}*"):
            v = await self.redis.get(key)
            out[key[len(prefix):]] = json.loads(v) if v is not None else None
        return out

    # ── ReAct 轨迹检查点 ─────────────────────────────────
    async def append_trajectory(self, task_id: str, entry: dict) -> None:
        key = self._traj(task_id)
        await self.redis.rpush(key, json.dumps(entry))
        await self.redis.expire(key, TASK_TTL)

    async def get_trajectory(self, task_id: str) -> list[dict]:
        items = await self.redis.lrange(self._traj(task_id), 0, -1)
        return [json.loads(i) for i in items]
