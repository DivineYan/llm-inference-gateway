"""pytest 公共夹具。

- 测试统一用 Redis db 15，避免污染默认 db。
- client：默认配置的内存 HTTP 客户端（基于 ASGITransport，无需起端口）。
- make_client：用指定配置文件构造客户端（限流/调度等需要小阈值时用）。
- flush_redis：清空测试 db，保证用例间隔离。
"""
import os

os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.redis_client import create_redis


@pytest_asyncio.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def make_client():
    clients = []

    async def _make(config_path: str | None = None) -> AsyncClient:
        app = create_app(config_path)
        transport = ASGITransport(app=app)
        c = AsyncClient(transport=transport, base_url="http://test")
        clients.append(c)
        return c

    yield _make
    for c in clients:
        await c.aclose()


@pytest_asyncio.fixture
async def flush_redis():
    r = create_redis()
    await r.flushdb()
    yield r
    await r.aclose()
