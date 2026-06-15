"""Redis 连接与 Lua 脚本加载。

限流/在途等全局状态放 Redis，多实例一致（M1 §7）。脚本用
register_script 注册（仅计算 SHA，不发起 IO），首次调用时执行。
"""
import os
from pathlib import Path

import redis.asyncio as aioredis

LUA_DIR = Path(__file__).parent / "lua"

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"


def get_redis_url() -> str:
    return os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)


def create_redis(url: str | None = None) -> aioredis.Redis:
    return aioredis.from_url(url or get_redis_url(), decode_responses=True)


def load_script(redis: aioredis.Redis, name: str):
    text = (LUA_DIR / name).read_text(encoding="utf-8")
    return redis.register_script(text)
