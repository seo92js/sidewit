import secrets
from typing import Annotated

from fastapi import Header

from sidewit.config import get_settings
from sidewit.errors import SidewitError


async def require_api_key(authorization: Annotated[str | None, Header()] = None) -> None:
    """Enforced only when SIDEWIT_API_KEY is set. /health is mounted without this."""
    secret = get_settings().api_key
    api_key = secret.get_secret_value() if secret else None
    if not api_key:
        return
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.encode(), api_key.encode()):
        raise SidewitError(401, "unauthorized", "Missing or invalid API key.")
