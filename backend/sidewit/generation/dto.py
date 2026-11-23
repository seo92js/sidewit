"""Provider DTO: the structured output requested from the LLM (SPEC §6.2).

Used only inside `sidewit.generation`. Never returned from the public API and never stored
as a domain model; `service` converts it. Fields have no defaults so that providers with
strict structured output (every property required, nullable via null) accept the schema.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt

from sidewit.domain.params import ParamType
from sidewit.domain.period import Period, PeriodKind
from sidewit.generation.clarification import LLMClarification


class LLMParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: ParamType
    # Always a string: union value types are not portable across providers.
    value: str


class LLMPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: PeriodKind
    days: StrictInt | None

    def period(self) -> Period:
        """Raises pydantic.ValidationError unless days match the kind (PERIOD_RULE)."""
        return Period(kind=self.kind, days=self.days)


class LLMPeriodBounds(LLMPeriod):
    """A relative period whose bounds the server writes into two declared parameters."""

    start: str
    end: str


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
    periods: list[LLMPeriodBounds]
    tool_suggestion: LLMToolSuggestion | None
