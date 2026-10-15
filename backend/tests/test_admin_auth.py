"""Environment-managed web authentication is independent of public API authentication."""

import json

import pytest
from pydantic import ValidationError

from sidewit.api import admin
from sidewit.config import Settings, get_settings

WEB_HEADERS = {"X-Sidewit-Web": "1"}


@pytest.fixture(autouse=True)
def admin_state(monkeypatch):
    for key in ("SIDEWIT_ADMIN_USERNAME", "SIDEWIT_ADMIN_PASSWORD", "SIDEWIT_ADMIN_COOKIE_SECURE"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    admin.get_admin_sessions.cache_clear()
    yield
    admin.get_admin_sessions.cache_clear()
    get_settings.cache_clear()


async def sign_in(client, username="admin", password="admin", **kwargs):
    return await client.post(
        "/api/web/session",
        headers=WEB_HEADERS,
        json={"username": username, "password": password},
        **kwargs,
    )


async def test_default_login_and_idempotent_logout(client):
    assert (await client.get("/api/web/session", headers=WEB_HEADERS)).status_code == 401
    assert (await client.get("/api/web/v1/contexts", headers=WEB_HEADERS)).status_code == 401
    logged_in = await sign_in(client)
    assert logged_in.status_code == 200
    assert logged_in.json() == {"username": "admin", "api_key_required": False}
    cookie = logged_in.headers["set-cookie"]
    for attribute in (
        "HttpOnly",
        "Path=/api/web",
        "SameSite=lax",
        f"Max-Age={admin.SESSION_SECONDS}",
    ):
        assert attribute in cookie
    assert "Secure" not in cookie
    assert logged_in.headers["cache-control"] == "no-store"
    assert (await client.get("/api/web/session", headers=WEB_HEADERS)).status_code == 200
    current = await client.get("/api/web/v1/contexts", headers=WEB_HEADERS)
    assert current.status_code == 200 and current.json() == {"items": []}
    assert current.headers["cache-control"] == "no-store"
    token = client.cookies.get(admin.COOKIE_NAME)
    for _ in range(2):
        assert (await client.delete("/api/web/session", headers=WEB_HEADERS)).status_code == 204
    assert not admin.get_admin_sessions().valid(token)
    client.cookies.set(admin.COOKIE_NAME, token, path=admin.COOKIE_PATH)
    assert (await client.get("/api/web/session", headers=WEB_HEADERS)).status_code == 401


async def test_env_credentials_and_secure_cookie(client, monkeypatch):
    monkeypatch.setenv("SIDEWIT_ADMIN_USERNAME", "관리자")
    monkeypatch.setenv("SIDEWIT_ADMIN_PASSWORD", "sensitive password 京都 🔑")
    monkeypatch.setenv("SIDEWIT_ADMIN_COOKIE_SECURE", "true")
    get_settings.cache_clear()
    assert (await sign_in(client)).status_code == 401
    good = await sign_in(client, "관리자", "sensitive password 京都 🔑")
    assert good.status_code == 200
    assert good.json()["username"] == "관리자"
    assert "Secure" in good.headers["set-cookie"]
    assert "sensitive" not in good.text


async def test_invalid_login_does_not_reveal_credentials(client, monkeypatch, caplog):
    monkeypatch.setenv("SIDEWIT_ADMIN_PASSWORD", "PRIVATE_ADMIN_PASSWORD")
    get_settings.cache_clear()
    for username, password in (("wrong", "PRIVATE_ADMIN_PASSWORD"), ("admin", "wrong")):
        response = await sign_in(client, username, password)
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_admin_credentials"
        assert "PRIVATE_ADMIN_PASSWORD" not in response.text
        assert admin.COOKIE_NAME not in response.headers.get("set-cookie", "")
    assert "PRIVATE_ADMIN_PASSWORD" not in caplog.text
    assert "PRIVATE_ADMIN_PASSWORD" not in repr(get_settings())


@pytest.mark.parametrize("password", ["\ud800", "\udfff", "PRIVATE_PASSWORD\ud800"])
async def test_invalid_unicode_password_is_rejected_without_exposing_input(
    client, password, caplog
):
    response = await client.post(
        "/api/web/session",
        headers=WEB_HEADERS | {"Content-Type": "application/json"},
        content=json.dumps({"username": "admin", "password": password}),
    )
    assert response.status_code == 422
    error = response.json()
    assert error["error"] == "invalid_request"
    assert error["details"]["errors"][0]["loc"] == ["body", "password"]
    assert "PRIVATE_PASSWORD" not in response.text and "PRIVATE_PASSWORD" not in caplog.text
    assert (await sign_in(client)).status_code == 200


async def test_web_and_public_api_credentials_cannot_replace_each_other(client, api_key):
    bearer = {"Authorization": f"Bearer {api_key}"}
    assert (await client.get("/api/v1/contexts")).status_code == 401
    assert (await client.get("/api/v1/contexts", headers=bearer)).status_code == 200
    assert (
        await client.get("/api/web/v1/contexts", headers=WEB_HEADERS | bearer)
    ).status_code == 401
    login = await sign_in(client)
    assert login.json()["api_key_required"] is True
    assert (await client.get("/api/web/v1/contexts", headers=WEB_HEADERS)).status_code == 200
    # Even explicitly forwarding the web cookie must not authenticate a public API call.
    cookie = {"Cookie": f"{admin.COOKIE_NAME}={client.cookies.get(admin.COOKIE_NAME)}"}
    assert (await client.get("/api/v1/contexts", headers=cookie)).status_code == 401
    assert (await client.get("/api/v1/health")).status_code == 200


async def test_browser_writes_share_the_store_without_requiring_the_public_api_key(client, api_key):
    await sign_in(client)
    created = await client.post(
        "/api/web/v1/contexts",
        headers=WEB_HEADERS,
        json={"name": "browser-context", "dialect": "postgresql"},
    )
    assert created.status_code == 201
    context = created.json()
    path = f"/contexts/{context['id']}"
    public = await client.get("/api/v1" + path, headers={"Authorization": f"Bearer {api_key}"})
    assert public.json() == context
    assert (await client.delete("/api/web/v1" + path, headers=WEB_HEADERS)).status_code == 204
    assert (
        await client.get("/api/v1" + path, headers={"Authorization": f"Bearer {api_key}"})
    ).status_code == 404
    await client.delete("/api/web/session", headers=WEB_HEADERS)
    assert (
        await client.post(
            "/api/web/v1/contexts",
            headers=WEB_HEADERS,
            json={"name": "signed-out", "dialect": "postgresql"},
        )
    ).status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/web/session"),
        ("DELETE", "/api/web/session"),
        ("GET", "/api/web/session"),
        ("GET", "/api/web/v1/contexts"),
        ("POST", "/api/web/v1/contexts"),
    ],
)
async def test_browser_routes_require_csrf_header(client, method, path):
    await sign_in(client)
    response = await client.request(method, path, json={"username": "admin", "password": "admin"})
    assert response.status_code == 403
    assert response.json()["error"] == "web_header_required"


async def test_cross_origin_preflight_is_not_allowed(client):
    response = await client.options(
        "/api/web/session",
        headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-Sidewit-Web,Content-Type",
        },
    )
    assert "access-control-allow-origin" not in response.headers


async def test_logout_expiry_restart_and_new_login_revoke_old_tokens(client, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(admin, "monotonic", lambda: now)
    await sign_in(client)
    original = client.cookies.get(admin.COOKIE_NAME)
    await sign_in(client)
    assert not admin.get_admin_sessions().valid(original)
    current = client.cookies.get(admin.COOKIE_NAME)
    assert admin.get_admin_sessions().valid(current)
    now += admin.SESSION_SECONDS
    assert (await client.get("/api/web/session", headers=WEB_HEADERS)).status_code == 401
    await sign_in(client)
    admin.get_admin_sessions.cache_clear()
    assert (await client.get("/api/web/session", headers=WEB_HEADERS)).status_code == 401
    monkeypatch.setenv("SIDEWIT_ADMIN_PASSWORD", "changed-after-restart")
    get_settings.cache_clear()
    assert (await sign_in(client)).status_code == 401
    assert (await sign_in(client, password="changed-after-restart")).status_code == 200


async def test_failed_login_throttle_expires_without_throttling_read_requests(client, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(admin, "monotonic", lambda: now)
    for _ in range(admin.MAX_LOGIN_FAILURES):
        assert (await sign_in(client, password="wrong")).status_code == 401
    limited = await sign_in(client)
    assert limited.status_code == 429 and limited.json()["details"]["retry_after"] > 0
    assert (await client.get("/api/v1/health")).status_code == 200
    now += admin.LOGIN_WINDOW_SECONDS
    assert (await sign_in(client)).status_code == 200


def test_session_store_is_bounded_and_retains_only_token_digests():
    sessions = admin.AdminSessions()
    first = sessions.create()
    for _ in range(admin.MAX_SESSIONS):
        last = sessions.create()
    assert not sessions.valid(first) and sessions.valid(last)
    assert len(sessions._sessions) == admin.MAX_SESSIONS
    assert all(isinstance(key, bytes) and len(key) == 32 for key in sessions._sessions)
    for index in range(admin.MAX_LOGIN_CLIENTS + 1):
        sessions.failed_login(str(index))
    assert len(sessions._failures) == admin.MAX_LOGIN_CLIENTS


def test_empty_admin_settings_use_default_credentials():
    settings = Settings(admin_username="", admin_password="")
    assert (
        settings.admin_username == "admin" and settings.admin_password.get_secret_value() == "admin"
    )


def test_invalid_admin_configuration_does_not_print_the_password():
    password = "PRIVATE_ADMIN_PASSWORD" * 60
    with pytest.raises(ValidationError) as error:
        Settings(admin_password=password)
    assert password not in str(error.value)
