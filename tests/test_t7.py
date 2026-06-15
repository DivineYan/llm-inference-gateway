"""T7：⑤ Dispatcher + mock 后端 + inflight guard。"""
from app.inflight import INFLIGHT_KEY, inflight_guard

HI = {"Authorization": "Bearer key-search-machine"}


async def test_dispatch_returns_chosen_backend(make_client, flush_redis):
    c = await make_client()
    r = await c.post("/v1/infer", json={"model": "gpt", "input": "hello"}, headers=HI)
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] in {"gpt-a", "gpt-b"}
    # mock 输出带上处理它的后端名，可看出路由到了哪个
    assert body["backend"] in body["output"]
    assert "hello" in body["output"]


async def test_inflight_released_after_request(make_client, flush_redis):
    c = await make_client()
    await c.post("/v1/infer", json={"model": "gpt", "input": "x"}, headers=HI)
    # 请求结束后在途归零（guard 的 finally 已 -1）
    assert await flush_redis.zcard(INFLIGHT_KEY) == 0


async def test_inflight_guard_increments_then_releases(flush_redis):
    await flush_redis.delete(INFLIGHT_KEY)
    async with inflight_guard(flush_redis, "trace-x"):
        assert await flush_redis.zcard(INFLIGHT_KEY) == 1
    assert await flush_redis.zcard(INFLIGHT_KEY) == 0
