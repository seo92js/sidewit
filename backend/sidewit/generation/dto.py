"""Provider DTO: the structured output requested from the LLM (SPEC §6.2).

Used only inside `sidewit.generation`. Never returned from the public API and never stored
as a domain model; `service` converts it. Fields have no defaults so that providers with
strict structured output (every property required, nullable via null) accept the schema.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from sidewit.domain.params import ParamType
from sidewit.generation.clarification import LLMClarification


class LLMParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: ParamType
    # Always a string: union value types are not portable across providers.
    value: str


class LLMToolSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str


class LLMGenerationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "ambiguous", "unanswerable"]
    reason: str | None
    clarifications: list[LLMClarification]
    statement: str | None
    parameters: list[LLMParameter]
    tool_suggestion: LLMToolSuggestion | None
