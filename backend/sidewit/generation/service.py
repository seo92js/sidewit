"""Generate: question + Context → validated Operation, or ambiguous/unanswerable
(SPEC §5.3, §6). Runs the planner/repair loop and records the Generation; the planners own
the prompts and provider DTOs."""

import time
from dataclasses import dataclass
from datetime import UTC

from sidewit.domain.context import ContextDefinition
from sidewit.domain.issue import Issue
from sidewit.generation.clarification import (
    MAX_CLARIFICATION_ANSWERS,
    MAX_CONTINUATION_CHARACTERS,
    LLMClarification,
    continuation_characters,
    user_message,
)
from sidewit.generation.draft import DraftResult, Outcome, generate_draft
from sidewit.generation.history import GenerationHistory, GenerationRecord
from sidewit.generation.mixed_plan import generate_remaining
from sidewit.generation.planners import GenerationInput, planner_for
from sidewit.generation.providers.base import LLMError, LLMProvider, Message
from sidewit.generation.tool_plan import (
    MAX_PLAN_OPERATIONS,
    PlannedDraftOperation,
    ToolPlanSelection,
    invoke_plan,
    plan_prompt,
)
from sidewit.generation.tool_plan import (
    PROMPT_VERSION as PLAN_PROMPT_VERSION,
)
from sidewit.generation.tool_selection import ToolSelectionError
from sidewit.observability import annotate
from sidewit.security import redact
from sidewit.store import ContextRecord, Store


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
    annotate(generation_id=str(record.id))
    started = time.monotonic()
    input_tokens: list[int] = []
    output_tokens: list[int] = []

    outcome: Outcome = "llm_error"
    errors: list[Issue] = []
    selection_error: ToolSelectionError | None = None
    handled = False
    candidates = (
        [tool for tool in store.tools_of(context.id) if tool.enabled]
        if not request.options.any()
        else []
    )
    if candidates:
        tool_prompt = plan_prompt(candidates, context, request)
        record.prompt_version = PLAN_PROMPT_VERSION
        record.attempts += 1
        try:
            selection_result = await provider.generate_structured(
                tool_prompt, messages, ToolPlanSelection
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
                    409, "context_changed", "Context changed. Start a new request."
                )
            selection = selection_result.parsed
            if selection.route == "plan":
                if len(selection.calls) + len(selection.generated_tasks) > MAX_PLAN_OPERATIONS:
                    raise ToolSelectionError(422, "invalid_tool_plan", "Inconsistent plan steps.")
                operations = invoke_plan(selection, candidates, store, current_context)
                if selection.generated_tasks:
                    drafts = await generate_remaining(
                        selection.generated_tasks, provider, context, definition, request
                    )
                    record.prompt_version += "+" + planner.prompt_version
                    for draft in drafts:
                        record.attempts += draft.attempts
                        input_tokens.extend(draft.input_tokens)
                        output_tokens.extend(draft.output_tokens)
                    # Recheck ALL selected Tools after generation; never mix snapshots.
                    current_context = store.context(context.id)
                    if current_context is None:
                        raise ContextDeleted(context.id)
                    if current_context.version != record.context_version:
                        raise ToolSelectionError(
                            409, "context_changed", "Context changed. Start a new request."
                        )
                    operations = invoke_plan(selection, candidates, store, current_context)
                    failures = [(i, d) for i, d in enumerate(drafts) if d.outcome != "ok"]
                    if failures:
                        priority = {"llm_error": 0, "invalid": 1, "unanswerable": 2, "ambiguous": 3}
                        index, failed = min(failures, key=lambda item: priority[item[1].outcome])
                        _apply_draft(record, failed)
                        outcome, errors = failed.outcome, failed.errors
                        # For ambiguity, keep only the first unresolved task's questions.
                        # Follow-up answers replan the whole request; do not merge questions
                        # whose identical wording may refer to different tasks.
                        if outcome == "invalid":
                            raise ToolSelectionError(
                                422,
                                "generation_invalid",
                                "A generated step failed validation.",
                                {
                                    "step_id": f"step_{len(selection.calls) + index + 1}",
                                    "validation_errors": [e.model_dump() for e in errors],
                                },
                            )
                    else:
                        for index, (task, draft) in enumerate(
                            zip(selection.generated_tasks, drafts, strict=True),
                            start=len(operations) + 1,
                        ):
                            plan = draft.plan
                            assert (
                                plan is not None
                                and plan.operation is not None
                                and plan.validation is not None
                            )
                            operations.append(
                                PlannedDraftOperation(
                                    id=f"step_{index}",
                                    task=task,
                                    source="metric" if plan.binding else "generated_sql",
                                    operation=plan.operation,
                                    validation=plan.validation,
                                    metric_binding=plan.binding,
                                    tool_suggestion=draft.suggestion,
                                )
                            )
                        record.operations = [step.model_dump(mode="json") for step in operations]
                        record.status, outcome = "ok", "ok"
                else:
                    record.operations = [step.model_dump(mode="json") for step in operations]
                    record.status, outcome = "ok", "ok"
                handled = True
            elif selection.route == "generate":
                if (
                    selection.calls
                    or selection.generated_tasks
                    or selection.clarifications
                    or selection.reason is not None
                ):
                    raise ToolSelectionError(
                        422, "invalid_tool_plan", "Inconsistent generation fallback response."
                    )
            else:
                if (
                    selection.calls
                    or selection.generated_tasks
                    or not selection.reason
                    or not selection.reason.strip()
                    or (selection.route == "ambiguous") != bool(selection.clarifications)
                ):
                    raise ToolSelectionError(
                        422, "invalid_tool_plan", "Inconsistent Tool plan response."
                    )
                _conclude(record, selection.route, selection.reason)
                if selection.route == "ambiguous":
                    _clarify(record, selection.clarifications)
                outcome, handled = selection.route, True
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
    if not handled:
        draft = await generate_draft(provider, planner, messages)
        record.attempts += draft.attempts
        input_tokens.extend(draft.input_tokens)
        output_tokens.extend(draft.output_tokens)
        outcome, errors = draft.outcome, draft.errors
        _apply_draft(record, draft)

    if outcome == "ambiguous" and selection_error is None:
        questions = record.clarification_questions
        # Reserve at least one character per answer. Actual free-text length remains
        # checked by the UI and request schema; it cannot be known in advance.
        minimum_characters = continuation_characters(
            request.question, request.clarification_answers
        )
        minimum_characters += sum(len(q["question"]) + 1 for q in questions)
        if (
            len(request.clarification_answers) + len(questions) > MAX_CLARIFICATION_ANSWERS
            or minimum_characters > MAX_CONTINUATION_CHARACTERS
        ):
            outcome = "unanswerable"
            _conclude(
                record,
                outcome,
                "This request needs more clarification than the continuation limits allow. "
                "Split it into smaller requests and include the details already provided.",
            )

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
        selection_error = ToolSelectionError(409, "context_changed", record.reason)
    history.add(record)
    if selection_error is not None:
        selection_error.details = {**selection_error.details, "generation_id": str(record.id)}
        raise selection_error
    return GenerationResult(outcome=outcome, record=record, errors=errors)


def _conclude(record: GenerationRecord, status: str, reason: str | None) -> None:
    """A non-SQL answer: no operation, validation, suggestion or binding is kept."""
    record.status = status
    record.reason = reason
    record.operation = record.validation = record.tool_suggestion = None
    record.metric_binding = None
    record.operations = None
    record.clarification_questions = []


def _clarify(record: GenerationRecord, questions: list[LLMClarification]) -> None:
    record.clarification_questions = [
        {
            "id": f"{record.id}:{index}",
            "question": q.question,
            "options": list(dict.fromkeys(q.options)),
        }
        for index, q in enumerate(questions)
    ]


def _apply_draft(record: GenerationRecord, draft: DraftResult) -> None:
    _conclude(record, "ok" if draft.outcome == "ok" else "failed", draft.reason)
    if draft.outcome in {"ambiguous", "unanswerable"}:
        record.status = draft.outcome
        if draft.questions:
            _clarify(record, draft.questions)
        return
    if draft.plan is not None:
        record.operation = (
            draft.plan.operation.model_dump(mode="json") if draft.plan.operation else None
        )
        record.validation = (
            draft.plan.validation.model_dump(mode="json") if draft.plan.validation else None
        )
        record.metric_binding = draft.plan.binding.model_dump() if draft.plan.binding else None
    record.tool_suggestion = draft.suggestion.model_dump() if draft.suggestion else None
