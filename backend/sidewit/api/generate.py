import asyncio
import uuid
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel, Field, field_validator

from sidewit.api.errors import ApiError
from sidewit.api.state import HistoryDep, StoreDep
from sidewit.config import get_settings
from sidewit.domain.context import ContextDefinition, check_context_size
from sidewit.domain.metric import MetricBinding, Period
from sidewit.domain.operation import SqlOperation
from sidewit.generation.history import GenerationRecord
from sidewit.generation.planners import GenerationInput, InvalidGenerateRequest, MetricOptions
from sidewit.generation.providers import build_provider
from sidewit.generation.providers.base import LLMProvider
from sidewit.generation.service import ContextDeleted, generate
from sidewit.validation.sql_validator import ValidationResult

router = APIRouter(tags=["generate"])


def get_llm_provider() -> LLMProvider | None:
    return build_provider(get_settings())


ProviderDep = Annotated[LLMProvider | None, Depends(get_llm_provider)]


GENERATION_TIMEOUT_SECONDS = 90


class GenerateRequest(BaseModel):
    context_id: uuid.UUID
    question: str = Field(min_length=1, max_length=8000)
    reference_time: AwareDatetime | None = None
    metric_id: str | None = None
    date_field: str | None = None
    period: Period | None = None
    # Answer only with a registered metric (never model-written SQL) for this request.
    require_metric: bool = False

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


def source_of(record: GenerationRecord) -> Source:
    return "metric" if record.metric_binding else "generated_sql"


class GenerateOk(BaseModel):
    generation_id: uuid.UUID
    context_version: int
    status: Literal["ok"] = "ok"
    source: Source
    operation: SqlOperation
    validation: ValidationResult
    tool_suggestion: ToolSuggestion | None
    metric_binding: MetricBinding | None = None


class GenerateAmbiguous(BaseModel):
    generation_id: uuid.UUID
    context_version: int
    status: Literal["ambiguous"] = "ambiguous"
    reason: str
    clarifications: list[str]


class GenerateUnanswerable(BaseModel):
    generation_id: uuid.UUID
    context_version: int
    status: Literal["unanswerable"] = "unanswerable"
    reason: str


@router.post("/generate")
async def generate_operation(
    body: GenerateRequest, store: StoreDep, history: HistoryDep, provider: ProviderDep
) -> GenerateOk | GenerateAmbiguous | GenerateUnanswerable:
    try:
        async with asyncio.timeout(GENERATION_TIMEOUT_SECONDS):
            return await _generate_operation(body, store, history, provider)
    except TimeoutError as e:
        raise ApiError(
            504, "generation_timeout", "Generation exceeded the 90-second deadline."
        ) from e


async def _generate_operation(
    body: GenerateRequest, store: StoreDep, history: HistoryDep, provider: ProviderDep
):
    context = store.context(body.context_id)
    if context is None:
        raise ApiError(404, "context_not_found", f"Context '{body.context_id}' does not exist.")
    definition = ContextDefinition.model_validate(context.definition)
    try:
        check_context_size(definition)
    except ValueError as e:
        raise ApiError(422, "context_too_large", str(e)) from e
    if provider is None:
        raise ApiError(
            503,
            "llm_not_configured",
            "Set SIDEWIT_LLM_PROVIDER, SIDEWIT_LLM_MODEL and the provider API key.",
        )

    request = GenerationInput(
        question=body.question,
        reference_time=body.reference_time or datetime.now(ZoneInfo(context.timezone)),
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
    except InvalidGenerateRequest as e:
        raise ApiError(422, "invalid_metric_request", str(e)) from e
    except ContextDeleted as e:
        raise ApiError(
            404,
            "context_not_found",
            f"Context '{body.context_id}' was deleted while the answer was generated.",
        ) from e
    record = result.record
    common = {"generation_id": record.id, "context_version": record.context_version}

    match result.outcome:
        case "ok":
            return GenerateOk(
                **common,
                source=source_of(record),
                operation=SqlOperation.model_validate(record.operation),
                validation=ValidationResult.model_validate(record.validation),
                tool_suggestion=record.tool_suggestion,
                metric_binding=record.metric_binding,
            )
        case "ambiguous":
            return GenerateAmbiguous(
                **common, reason=record.reason, clarifications=record.clarifications or []
            )
        case "unanswerable":
            return GenerateUnanswerable(**common, reason=record.reason)
        case "invalid":
            # The rejected SQL stays in in-memory history only (SPEC §5.3).
            raise ApiError(
                422,
                "generation_invalid",
                "Generation failed validation.",
                {
                    "generation_id": str(record.id),
                    "validation_errors": [e.model_dump() for e in result.errors],
                },
            )
        case "llm_error":
            # Provider error details stay in masked in-memory history.
            raise ApiError(502, "llm_error", "LLM call failed.", {"generation_id": str(record.id)})


# Recent Generations (memory only): admin UI / development, not a public interface (§5.3).


class GenerationSummary(BaseModel):
    id: uuid.UUID
    context_id: uuid.UUID
    context_version: int
    question: str
    status: str
    provider: str
    model: str
    prompt_version: str
    attempts: int
    latency_ms: int
    created_at: datetime


class GenerationDetail(GenerationSummary):
    reference_time: datetime
    source: Source | None = None  # set for ok generations
    operation: SqlOperation | None
    reason: str | None
    clarifications: list[str] | None
    tool_suggestion: ToolSuggestion | None
    validation: ValidationResult | None
    input_tokens: int | None
    output_tokens: int | None
    metric_binding: MetricBinding | None = None


class GenerationList(BaseModel):
    items: list[GenerationSummary]


def _get_generation(history: HistoryDep, generation_id: uuid.UUID) -> GenerationRecord:
    record = history.get(generation_id)
    if record is None:
        raise ApiError(
            404,
            "generation_not_found",
            f"Generation '{generation_id}' is not in this server's recent history.",
        )
    return record


@router.get("/generations")
async def list_generations(
    history: HistoryDep,
    context_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> GenerationList:
    records = history.recent(context_id, limit)
    return GenerationList(
        items=[GenerationSummary.model_validate(r, from_attributes=True) for r in records]
    )


@router.get("/generations/{generation_id}")
async def get_generation(generation_id: uuid.UUID, history: HistoryDep) -> GenerationDetail:
    record = _get_generation(history, generation_id)
    detail = GenerationDetail.model_validate(record, from_attributes=True)
    detail.source = source_of(record) if record.status == "ok" else None
    return detail
