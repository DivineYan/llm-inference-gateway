"""DispatchingModelClient —— 按 backend.provider 分流（M3 §4.1）。

editor 持有一个 ModelClient 即可（ModelGateway/executor 都把 backend 传进 call），
本类按每个 backend 的 provider 选具体实现：mock → MockModelClient，
openai_compatible → OpenAICompatibleClient。新增 provider 只在此登记。
"""
from app.config_models import BackendConfig
from app.model.contract import ModelRequest, ModelResponse
from app.model.mock_client import MockModelClient
from app.model.openai_client import OpenAICompatibleClient


class DispatchingModelClient:
    def __init__(self, clients: dict | None = None):
        # 默认：mock + openai_compatible 各一个实例（无状态，可共享）
        self.clients = clients or {
            "mock": MockModelClient(),
            "openai_compatible": OpenAICompatibleClient(),
        }

    async def call(self, backend: BackendConfig, req: ModelRequest) -> ModelResponse:
        client = self.clients.get(backend.provider)
        if client is None:
            raise ValueError(f"未注册的 provider: {backend.provider}")
        return await client.call(backend, req)
