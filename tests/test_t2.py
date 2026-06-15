"""T2：配置中心 —— 加载 YAML、建查表索引。"""
from app.config_loader import load_config


def test_load_default_config():
    cfg = load_config("config.yaml")
    assert cfg.caller_count == 2
    assert len(cfg.backends) == 4


def test_caller_lookup_by_credential():
    cfg = load_config("config.yaml")
    machine = cfg.caller_by_credential("key-search-machine")
    assert machine is not None
    assert machine.caller_id == "svc:search"
    assert machine.type == "machine"
    assert machine.priority == "high"
    assert "gpt" in machine.allowed_models

    human = cfg.caller_by_credential("key-zhangsan-human")
    assert human.type == "human"
    assert human.priority == "low"

    # 画像不应携带凭证字段
    assert not hasattr(machine, "credential")

    assert cfg.caller_by_credential("bogus") is None


def test_healthy_backends_for_model():
    cfg = load_config("config.yaml")
    # gpt 有两个健康后端
    gpt = cfg.healthy_backends_for_model("gpt")
    assert {b.name for b in gpt} == {"gpt-a", "gpt-b"}
    # local 仅有的后端不健康 → 空列表（将触发 503）
    assert cfg.healthy_backends_for_model("local") == []
