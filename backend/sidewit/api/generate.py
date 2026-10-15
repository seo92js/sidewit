import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from sidewit.api.state import HistoryDep, StoreDep
from sidewit.application.generate import (
    GenerateAmbiguous,
    GenerateOk,
    GenerateRequest,
    GenerateUnanswerable,
    Source,
    ToolSuggestion,
    source_of,
)
from sidewit.application.generate import (
    generate_operation as run_generate,
)
from sidewit.application.runtime import get_llm_provider
from sidewit.domain.metric import MetricBinding
from sidewit.domain.operation import SqlOperation
from sidewit.errors import SidewitError
from sidewit.generation.clarification import ClarificationAnswer, ClarificationQuestion
from sidewit.generation.history import GenerationRecord
from sidewit.generation.providers.base import LLMProvider
from sidewit.generation.tool_plan import PlannedOperation
from sidewit.validation.sql_validator import ValidationResult

router = APIRouter(tags=["generate"])
ProviderDep = Annotated[LLMProvider | None, Depends(get_llm_provider)]


@router.post("/generate")
async def generate_operation(
    body: GenerateRequest, store: StoreDep, history: HistoryDep, provider: ProviderDep
) -> GenerateOk | GenerateAmbiguous | GenerateUnanswerable:
    return await run_generate(body, store, history, provider)


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
    # Untrusted model selection, for debugging only; never an invocable operation.
    tool_selection: dict[str, Any] | None = None
    reference_time: datetime
    source: Source | Literal["plan"] | None = None  # set for ok generations
    operation: SqlOperation | None
    operations: list[PlannedOperation] | None = None
    reason: str | None
    clarification_questions: list[ClarificationQuestion]
    clarification_answers: list[ClarificationAnswer]
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
        raise SidewitError(
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
