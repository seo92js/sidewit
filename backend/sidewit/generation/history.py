"""Recent Generations, kept in process memory only (SPEC §3.4).

Generations hold questions, generated SQL and parameter values, so they are never written to
disk or to the log. Request observations own operational logging. A restart forgets them.
"""

import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class GenerationRecord:
    context_id: uuid.UUID
    context_version: int
    question: str
    reference_time: datetime
    provider: str
    model: str
    prompt_version: str
    attempts: int = 0
    status: str = "failed"
    operation: dict[str, Any] | None = None
    operations: list[dict[str, Any]] | None = None
    reason: str | None = None
    clarification_questions: list[dict[str, Any]] = field(default_factory=list)
    clarification_answers: list[dict[str, str]] = field(default_factory=list)
    tool_suggestion: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    tool_selection: dict[str, Any] | None = None
    metric_binding: dict[str, Any] | None = None
    latency_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class GenerationHistory:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._records: OrderedDict[uuid.UUID, GenerationRecord] = OrderedDict()

    def add(self, record: GenerationRecord) -> None:
        self._records[record.id] = record
        while len(self._records) > self.limit:
            self._records.popitem(last=False)

    def get(self, generation_id: uuid.UUID) -> GenerationRecord | None:
        return self._records.get(generation_id)

    def recent(self, context_id: uuid.UUID | None, limit: int) -> list[GenerationRecord]:
        records = [
            r
            for r in reversed(self._records.values())
            if context_id is None or r.context_id == context_id
        ]
        return records[:limit]

    def forget_context(self, context_id: uuid.UUID) -> None:
        for generation_id in [i for i, r in self._records.items() if r.context_id == context_id]:
            del self._records[generation_id]
