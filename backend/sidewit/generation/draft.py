"""One SQL/metric draft with one repair, without persistence or Tool selection."""

from dataclasses import dataclass, field
from typing import Any, Literal

from sidewit.domain.issue import Issue
from sidewit.generation.clarification import LLMClarification
from sidewit.generation.dto import LLMToolSuggestion
from sidewit.generation.planners import Plan, Planner, StatusOutput
from sidewit.generation.providers.base import LLMError, LLMProvider, Message
from sidewit.security import redact

MAX_ATTEMPTS = 2
Outcome = Literal["ok", "ambiguous", "unanswerable", "invalid", "llm_error"]


@dataclass
class DraftResult:
    outcome: Outcome = "llm_error"
    plan: Plan | None = None
    reason: str | None = None
    questions: list[LLMClarification] = field(default_factory=list)
    suggestion: LLMToolSuggestion | None = None
    errors: list[Issue] = field(default_factory=list)
    attempts: int = 0
    input_tokens: list[int] = field(default_factory=list)
    output_tokens: list[int] = field(default_factory=list)


async def generate_draft(
    provider: LLMProvider, planner: Planner[Any], messages: list[Message]
) -> DraftResult:
    result = DraftResult()
    messages = list(messages)
    system = planner.system_prompt()
    for _ in range(MAX_ATTEMPTS):
        result.attempts += 1
        try:
            response = await provider.generate_structured(system, messages, planner.output_schema)
        except LLMError as exc:
            result.outcome = "llm_error"
            result.reason = redact(f"LLM call failed: {exc}")
            return result
        if response.input_tokens is not None:
            result.input_tokens.append(response.input_tokens)
        if response.output_tokens is not None:
            result.output_tokens.append(response.output_tokens)
        output: StatusOutput = response.parsed
        if output.status == "ambiguous" and not output.clarifications:
            result.outcome = "llm_error"
            result.reason = "LLM omitted clarification questions."
            return result
        if output.status != "ok":
            result.outcome = output.status
            result.reason = output.reason or "No reason provided."
            result.questions = output.clarifications if output.status == "ambiguous" else []
            result.plan = None
            result.errors = []
            return result
        result.plan = planner.plan(response.parsed)
        if result.plan.kind == "ambiguous":
            result.outcome = "ambiguous"
            result.reason = result.plan.reason or "Clarification needed."
            result.questions = result.plan.clarifications
            result.errors = []
            return result
        if result.plan.kind == "ok":
            result.outcome = "ok"
            result.reason = None
            result.errors = []
            result.suggestion = output.tool_suggestion
            return result
        result.outcome = "invalid"
        result.reason = "Generation failed validation."
        result.errors = result.plan.validation.errors if result.plan.validation else []
        messages += [
            Message("assistant", output.model_dump_json()),
            Message("user", planner.repair_message(result.errors)),
        ]
    return result
