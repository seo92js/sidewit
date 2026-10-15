"""Score independent Tool steps without combining their SQL or parameter scopes."""

from itertools import permutations
from typing import Any

from pydantic import ValidationError

from sidewit.application.generate import GenerateOk
from sidewit.eval.cases import Expectations, SuiteContext
from sidewit.eval.scoring import Failure, score_operation


def score_plan(
    expect: Expectations, body: dict[str, Any], context: SuiteContext
) -> tuple[list[Failure], list[dict[str, Any]]]:
    try:
        plan = GenerateOk.model_validate(body)
    except ValidationError:
        return [Failure(expectation="generation", detail="Malformed operation result.")], []

    failures = []
    errors = []
    for index, step in enumerate(plan.operations, start=1):
        if step.id != f"step_{index}":
            failures.append(Failure(expectation="steps", detail="Invalid or duplicate step IDs."))
        if not step.validation.valid:
            failures.append(
                Failure(expectation="validity", detail=f"{step.id}: API did not report valid SQL.")
            )
        errors.extend(
            {**issue.model_dump(), "step_id": step.id} for issue in step.validation.errors
        )

    expected = expect.steps
    if expected is None:
        if len(plan.operations) != 1:
            return failures + [
                Failure(
                    expectation="steps", detail="Define expect.steps for a multi-operation plan."
                )
            ], errors
        expected = [expect]
    if len(expected) != len(plan.operations):
        return failures + [
            Failure(
                expectation="steps",
                detail=f"expected {len(expected)} operations, got {len(plan.operations)}",
            )
        ], errors

    # Independent operations have no required ordering. A one-to-one assignment prevents
    # one correct step from satisfying multiple expectations. At most 5! assignments.
    scores = [
        [
            score_operation(rule, step.operation, context.definition, context.timezone, step.source)
            for step in plan.operations
        ]
        for rule in expected
    ]
    assignment = min(
        permutations(range(len(plan.operations))),
        key=lambda order: sum(len(scores[i][j]) for i, j in enumerate(order)),
    )
    for i, j in enumerate(assignment):
        failures.extend(
            Failure(
                expectation=f"steps[{i}].{failure.expectation}",
                detail=f"{plan.operations[j].id}: {failure.detail}",
            )
            for failure in scores[i][j]
        )
    return failures, errors
