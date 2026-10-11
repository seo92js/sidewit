"""Generate: question + Context → validated Operation, or ambiguous/unanswerable
(SPEC §5.3, §6). Runs the planner/repair loop and records the Generation; the planners own
the prompts and provider DTOs."""

import time
from dataclasses import dataclass
from datetime import UTC
from typing import Literal

from sidewit.domain.context import ContextDefinition
from sidewit.domain.issue import Issue
from sidewit.generation.history import GenerationHistory, GenerationRecord
from sidewit.generation.planners import GenerationInput, StatusOutput, planner_for
from sidewit.generation.providers.base import LLMError, LLMProvider, Message
from sidewit.security import redact
from sidewit.store import ContextRecord, Store

MAX_ATTEMPTS = 2  # first generation + one repair

Outcome = Literal["ok", "ambiguous", "unanswerable", "invalid", "llm_error"]


class ContextDeleted(Exception):
    """The Context was deleted while the model was answering; nothing was recorded."""


@dataclass
class GenerationResult:
    outcome: Outcome
    record: GenerationRecord
    errors: list[Issue]


async def generate(
    store: Store,
    history: GenerationHistory,
    provider: LLMProvider,
    context: ContextRecord,
    definition: ContextDefinition,
    request: GenerationInput,
) -> GenerationResult:
    """`definition` is the parsed `context.definition`. Raises InvalidGenerateRequest before
    calling the provider when the request options cannot apply to the Context."""
    planner = planner_for(
        definition,
        context.allow_generated_sql,
        context.dialect,
        context.timezone,
        request,
    )
    system = planner.system_prompt()
    messages = [Message("user", request.question)]
    record = GenerationRecord(
        context_id=context.id,
        context_version=context.version,
        question=request.question,
        reference_time=request.reference_time.astimezone(UTC),
        provider=provider.name,
        model=provider.model,
        prompt_version=planner.prompt_version,
    )
    started = time.monotonic()
    input_tokens: list[int] = []
    output_tokens: list[int] = []

    outcome: Outcome = "llm_error"
    errors: list[Issue] = []
    while record.attempts < MAX_ATTEMPTS:
        record.attempts += 1
        try:
            result = await provider.generate_structured(system, messages, planner.output_schema)
        except LLMError as e:
            record.status = "failed"
            # Providers redact their own key; apply patterns before retaining the error.
            record.reason = redact(f"LLM call failed: {e}")
            outcome = "llm_error"
            break
        if result.input_tokens is not None:
            input_tokens.append(result.input_tokens)
        if result.output_tokens is not None:
            output_tokens.append(result.output_tokens)
        output: StatusOutput = result.parsed

        if output.status != "ok":
            # Never turn ambiguous/unanswerable into SQL; any statement sent along is dropped.
            _conclude(record, output.status, output.reason or "No reason provided.")
            record.clarifications = output.clarifications if output.status == "ambiguous" else None
            outcome = output.status
            break

        plan = planner.plan(result.parsed)
        if plan.kind == "ambiguous":
            _conclude(record, "ambiguous", plan.reason or "Clarification needed.")
            record.clarifications = plan.clarifications
            outcome = "ambiguous"
            break

        record.operation = plan.operation.model_dump(mode="json") if plan.operation else None
        record.validation = plan.validation.model_dump(mode="json") if plan.validation else None
        record.metric_binding = plan.binding.model_dump() if plan.binding else None
        if plan.kind == "ok":
            record.status = "ok"
            record.reason = None
            record.tool_suggestion = (
                output.tool_suggestion.model_dump() if output.tool_suggestion else None
            )
            outcome = "ok"
            break

        record.status = "failed"
        record.reason = "Generation failed validation."
        outcome = "invalid"
        errors = plan.validation.errors if plan.validation else []
        messages += [
            Message("assistant", output.model_dump_json()),
            Message("user", planner.repair_message(errors)),
        ]

    record.latency_ms = round((time.monotonic() - started) * 1000)
    record.input_tokens = sum(input_tokens) if input_tokens else None
    record.output_tokens = sum(output_tokens) if output_tokens else None
    # The Context is not held while the model answers, so it may have been deleted since.
    if store.context(context.id) is None:
        raise ContextDeleted(context.id)
    history.add(record)
    return GenerationResult(outcome=outcome, record=record, errors=errors)


def _conclude(record: GenerationRecord, status: str, reason: str) -> None:
    """A non-SQL answer: no operation, validation, suggestion or binding is kept."""
    record.status = status
    record.reason = reason
    record.operation = record.validation = record.tool_suggestion = None
    record.metric_binding = None
