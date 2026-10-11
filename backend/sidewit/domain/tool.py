from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
ToolStatus = Literal["valid", "invalid", "stale"]


class ToolOperation(BaseModel):
    """Operation template stored on a Tool: no values; dialect comes from the Context."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["sql"] = "sql"
    statement: str = Field(min_length=1, max_length=64000)
