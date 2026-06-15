"""T3：① Authenticator —— 凭证校验 + 画像注入 + 模型权限。"""


async def test_missing_credential_401(client):
    resp = await client.post("/v1/infer", json={"model": "gpt", "input": "x"})
    assert resp.status_code == 401
    assert resp.json()["reason"] == "unauthenticated"


async def test_invalid_credential_401(client):
    resp = await client.post(
        "/v1/infer",
        json={"model": "gpt", "input": "x"},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 401
    assert resp.json()["reason"] == "unauthenticated"


async def test_valid_credential_passes(client):
    resp = await client.post(
        "/v1/infer",
        json={"model": "gpt", "input": "x"},
        headers={"Authorization": "Bearer key-search-machine"},
    )
    assert resp.status_code == 200
    assert resp.json()["caller"] == "svc:search"


async def test_model_not_allowed_403(client):
    # 张三只能用 gpt，请求 claude → 403
    resp = await client.post(
        "/v1/infer",
        json={"model": "claude", "input": "x"},
        headers={"Authorization": "Bearer key-zhangsan-human"},
    )
    assert resp.status_code == 403
    assert resp.json()["reason"] == "model_not_allowed"


async def test_trace_id_present_in_response(client):
    resp = await client.post(
        "/v1/infer",
        json={"model": "gpt", "input": "x"},
        headers={"Authorization": "Bearer key-search-machine"},
    )
    assert resp.json()["trace_id"]
