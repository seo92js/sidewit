"""Expose successful generation results as independently reviewable operation steps."""

from typing import Any

from sidewit.generation.history import GenerationRecord


def operation_steps(record: GenerationRecord) -> list[dict[str, Any]]:
    if record.status != "ok":
        raise ValueError("Only successful generations have operation steps.")
    if record.operations is not None:
        return record.operations
    return [
        {
            "id": "step_1",
            "source": "metric" if record.metric_binding else "generated_sql",
            "task": record.question,
            "operation": record.operation,
            "validation": record.validation,
            "tool_suggestion": record.tool_suggestion,
            "metric_binding": record.metric_binding,
        }
    ]
