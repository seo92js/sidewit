from pydantic import BaseModel


class Issue(BaseModel):
    code: str
    message: str
    ref: str | None = None
