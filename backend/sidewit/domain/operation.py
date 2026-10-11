from typing import Any, Literal

from pydantic import BaseModel

from sidewit.domain.dialect import Dialect
from sidewit.domain.params import ParamType


class SqlOperation(BaseModel):
    type: Literal["sql"] = "sql"
    dialect: Dialect
    statement: str
    parameters: dict[str, Any]
    parameter_types: dict[str, ParamType]
