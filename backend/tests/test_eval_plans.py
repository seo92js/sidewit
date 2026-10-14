import copy

import httpx
import pytest
from pydantic import ValidationError

from sidewit.eval.cases import Expectations, GoldenCase
from sidewit.eval.runner import _run_case


def step(index, value="서울", statement=None):
    return {
        "id": f"step_{index}",
        "source": "tool",
        "selected_tool": {
            "id": "00000000-0000-0000-0000-000000000002",
            "name": "count_customers",
            "version": 1,
        },
        "operation": {
            "type": "sql",
            "dialect": "postgresql",
            "statement": statement or "SELECT COUNT(*) FROM customers WHERE region = :region",
            "parameters": {"region": value},
            "parameter_types": {"region": "string"},
        },
        "validation": {"valid": True, "errors": [], "warnings": [], "checks": ["syntax"]},
    }


def response(steps):
    return {
        "generation_id": "00000000-0000-0000-0000-000000000001",
        "context_version": 1,
        "status": "ok",
        "source": "plan",
        "execution": "independent",
        "operations": steps,
    }


def rule(region):
    return {
        "resources": ["customers"],
        "constraints": [{"field": "customers.region", "op": "=", "parameter_value": region}],
    }


async def evaluate(suite, body, **expect):
    case = GoldenCase(
        id="plan_review",
        question="Count customers",
        category="plan",
        expect={"status": "ok", "source": "plan", **expect},
    )
    async with httpx.AsyncClient(
        base_url="http://fixture",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    ) as client:
        return await _run_case(client, suite, "context", case)


async def test_one_step_uses_existing_sql_expectations(golden_suite):
    result = await evaluate(golden_suite, response([step(1)]), **rule("서울"))
    assert result.passed, result.failures


async def test_two_steps_match_independently_in_any_order(golden_suite):
    result = await evaluate(
        golden_suite, response([step(1, "부산"), step(2)]), steps=[rule("서울"), rule("부산")]
    )
    assert result.passed, result.failures


async def test_one_good_step_cannot_cover_two_expectations(golden_suite):
    result = await evaluate(
        golden_suite, response([step(1), step(2)]), steps=[rule("서울"), rule("부산")]
    )
    assert not result.passed
    assert any(f.expectation == "steps[1].constraints" for f in result.failures)


async def test_invalid_second_sql_is_not_hidden_by_api_valid_flag(golden_suite):
    result = await evaluate(
        golden_suite,
        response([step(1), step(2, "부산", "SELECT * FROM absent WHERE x = :region")]),
        steps=[rule("서울"), rule("부산")],
    )
    assert not result.passed
    assert any(f.expectation.endswith("validity") and "step_2" in f.detail for f in result.failures)


async def test_reported_errors_keep_step_identity(golden_suite):
    item = step(1)
    item["validation"] = {
        "valid": False,
        "errors": [{"code": "bad", "message": "Invalid", "ref": None}],
        "warnings": [],
        "checks": [],
    }
    result = await evaluate(golden_suite, response([item]))
    assert not result.passed
    assert result.validation_errors[0]["step_id"] == "step_1"


@pytest.mark.parametrize("steps", [[], [step(i) for i in range(1, 7)], [{"id": "step_1"}]])
async def test_malformed_plan_fails_without_crashing(golden_suite, steps):
    result = await evaluate(golden_suite, response(steps))
    assert not result.passed
    assert result.failures[0].expectation == "generation"


async def test_duplicate_ids_fail(golden_suite):
    result = await evaluate(
        golden_suite, response([step(1), step(1, "부산")]), steps=[rule("서울"), rule("부산")]
    )
    assert not result.passed
    assert any(f.expectation == "steps" for f in result.failures)


async def test_multiple_operations_require_explicit_step_expectations(golden_suite):
    result = await evaluate(golden_suite, response([step(1), step(2, "부산")]))
    assert not result.passed
    assert "expect.steps" in result.failures[0].detail


async def test_missing_step_is_failure(golden_suite):
    result = await evaluate(golden_suite, response([step(1)]), steps=[rule("서울"), rule("부산")])
    assert not result.passed
    assert "expected 2" in result.failures[0].detail


@pytest.mark.parametrize(
    "extra", [{"source": "metric"}, {"status": "ambiguous"}, {"resources": []}]
)
def test_step_expectations_cannot_silently_ignore_top_level_rules(extra):
    with pytest.raises(ValidationError):
        Expectations.model_validate(
            {"status": "ok", "source": "plan", "steps": [rule("서울")], **extra}
        )


async def test_tool_clock_policy_preserved(golden_suite):
    item = copy.deepcopy(step(1))
    item["operation"].update(statement="SELECT NOW()", parameters={}, parameter_types={})
    result = await evaluate(golden_suite, response([item]))
    assert result.passed, result.failures


async def test_generated_plan_step_uses_generation_policy(golden_suite):
    item = step(1)
    item.pop("selected_tool")
    item.update(source="generated_sql", task="Current time")
    item["operation"].update(statement="SELECT NOW()", parameters={}, parameter_types={})
    result = await evaluate(golden_suite, response([item]))
    assert not result.passed
    assert any("relative_time_function" in failure.detail for failure in result.failures)


async def test_legacy_single_response_is_rejected(golden_suite):
    legacy = response([step(1)])
    single = legacy.pop("operations")[0]
    legacy.update(operation=single["operation"], validation=single["validation"])
    result = await evaluate(golden_suite, legacy, **rule("서울"))
    assert not result.passed
    assert result.failures[0].expectation == "generation"
    assert result.failures[0].detail == "Malformed operation result."


@pytest.mark.parametrize(
    "source,step_source",
    [
        ("generated_sql", "tool"),
        ("metric", "tool"),
        ("generated_sql", "metric"),
        ("metric", "generated_sql"),
    ],
)
async def test_contradictory_sources_cannot_bypass_policy(golden_suite, source, step_source):
    item = step(1)
    item["operation"].update(statement="SELECT NOW()", parameters={}, parameter_types={})
    if step_source != "tool":
        item.pop("selected_tool")
        item.update(source=step_source, task="Current time")
    body = response([item])
    body["source"] = source
    result = await evaluate(golden_suite, body, source=source)
    assert not result.passed
    assert result.failures[0].detail == "Malformed operation result."


async def test_standalone_source_cannot_label_multiple_steps(golden_suite):
    items = [step(1), step(2)]
    for item in items:
        item.pop("selected_tool")
        item.update(source="generated_sql", task="Count customers")
    body = response(items)
    body["source"] = "generated_sql"
    result = await evaluate(golden_suite, body, source="generated_sql")
    assert not result.passed
    assert result.failures[0].detail == "Malformed operation result."
