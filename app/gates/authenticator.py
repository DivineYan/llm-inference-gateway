"""① Authenticator —— 鉴权与身份解析（M1 §4.1）。

校验凭证 → 注入调用方画像 → 校验请求模型是否在该调用方可用列表内。
鉴权无状态、无远程调用：画像预加载在内存，一次查表。
"""
from app.config_loader import LoadedConfig
from app.context import RequestContext
from app.errors import MODEL_NOT_ALLOWED, UNAUTHENTICATED, GatewayError


class Authenticator:
    name = "authenticator"

    def __init__(self, config: LoadedConfig):
        self.config = config

    async def check(self, ctx: RequestContext) -> None:
        if not ctx.credential:
            raise GatewayError(401, UNAUTHENTICATED, "缺少凭证")

        profile = self.config.caller_by_credential(ctx.credential)
        if profile is None:
            raise GatewayError(401, UNAUTHENTICATED, "凭证无效")

        ctx.caller_profile = profile

        if ctx.requested_model not in profile.allowed_models:
            raise GatewayError(
                403, MODEL_NOT_ALLOWED, f"模型 {ctx.requested_model} 不在可用列表"
            )
