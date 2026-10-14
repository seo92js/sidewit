"""Build UUID API paths for Tools created by named test fixtures."""

import uuid

from sidewit.api.state import get_store
from sidewit.main import app


def tool_path(name: str, suffix: str = "", prefix: str = "/api/v1/tools") -> str:
    store = app.dependency_overrides[get_store]()
    tool = store.tool(name)
    tool_id = tool.id if tool is not None else uuid.UUID(int=0)
    return f"{prefix}/{tool_id}{suffix}"
