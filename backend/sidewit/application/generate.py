"""Generate request contract and orchestration, independent of HTTP and MCP."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from sidewit.domain.context import ContextDefinition, check_context_size
from sidewit.domain.issue import Issue
from sidewit.domain.metric import Period
from sidewit.errors import SidewitError
from sidewit.generation.clarification import (
    MAX_CLARIFICATION_ANSWERS,
    MAX_CONTINUATION_CHARACTERS,
    ClarificationAnswer,
    ClarificationQuestion,
    continuation_characters,
)
from sidewit.generation.history import GenerationHistory, GenerationRecord
from sidewit.generation.operations import operation_steps
from sidewit.generation.planners import GenerationInput, InvalidGenerateRequest, MetricOptions
from sidewit.generation.providers.base import LLMProvider
from sidewit.generation.providers.observed import ObservedProvider
from sidewit.generation.service import ContextDeleted, generate
from sidewit.generation.tool_plan import MAX_PLAN_OPERATIONS, PlannedOperation
from sidewit.generation.tool_selection import ToolSelectionError
from sidewit.observability import annotate
from sidewit.store import Store

GENERATION_TIMEOUT_SECONDS = 90


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_id: uuid.UUID
    question: str = Field(min_length=1, max_length=8000)
    reference_time: AwareDatetime | None = None
    expected_context_version: int | None = Field(default=None, ge=1)
    clarification_answers: list[ClarificationAnswer] = Field(
        default_factory=list, max_length=MAX_CLARIFICATION_ANSWERS
    )

    metric_id: str | None = None
    date_field: str | None = None
    period: Period | None = None
    # Answer only with a registered metric (never model-written SQL) for this request.
    require_metric: bool = False

    @model_validator(mode="after")
    def _continuation(self):
        if self.clarification_answers:
            if self.reference_time is None or self.expected_context_version is None:
                raise ValueError(
                    "Clarification answers require reference_time and expected_context_version."
                )
            ids = [a.id for a in self.clarification_answers]
            if len(ids) != len(set(ids)):
                raise ValueError("Clarification answer IDs must be unique.")
            if (
                continuation_characters(self.question, self.clarification_answers)
                > MAX_CONTINUATION_CHARACTERS
            ):
                raise ValueError("Question and clarification answers exceed 16000 characters.")
        return self

    @field_validator("question")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be blank")
        return value


class ToolSuggestion(BaseModel):
    name: str
    description: str


# metric: SQL built by the server from a registered metric contract.
# generated_sql: SQL written by the model and structurally validated; review its meaning.
Source = Literal["metric", "generated_sql"]


def source_of(record: GenerationRecord) -> Source | Literal["plan"]:
    if record.operations is not None:
        return "plan"
    return "metric" if record.metric_binding else "generated_sql"


class GenerateOk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_id: uuid.UUID
    context_version: int
    status: Literal["ok"] = "ok"
    source: Source | Literal["plan"]
    execution: Literal["independent"] = "independent"
    operations: list[PlannedOperation] = Field(min_length=1, max_length=MAX_PLAN_OPERATIONS)

    @model_validator(mode="after")
    def _source_consistency(self):
        if self.source != "plan" and (
            len(self.operations) != 1 or self.operations[0].source != self.source
        ):
            raise ValueError("A standalone result requires one step matching its source.")
        return self


class GenerateAmbiguous(BaseModel):
    generation_id: uuid.UUID
    context_version: int
    status: Literal["ambiguous"] = "ambiguous"
    reason: str
    clarification_questions: list[ClarificationQuestion]
    reference_time: datetime
    continuation_request: GenerateRequest


class GenerateUnanswerable(BaseModel):
    generation_id: uuid.UUID
    context_version: int
    status: Literal["unanswerable"] = "unanswerable"
    reason: str


def _public_validation_errors(errors: list[Issue]) -> list[dict[str, str | None]]:
    """Generation diagnostics may contain entire SQL expressions, literals or parser excerpts.
    Keep codes and plain identifier references; detailed diagnostics belong to history/repair.
    """
    return [
        {
            "code": issue.code,
            "message": issue.code.replace("_", " ").capitalize() + ".",
            "ref": issue.ref
            if issue.ref and all(part.isidentifier() for part in issue.ref.split("."))
            else None,
        }
        for issue in errors
    ]


async def generate_operation(
    body: GenerateRequest, store: Store, history: GenerationHistory, provider: LLMProvider | None
) -> GenerateOk | GenerateAmbiguous | GenerateUnanswerable:
    annotate(action="generate", context_id=str(body.context_id), llm_calls=0)
    if provider is not None:
        annotate(provider=provider.name, model=provider.model, token_usage_complete=True)
        provider = ObservedProvider(provider)
    try:
        async with asyncio.timeout(GENERATION_TIMEOUT_SECONDS):
            return await _generate_operation(body, store, history, provider)
    except TimeoutError as e:
        raise SidewitError(
            504, "generation_timeout", "Generation exceeded the 90-second deadline."
        ) from e


async def _generate_operation(
    body: GenerateRequest, store: Store, history: GenerationHistory, provider: LLMProvider | None
):
    context = store.context(body.context_id)
    if context is None:
        raise SidewitError(404, "context_not_found", f"Context '{body.context_id}' does not exist.")
    annotate(context_version=context.version)
    if (
        body.expected_context_version is not None
        and body.expected_context_version != context.version
    ):
        raise SidewitError(
            409,
            "context_changed",
            "Context changed. Start a new request.",
            {"expected_version": body.expected_context_version, "current_version": context.version},
        )
    definition = ContextDefinition.model_validate(context.definition)
    try:
        check_context_size(definition)
    except ValueError as e:
        raise SidewitError(422, "context_too_large", str(e)) from e
    try:
        # Both the stored UTC instant and the Context-local prompt time must be representable.
        reference_time = (body.reference_time or datetime.now(UTC)).astimezone(UTC)
        reference_time = reference_time.astimezone(ZoneInfo(context.timezone))
    except (OverflowError, ValueError):
        raise SidewitError(
            422,
            "invalid_reference_time",
            "Reference time is outside the supported range in UTC or the Context timezone.",
        ) from None
    if provider is None:
        raise SidewitError(
            503,
            "llm_not_configured",
            "Set SIDEWIT_LLM_PROVIDER, SIDEWIT_LLM_MODEL and the provider API key.",
        )

    request = GenerationInput(
        question=body.question,
        clarification_answers=tuple(body.clarification_answers),
        reference_time=reference_time,
        options=MetricOptions(
            metric_id=body.metric_id,
            date_field=body.date_field,
            period=body.period,
            require_metric=body.require_metric,
        ),
    )
    try:
        result = await generate(
            store,
            history,
            provider,
            context,
            definition,
            request,
        )
    except ToolSelectionError as e:
        details = dict(e.details)
        for key in ("validation_errors", "errors"):
            if key in details:
                details[key] = _public_validation_errors(
                    [Issue.model_validate(issue) for issue in details[key]]
                )
        raise SidewitError(e.status, e.code, str(e), details) from e
    except InvalidGenerateRequest as e:
        raise SidewitError(422, "invalid_metric_request", str(e)) from e
    except ContextDeleted as e:
        raise SidewitError(
            404,
            "context_not_found",
            f"Context '{body.context_id}' was deleted while the answer was generated.",
        ) from e
    record = result.record
    annotate(status=result.outcome)
    common = {"generation_id": record.id, "context_version": record.context_version}

    match result.outcome:
        case "ok":
            steps = operation_steps(record)
            annotate(
                operation_count=len(steps),
                selected_tools=[
                    {
                        "tool_id": step["selected_tool"]["id"],
                        "tool_version": step["selected_tool"]["version"],
                    }
                    for step in steps
                    if step["source"] == "tool"
                ],
            )
            return GenerateOk(**common, source=source_of(record), operations=steps)
        case "ambiguous":
            return GenerateAmbiguous(
                **common,
                reason=record.reason,
                clarification_questions=record.clarification_questions,
                reference_time=record.reference_time,
                continuation_request=body.model_copy(
                    update={
                        "reference_time": record.reference_time,
                        "expected_context_version": record.context_version,
                    },
                    deep=True,
                ),
            )
        case "unanswerable":
            return GenerateUnanswerable(**common, reason=record.reason)
        case "invalid":
            # The rejected SQL stays in in-memory history only (SPEC §5.3).
            raise SidewitError(
                422,
                "generation_invalid",
                "Generation failed validation.",
                {
                    "generation_id": str(record.id),
                    "validation_errors": _public_validation_errors(result.errors),
                },
            )
        case "llm_error":
            # Provider error details stay in masked in-memory history.
            raise SidewitError(
                502, "llm_error", "LLM call failed.", {"generation_id": str(record.id)}
            )
