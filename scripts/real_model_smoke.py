"""真实模型冲烟 —— 验证 OpenAI 兼容适配器 + 原生 function-calling 的 ReAct 闭环。

四家均为 OpenAI 兼容协议，一个适配器全覆盖。需要对应 API key（环境变量）。

用法（PowerShell）：
    $env:DEEPSEEK_API_KEY="sk-..."
    D:\\Python\\envs\\agent_project\\python.exe scripts/real_model_smoke.py deepseek
    D:\\Python\\envs\\agent_project\\python.exe scripts/real_model_smoke.py deepseek --fault
  provider 省略则自动选第一个已设 key 的；可用 $env:SMOKE_MODEL 覆盖模型名。

模式：
  默认      A.直连文本（连通/鉴权/解析）+ B.ReAct 健康自检（原生 FC 单步）。
  --fault   注入故障流量（mock 后端失败→熔断+遥测出错）后，让真模型 agent 多步排查根因。
"""
import asyncio
import os
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/12"

# (base_url, key 环境变量名, 默认模型)
PRESETS = {
    "openai":   ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4o-mini"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "qwen":     ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", "qwen-plus"),
    "kimi":     ("https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", "moonshot-v1-8k"),
    "glm":      ("https://open.bigmodel.cn/api/paas/v4", "ZHIPUAI_API_KEY", "glm-4-flash"),
}


def _pick_provider() -> str:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args and args[0] in PRESETS:
        return args[0]
    if os.environ.get("SMOKE_PROVIDER") in PRESETS:
        return os.environ["SMOKE_PROVIDER"]
    for name, (_, key_env, _m) in PRESETS.items():
        if os.environ.get(key_env):
            return name
    print("未检测到任何 provider 的 API key。请设置其一后重试：")
    for name, (_, key_env, _m) in PRESETS.items():
        print(f"  {name:9s} → $env:{key_env}")
    sys.exit(1)


def _real_backend(provider, base_url, key_env, real_model) -> dict:
    return {"name": provider, "model": "gpt", "address": "real", "provider": "openai_compatible",
            "base_url": base_url, "api_key_env": key_env, "real_model": real_model}


def _write_cfg(cfg: dict) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
        return f.name


async def _flush() -> None:
    from app.redis_client import create_redis
    r = create_redis(); await r.flushdb(); await r.aclose()


async def run_basic(provider, base_url, key_env, real_model):
    from app.config_models import BackendConfig
    from app.model import OpenAICompatibleClient
    from app.model.contract import Message, ModelRequest

    print("\n[A] 直连文本调用 …")
    backend = BackendConfig(**_real_backend(provider, base_url, key_env, real_model))
    resp = await OpenAICompatibleClient().call(backend, ModelRequest(
        messages=[Message(role="user", content="用一句话证明你在线，并说出你是谁。")]))
    print(f"    content: {resp.content}")
    print(f"    usage  : {resp.usage}")

    print("\n[B] 跑真实 ReAct 健康自检（原生 FC）…")
    cfg = {
        "callers": [{"credential": "smoke", "caller_id": "svc:smoke", "type": "machine",
                     "owner": "冲烟", "priority": "high",
                     "rate_limit": {"rate_per_sec": 1000, "burst": 1000}, "allowed_models": ["gpt"]}],
        "backends": [_real_backend(provider, base_url, key_env, real_model)],
        "thresholds": {"high_watermark": 1000, "low_watermark": 1},
    }
    await _flush()
    cfg_path = _write_cfg(cfg)
    from app.main import create_app
    app = create_app(cfg_path)
    out = await app.state.react.run(
        task_id="smoke-diag",
        goal="调用 get_backend_health 查看各后端状态，并用一句话报告是否健康。", caller=None)
    print(f"    status    : {out['status']}")
    print(f"    conclusion: {out.get('conclusion')}")
    traj = await app.state.tasks.get_trajectory("smoke-diag")
    print(f"    工具调用  : {[t['action'] for t in traj]}")
    await app.state.redis.aclose()
    os.unlink(cfg_path)


async def run_fault(provider, base_url, key_env, real_model):
    """注入故障：mock 后端 flaky(behavior=failure) 服务 model=chat；打几发失败流量
    → 熔断打开 + 遥测出错。再让真模型 agent 多步排查根因。"""
    from httpx import ASGITransport, AsyncClient

    cfg = {
        "callers": [{"credential": "smoke", "caller_id": "svc:smoke", "type": "machine",
                     "owner": "冲烟", "priority": "high",
                     "rate_limit": {"rate_per_sec": 1000, "burst": 1000},
                     "allowed_models": ["gpt", "chat"]}],
        "backends": [
            _real_backend(provider, base_url, key_env, real_model),          # 真模型：agent 推理用
            {"name": "flaky", "model": "chat", "address": "mock://flaky",     # 故障后端：被诊断对象
             "provider": "mock", "behavior": "failure"},
        ],
        "thresholds": {"high_watermark": 1000, "low_watermark": 1},
        "safeguard": {
            "retry": {"max_attempts": 1, "base_backoff_ms": 1, "max_backoff_ms": 10},
            "circuit": {"window_seconds": 60, "failure_rate": 0.5, "min_samples": 3,
                        "cooldown_seconds": 30, "half_open_probes": 1},
            "degrade": {"fallback_model": {}, "fallback_response": "服务繁忙"}},
    }
    await _flush()
    cfg_path = _write_cfg(cfg)
    from app.main import create_app
    app = create_app(cfg_path)

    print("\n[1] 注入失败流量：对 model=chat（后端 flaky）打 8 发 …")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for _ in range(8):
            await c.post("/v1/infer", json={"model": "chat", "input": "x"},
                         headers={"Authorization": "Bearer smoke"})
        health = (await c.get("/health")).json()
    circuits = {b["name"]: b["circuit"] for b in health["backends"]}
    agg = await app.state.telemetry.aggregate(300, group_by="model")
    print(f"    熔断态  : {circuits}")
    print(f"    遥测(chat): {agg.get('chat')}")

    print("\n[2] 真模型 ReAct 排查根因（开放目标，看是否多步调工具）…")
    out = await app.state.react.run(
        task_id="fault-diag",
        goal=("最近有部分请求失败或被兜底。请用工具排查：哪个模型/后端在出问题、"
              "错误类型是什么、当前熔断状态如何，并给出根因和处置建议。"),
        caller=None, max_steps=6)
    print(f"    status    : {out['status']}")
    traj = await app.state.tasks.get_trajectory("fault-diag")
    print(f"    工具调用链: {[t['action'] for t in traj]}  （共 {len(traj)} 步）")
    print(f"    conclusion:\n{out.get('conclusion')}")
    await app.state.redis.aclose()
    os.unlink(cfg_path)


async def main():
    provider = _pick_provider()
    base_url, key_env, default_model = PRESETS[provider]
    real_model = os.environ.get("SMOKE_MODEL", default_model)
    if not os.environ.get(key_env):
        print(f"provider={provider} 需要环境变量 {key_env}"); sys.exit(1)
    fault = "--fault" in sys.argv
    print(f"== provider={provider}  model={real_model}  mode={'fault' if fault else 'basic'} ==")
    if fault:
        await run_fault(provider, base_url, key_env, real_model)
    else:
        await run_basic(provider, base_url, key_env, real_model)
    print("\n冲烟完成。")


if __name__ == "__main__":
    asyncio.run(main())
