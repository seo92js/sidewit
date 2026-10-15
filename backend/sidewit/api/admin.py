"""One environment-configured administrator; web sessions never authenticate the public API."""

import hashlib
import math
import secrets
from collections import OrderedDict
from functools import lru_cache
from time import monotonic

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from sidewit.config import get_settings
from sidewit.errors import SidewitError

COOKIE_NAME = "sidewit_admin"
COOKIE_PATH = "/api/web"
SESSION_SECONDS = 12 * 60 * 60
MAX_SESSIONS = 32
LOGIN_WINDOW_SECONDS = 60
MAX_LOGIN_FAILURES = 10
MAX_LOGIN_CLIENTS = 256
WEB_HEADER = "x-sidewit-web"


class AdminSessions:
    def __init__(self) -> None:
        self._sessions: OrderedDict[bytes, float] = OrderedDict()
        self._failures: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def create(self) -> str:
        now = monotonic()
        for key, expires in list(self._sessions.items()):
            if expires <= now:
                del self._sessions[key]
        token = secrets.token_urlsafe(32)
        self._sessions[_digest(token)] = now + SESSION_SECONDS
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)
        return token

    def valid(self, token: str | None) -> bool:
        if not token or len(token) > 128:
            return False
        key = _digest(token)
        expires = self._sessions.get(key)
        if expires is None:
            return False
        if expires <= monotonic():
            del self._sessions[key]
            return False
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(_digest(token), None)

    def check_login(self, client: str) -> None:
        started, count = self._failures.get(client, (monotonic(), 0))
        remaining = started + LOGIN_WINDOW_SECONDS - monotonic()
        if remaining <= 0:
            self._failures.pop(client, None)
        elif count >= MAX_LOGIN_FAILURES:
            raise SidewitError(
                429,
                "login_rate_limited",
                "Too many failed sign-in attempts. Try again shortly.",
                {"retry_after": math.ceil(remaining)},
            )

    def failed_login(self, client: str) -> None:
        started, count = self._failures.get(client, (monotonic(), 0))
        self._failures[client] = (started, count + 1)
        self._failures.move_to_end(client)
        while len(self._failures) > MAX_LOGIN_CLIENTS:
            self._failures.popitem(last=False)

    def successful_login(self, client: str) -> None:
        self._failures.pop(client, None)


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@lru_cache
def get_admin_sessions() -> AdminSessions:
    return AdminSessions()


async def require_web_header(request: Request) -> None:
    # Cross-origin forms cannot send this header, and no CORS policy allows cross-origin
    # scripts to preflight it. Require it on login/logout as well as management requests.
    if request.headers.get(WEB_HEADER) != "1":
        raise SidewitError(403, "web_header_required", "Missing web request header.")


async def require_admin_session(
    request: Request, response: Response, _: None = Depends(require_web_header)
) -> None:
    if not get_admin_sessions().valid(request.cookies.get(COOKIE_NAME)):
        raise SidewitError(401, "admin_session_required", "Sign in to continue.")
    response.headers["Cache-Control"] = "no-store"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=1, max_length=1024)

    @field_validator("password")
    @classmethod
    def _valid_unicode_password(cls, value: SecretStr) -> SecretStr:
        try:
            value.get_secret_value().encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Password must contain valid Unicode.") from None
        return value


class AdminSession(BaseModel):
    username: str
    api_key_required: bool


def _session_info() -> AdminSession:
    settings = get_settings()
    return AdminSession(username=settings.admin_username, api_key_required=bool(settings.api_key))


router = APIRouter(dependencies=[Depends(require_web_header)])


@router.post("/session")
async def login(body: LoginRequest, request: Request, response: Response) -> AdminSession:
    settings = get_settings()
    sessions = get_admin_sessions()
    client = request.client.host if request.client else "unknown"
    sessions.check_login(client)
    username_ok = secrets.compare_digest(body.username.encode(), settings.admin_username.encode())
    password_ok = secrets.compare_digest(
        body.password.get_secret_value().encode(),
        settings.admin_password.get_secret_value().encode(),
    )
    if not (username_ok & password_ok):
        sessions.failed_login(client)
        raise SidewitError(401, "invalid_admin_credentials", "Invalid username or password.")
    sessions.successful_login(client)
    sessions.revoke(request.cookies.get(COOKIE_NAME))
    token = sessions.create()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_SECONDS,
        path=COOKIE_PATH,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return _session_info()


@router.get("/session", dependencies=[Depends(require_admin_session)])
async def current_session(response: Response) -> AdminSession:
    response.headers["Cache-Control"] = "no-store"
    return _session_info()


@router.delete("/session", status_code=204)
async def logout(request: Request) -> Response:
    get_admin_sessions().revoke(request.cookies.get(COOKIE_NAME))
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        COOKIE_NAME,
        path=COOKIE_PATH,
        httponly=True,
        secure=get_settings().admin_cookie_secure,
        samesite="lax",
    )
    return response
