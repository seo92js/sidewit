from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The provider call failed or returned no usable structured output."""


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    parsed: T
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMProvider(Protocol):
    """Contract ends at the provider DTO: returns `schema` parsed from structured output.
    Validation, conversion and repair are the caller's job."""

    name: str
    model: str

    async def generate_structured(
        self, system: str, messages: list[Message], schema: type[T]
    ) -> StructuredResult[T]: ...
