"""Generate: question + Context → validated Operation, or ambiguous/unanswerable
(SPEC §5.3, §6). Runs the planner/repair loop and records the Generation; the planners own
the prompts and provider DTOs."""

import time
from dataclasses import dataclass
from datetime import UTC
from typing import Literal

from sidewit.domain.context import ContextDefinition
from sidewit.domain.issue import Issue
from sidewit.generation.clarification import (
    LLMClarification,
    user_message,
)
from sidewit.generation.history import GenerationHistory, GenerationRecord
from sidewit.generation.planners import GenerationInput, StatusOutput, planner_for
from sidewit.generation.providers.base import LLMError, LLMProvider, Message
from sidewit.generation.tool_selection import (
    PROMPT_VERSION as TOOL_PROMPT_VERSION,
)
from sidewit.generation.tool_selection import (
    ToolSelection,
    ToolSelectionError,
    invoke_selection,
    selection_prompt,
)
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
    messages = [Message("user", user_message(request.question, request.clarification_answers))]
    record = GenerationRecord(
        context_id=context.id,
        context_version=context.version,
        question=request.question,
        clarification_answers=[a.model_dump() for a in request.clarification_answers],
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
    selection_error: ToolSelectionError | None = None
    handled = False
    candidates = store.tools_of(context.id) if not request.options.any() else []
    if candidates:
        tool_prompt = selection_prompt(candidates, context, request)
        record.prompt_version = TOOL_PROMPT_VERSION
        record.attempts += 1
        try:
            selection_result = await provider.generate_structured(
                tool_prompt, messages, ToolSelection
            )
            if selection_result.input_tokens is not None:
                input_tokens.append(selection_result.input_tokens)
            if selection_result.output_tokens is not None:
                output_tokens.append(selection_result.output_tokens)
            record.tool_selection = selection_result.parsed.model_dump(mode="json")
            current_context = store.context(context.id)
            if current_context is None:
                raise ContextDeleted(context.id)
            if current_context.version != record.context_version:
                raise ToolSelectionError(
                    409, "context_changed", "Context changed. Submit the question again."
                )
            selection = selection_result.parsed
            if selection.route != "tool" and (
                selection.tool_name is not None or selection.parameters
            ):
                raise ToolSelectionError(
                    422, "invalid_tool_selection", "Unexpected Tool arguments for this route."
                )
            if selection.route == "ambiguous":
                if not selection.reason or not selection.clarifications:
                    raise ToolSelectionError(
                        422,
                        "invalid_tool_selection",
                        "Tool selection omitted clarification questions.",
                    )
                _conclude(record, "ambiguous", selection.reason)
                _clarify(record, selection.clarifications)
                outcome, handled = "ambiguous", True
            elif selection.route == "tool":
                operation, validation, selected_tool = invoke_selection(
                    selection, candidates, store, current_context
                )
                record.operation = operation.model_dump(mode="json")
                record.validation = validation.model_dump(mode="json")
                record.selected_tool = {
                    "name": selected_tool.name,
                    "version": selected_tool.version,
                }
                record.metric_binding = selected_tool.metric_binding
                record.status = "ok"
                outcome, handled = "ok", True
        except ToolSelectionError as exc:
            # Persist diagnostics and usage through the same finalization as other outcomes.
            # Never resume generation after a failed selection or invocation.
            record.reason = f"{exc.code}: {exc}"
            selection_error = exc
            handled = True
        except LLMError as exc:
            record.reason = redact(f"LLM call failed: {exc}")
            outcome, handled = "llm_error", True
    if candidates and not handled:
        record.prompt_version += "+" + planner.prompt_version
    for _ in range(0 if handled else MAX_ATTEMPTS):
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

        if output.status == "ambiguous" and not output.clarifications:
            record.reason = "LLM omitted clarification questions."
            outcome = "llm_error"
            break
        if output.status != "ok":
            # Never turn ambiguous/unanswerable into SQL; any statement sent along is dropped.
            _conclude(record, output.status, output.reason or "No reason provided.")
            if output.status == "ambiguous":
                _clarify(record, output.clarifications)
            outcome = output.status
            break

        plan = planner.plan(result.parsed)
        if plan.kind == "ambiguous":
            _conclude(record, "ambiguous", plan.reason or "Clarification needed.")
            _clarify(record, plan.clarifications)
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
    current_context = store.context(context.id)
    if current_context is None:
        raise ContextDeleted(context.id)
    if (
        request.expected_context_version is not None
        and current_context.version != request.expected_context_version
    ):
        _conclude(record, "failed", "Context changed. Start a new request.")
        record.selected_tool = None
        selection_error = ToolSelectionError(409, "context_changed", record.reason)
    history.add(record)
    if selection_error is not None:
        selection_error.details = {**selection_error.details, "generation_id": str(record.id)}
        raise selection_error
    return GenerationResult(outcome=outcome, record=record, errors=errors)


def _conclude(record: GenerationRecord, status: str, reason: str) -> None:
    """A non-SQL answer: no operation, validation, suggestion or binding is kept."""
    record.status = status
    record.reason = reason
    record.operation = record.validation = record.tool_suggestion = None
    record.metric_binding = None


def _clarify(record: GenerationRecord, questions: list[LLMClarification]) -> None:
    record.clarifications = [q.question for q in questions]
    record.clarification_questions = [
        {
            "id": f"{record.id}:{index}",
            "question": q.question,
            "options": list(dict.fromkeys(q.options)),
        }
        for index, q in enumerate(questions)
    ]
