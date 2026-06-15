"""M2-T4：Degrader —— 备用模型切换 + 兜底响应（M2 §4.3）。"""
from app.config_models import DegradeConfig
from app.safeguard.degrade import Degrader


def _degrader(**over):
    cfg = DegradeConfig(
        fallback_model={"gpt": "local"},
        fallback_response="服务繁忙，请稍后重试",
    ).model_copy(update=over)
    return Degrader(cfg)


def test_fallback_for_returns_backup_model():
    d = _degrader()
    assert d.fallback_for("gpt") == "local"


def test_fallback_for_returns_none_when_unmapped():
    d = _degrader()
    assert d.fallback_for("claude") is None


def test_fallback_response_is_marked_degraded():
    d = _degrader()
    resp = d.fallback_response("gpt")
    assert resp["degraded"] is True
    assert resp["served_fallback"] is True
    assert resp["output"] == "服务繁忙，请稍后重试"
    assert resp["model"] == "gpt"
    assert resp["backend"] is None
