async def test_health_ok(client):
    res = await client.get("/api/v1/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "storage": "ok"}


async def test_health_reports_unusable_directory(client, store, monkeypatch):
    monkeypatch.setattr(store, "healthy", lambda: False)
    res = await client.get("/api/v1/health")
    assert res.status_code == 503
    assert res.json()["storage"] == "unavailable"
