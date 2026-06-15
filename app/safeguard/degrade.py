"""③ Degrader —— 降级（M2 §4.3）。

提供两件降级所需的素材，"何时降级"由执行器（T5）决定：
- fallback_for(model)：查该模型的备用模型（只一级，防级联绕圈）。
- fallback_response(model)：备用也不可用时的预设兜底响应（结构正常、标记 degraded）。

降级对调用方透明但可观测：兜底响应是正常结构而非异常崩溃（NFR-3），
但带 degraded 标记，便于排查。
"""
from app.config_models import DegradeConfig


class Degrader:
    def __init__(self, config: DegradeConfig):
        self.cfg = config

    def fallback_for(self, model: str) -> str | None:
        """该模型的备用模型；没配则 None。"""
        return self.cfg.fallback_model.get(model)

    def fallback_response(self, model: str) -> dict:
        """全挂兜底：固定文本 + degraded 标记，保证永远有返回。"""
        return {
            "backend": None,
            "model": model,
            "output": self.cfg.fallback_response,
            "degraded": True,
            "served_fallback": True,
        }
