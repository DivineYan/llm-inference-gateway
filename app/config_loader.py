"""配置加载 —— 读 YAML，校验，建好查表索引，常驻内存。

启动时加载一次（M1 不做热更新）。提供两个高频查询：
- 凭证 → 画像（鉴权用，一次查表）
- 模型 → 健康后端列表（路由用）
"""
from pathlib import Path

import yaml

from app.config_models import (
    AgentConfig,
    BackendConfig,
    CallerProfile,
    GatewayConfig,
    SafeguardConfig,
    Thresholds,
)


class LoadedConfig:
    """加载并索引后的配置，供各闸门只读访问。"""

    def __init__(self, raw: GatewayConfig):
        self.backends: list[BackendConfig] = raw.backends
        self.thresholds: Thresholds = raw.thresholds
        self.safeguard: SafeguardConfig = raw.safeguard
        self.agent: AgentConfig = raw.agent
        # 凭证 → 画像（剔除 credential 字段，画像本身不携带凭证）
        self._by_credential: dict[str, CallerProfile] = {
            entry.credential: CallerProfile(
                **entry.model_dump(exclude={"credential"})
            )
            for entry in raw.callers
        }

    def caller_by_credential(self, credential: str) -> CallerProfile | None:
        return self._by_credential.get(credential)

    def healthy_backends_for_model(self, model: str) -> list[BackendConfig]:
        return [
            b for b in self.backends if b.model == model and b.healthy
        ]

    @property
    def caller_count(self) -> int:
        return len(self._by_credential)

    @property
    def callers(self) -> list[CallerProfile]:
        """所有调用方画像（不含凭证），供观测/建议只读访问。"""
        return list(self._by_credential.values())


def load_config(path: str | Path) -> LoadedConfig:
    text = Path(path).read_text(encoding="utf-8")
    raw = GatewayConfig(**yaml.safe_load(text))
    return LoadedConfig(raw)
