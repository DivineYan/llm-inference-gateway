"""T1：骨架 + 推理接口可收发。

注：T3 起接口已接入鉴权，回显需带合法凭证。此处验证骨架收发与字段校验。
"""

AUTH = {"Authorization": "Bearer key-search-machine"}


async def test_infer_roundtrip(client):
    resp = await client.post(
        "/v1/infer", json={"model": "gpt", "input": "hello"}, headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "gpt"
    assert "hello" in body["output"]


async def test_infer_requires_fields(client):
    # 缺字段被 FastAPI 在进入端点前校验拦截（422）
    resp = await client.post("/v1/infer", json={"model": "gpt"}, headers=AUTH)
    assert resp.status_code == 422
