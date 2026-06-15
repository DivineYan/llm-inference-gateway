"""④ Router —— 模型路由（M1 §4.4 / M2 §1）。

在"请求模型"对应的健康后端中按权重排出一个有序候选列表（M2）：
- 表头由平滑加权轮询（SWRR）轮转产出 —— 多次请求的表头分布稳定逼近权重比，
  保留 M1 的加权语义；
- 其余后端作为 failover 顺序，供 M2 保障执行器在表头熔断/失败时依次顶上。
无任何健康后端 → 503 no_backend。

SWRR 选择不含 await，在 asyncio 单线程下天然原子，无需加锁或上 Redis。

Tier4（运行时覆盖层）：weight/healthy 取**有效值**（覆盖优先于文件值），每请求按
有效配置算签名；签名变了才重建 SWRR（覆盖很少变，平滑状态在不变时跨请求保留）。
动态调权（M1 推迟的 P1）即由此落地。
"""
from app.config_loader import LoadedConfig
from app.config_models import BackendConfig
from app.context import RequestContext
from app.errors import NO_BACKEND, GatewayError


class SmoothWeightedRoundRobin:
    """Nginx 风格平滑加权轮询。"""

    def __init__(self, backends: list[BackendConfig]):
        self._entries = [{"backend": b, "weight": b.weight, "current": 0} for b in backends]
        self._total = sum(b.weight for b in backends)

    def pick(self) -> BackendConfig:
        best = None
        for e in self._entries:
            e["current"] += e["weight"]
            if best is None or e["current"] > best["current"]:
                best = e
        best["current"] -= self._total
        return best["backend"]

    def rank(self) -> list[BackendConfig]:
        """有序候选：SWRR 轮转出的表头 + 其余后端（failover 顺序，M2）。"""
        winner = self.pick()
        rest = [e["backend"] for e in self._entries if e["backend"] is not winner]
        return [winner, *rest]


class Router:
    name = "router"

    def __init__(self, config: LoadedConfig, overrides):
        self.config = config
        self.overrides = overrides
        # 缓存：model → (有效配置签名, SWRR)。签名不变时复用，保留平滑状态。
        self._cache: dict[str, tuple] = {}

    async def _effective(self, model: str) -> list[BackendConfig]:
        """该模型的有效健康后端（weight/healthy 取覆盖优先）。"""
        out = []
        for b in self.config.backends:
            if b.model != model:
                continue
            healthy = await self.overrides.get(f"backend:{b.name}:healthy", b.healthy)
            if not healthy:
                continue
            weight = await self.overrides.get(f"backend:{b.name}:weight", b.weight)
            out.append(b.model_copy(update={"weight": weight}))
        return out

    async def check(self, ctx: RequestContext) -> None:
        eff = await self._effective(ctx.requested_model)
        if not eff:
            raise GatewayError(503, NO_BACKEND, f"模型 {ctx.requested_model} 无可用后端")

        sig = tuple((b.name, b.weight) for b in eff)
        cached = self._cache.get(ctx.requested_model)
        if cached is None or cached[0] != sig:       # 有效配置变了才重建
            cached = (sig, SmoothWeightedRoundRobin(eff))
            self._cache[ctx.requested_model] = cached

        ctx.candidates = cached[1].rank()
        ctx.chosen_backend = ctx.candidates[0]  # 临时默认；执行器以实际服务的后端覆盖
