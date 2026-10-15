import copy
import json

import httpx
import pytest

from sidewit.application.runtime import get_llm_provider
from sidewit.eval.runner import EvalSetupError, run_suite
from sidewit.generation.dto import LLMGenerationOutput, LLMParameter, LLMToolSuggestion
from sidewit.generation.prompt import PROMPT_VERSION
from sidewit.generation.providers.fake import FakeProvider
from sidewit.main import app
from tests.tool_urls import tool_path


def output(case):
    reference = case.reference
    return LLMGenerationOutput(
        status=case.expect.status,
        reason=None if reference else "Fixture reason",
        clarifications=[{"question": "Which metric?", "options": []}]
        if case.expect.status == "ambiguous"
        else [],
        statement=reference.statement if reference else None,
        parameters=[
            LLMParameter(
                name=name,
                type=reference.parameter_types[name],
                value=str(value).lower() if isinstance(value, bool) else str(value),
            )
            for name, value in reference.parameters.items()
        ]
        if reference
        else [],
        tool_suggestion=LLMToolSuggestion(name="fixture_tool", description="Fixture tool")
        if reference
        else None,
    )


@pytest.fixture
def eval_client(client):
    client.base_url = "http://test/api/v1/"
    return client


@pytest.fixture
def install_provider():
    def install(*responses):
        provider = FakeProvider(list(responses))
        app.dependency_overrides[get_llm_provider] = lambda: provider
        return provider

    return install


def case_by_id(suite, case_id):
    return next(c for c in suite.cases if c.id == case_id)


async def test_entire_golden_suite_through_real_api_with_fixture_provider(
    eval_client,
    golden_suite,
    install_provider,
):
    provider = install_provider(*(output(c) for c in golden_suite.cases))
    report = await run_suite(eval_client, golden_suite)
    assert (report.total, report.passed, report.failed) == (18, 18, 0)
    assert (report.provider, report.model, report.prompt_version) == (
        "fake",
        "fake-model",
        PROMPT_VERSION,
    )
    assert len(provider.calls) == 18
    assert [r.id for r in report.cases] == [c.id for c in golden_suite.cases]
    assert all(
        r.generation_id and r.reference_time == golden_suite.reference_time for r in report.cases
    )
    assert {r.actual_status for r in report.cases} == {"ok", "ambiguous", "unanswerable"}
    data = json.loads(report.model_dump_json())
    assert data["passed"] == 18
    assert data["cases"][0]["reference_time"]
    record = (await eval_client.get(f"/generations/{report.cases[0].generation_id}")).json()
    assert record["reference_time"] == "2026-10-02T00:00:00Z"


async def test_repeat_suite_reuses_identical_context_without_version_bump(
    eval_client,
    golden_suite,
    install_provider,
):
    case = case_by_id(golden_suite, "total_revenue")
    install_provider(output(case), output(case))
    first = await run_suite(eval_client, golden_suite, [case.id])
    second = await run_suite(eval_client, golden_suite, [case.id])
    assert second.context_id == first.context_id
    assert first.context_version == second.context_version == 1
    assert second.passed == 1
    contexts = (await eval_client.get("/contexts")).json()["items"]
    assert len(contexts) == 1


async def test_changed_fixture_updates_context_with_optimistic_version(
    eval_client,
    golden_suite,
    install_provider,
):
    suite = golden_suite.model_copy(deep=True)
    case = case_by_id(suite, "total_revenue")
    install_provider(output(case), output(case))
    first = await run_suite(eval_client, suite, [case.id])
    suite.context.definition.resources[0].description = "Updated fixture"
    second = await run_suite(eval_client, suite, [case.id])
    assert second.context_id == first.context_id
    assert second.context_version == 2


async def test_unknown_case_filter_fails_before_mutating_context(
    eval_client,
    golden_suite,
    install_provider,
):
    provider = install_provider()
    with pytest.raises(EvalSetupError, match="Unknown case ids"):
        await run_suite(eval_client, golden_suite, ["typo"])
    assert (await eval_client.get("/contexts")).json()["items"] == []
    assert provider.calls == []


async def test_case_reference_time_overrides_suite_time(
    eval_client, golden_suite, install_provider
):
    suite = golden_suite.model_copy(deep=True)
    case = case_by_id(suite, "customer_list")
    case.reference_time = suite.reference_time.replace(day=1)
    provider = install_provider(output(case))
    report = await run_suite(eval_client, suite, [case.id])
    assert report.cases[0].reference_time == case.reference_time
    assert "2026-10-01T09:00:00+09:00" in provider.calls[0][0]


async def test_wrong_rule_is_an_expectation_failure_not_validation_failure(
    eval_client,
    golden_suite,
    install_provider,
):
    case = case_by_id(golden_suite, "total_revenue")
    response = output(case)
    response.statement = response.statement.replace("COMPLETED", "PENDING")
    install_provider(response)
    report = await run_suite(eval_client, golden_suite, [case.id])
    result = report.cases[0]
    assert report.failed == 1
    assert result.actual_status == "ok"
    assert [f.expectation for f in result.failures] == ["steps[0].constraints"]
    assert result.validation_errors == []


async def test_status_mismatch_is_reported(eval_client, golden_suite, install_provider):
    response = output(case_by_id(golden_suite, "total_revenue"))
    install_provider(response)
    report = await run_suite(eval_client, golden_suite, ["churn_rate"])
    result = report.cases[0]
    assert not result.passed
    assert (result.expected_status, result.actual_status) == ("unanswerable", "ok")
    assert [f.expectation for f in result.failures] == ["status"]


async def test_generation_invalid_records_errors_id_and_continues_suite(
    eval_client,
    golden_suite,
    install_provider,
):
    bad = output(case_by_id(golden_suite, "customer_list"))
    bad.statement = "SELECT nonexistent FROM customers"
    install_provider(bad, bad, output(case_by_id(golden_suite, "churn_rate")))
    report = await run_suite(eval_client, golden_suite, ["customer_list", "churn_rate"])
    assert (report.total, report.passed, report.failed) == (2, 1, 1)
    rejected = report.cases[0]
    assert rejected.actual_status == "generation_invalid"
    assert rejected.generation_id
    assert rejected.validation_errors[0]["code"] == "unknown_field"
    assert "statement" not in rejected.model_dump()


async def test_provider_error_is_reported(eval_client, golden_suite, install_provider):
    install_provider(RuntimeError("fixture provider unavailable"))
    report = await run_suite(eval_client, golden_suite, ["total_revenue"])
    assert report.failed == 1
    assert report.cases[0].actual_status == "llm_error"
    assert report.cases[0].generation_id


async def test_api_key_is_used_for_suite_requests(
    eval_client,
    golden_suite,
    install_provider,
    api_key,
):
    install_provider(output(case_by_id(golden_suite, "total_revenue")))
    with pytest.raises(EvalSetupError, match="HTTP 401"):
        await run_suite(eval_client, golden_suite, ["total_revenue"])
    eval_client.headers["Authorization"] = f"Bearer {api_key}"
    report = await run_suite(eval_client, golden_suite, ["total_revenue"])
    assert report.passed == 1
    assert api_key not in report.model_dump_json()


async def test_fixture_generate_draft_save_and_deterministic_invoke(
    eval_client,
    golden_suite,
    install_provider,
):
    provider = install_provider(output(case_by_id(golden_suite, "busan_revenue")))
    report = await run_suite(eval_client, golden_suite, ["busan_revenue"])
    draft_response = await eval_client.post(
        "/tools/draft", json={"generation_id": report.cases[0].generation_id}
    )
    assert draft_response.status_code == 200
    draft = draft_response.json()
    arguments = draft.pop("example_arguments")
    saved = await eval_client.post("/tools", json=draft)
    assert saved.status_code == 201, saved.text
    assert saved.json()["status"] == "valid"
    for _ in range(2):
        response = await eval_client.post(
            tool_path("fixture_tool", "/invoke", prefix="/tools"), json={"arguments": arguments}
        )
        assert response.status_code == 200
        assert response.json()["operation"]["parameters"] == arguments
        assert response.json()["operation"]["statement"] == draft["operation"]["statement"]
    assert len(provider.calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "html", "malformed", "invalid_operation"])
async def test_case_request_errors_are_reported_without_aborting_remaining_cases(
    golden_suite, failure
):
    calls = 0

    def handler(request):
        nonlocal calls
        if request.url.path.endswith("/contexts"):
            return httpx.Response(
                200 if request.method == "GET" else 201,
                json={"items": []} if request.method == "GET" else {"id": "context", "version": 1},
            )
        if "/generations/" in request.url.path:
            return httpx.Response(404)
        calls += 1
        if calls == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("unavailable", request=request)
            if failure == "html":
                return httpx.Response(502, text="<html>Bad gateway</html>")
            if failure == "invalid_operation":
                return httpx.Response(
                    200,
                    json={"status": "ok", "generation_id": "bad", "validation": {"valid": True}},
                )
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={"status": "unanswerable", "generation_id": "fixture", "reason": "Missing data"},
        )

    async with httpx.AsyncClient(
        base_url="http://fixture/api/v1", transport=httpx.MockTransport(handler)
    ) as client:
        report = await run_suite(client, golden_suite, ["customer_list", "churn_rate"])
    assert (report.passed, report.failed) == (1, 1)
    assert report.cases[0].failures
    assert report.cases[1].passed


async def test_setup_conflict_is_a_setup_error(golden_suite):
    existing = golden_suite.context.model_dump(mode="json", by_alias=True) | {
        "id": "context",
        "version": 1,
    }
    existing["definition"] = copy.deepcopy(existing["definition"])
    existing["definition"]["rules"] = []

    def handler(request):
        return (
            httpx.Response(200, json={"items": [existing]})
            if request.method == "GET"
            else httpx.Response(409)
        )

    async with httpx.AsyncClient(
        base_url="http://fixture/api/v1", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(EvalSetupError, match="HTTP 409"):
            await run_suite(client, golden_suite, ["total_revenue"])
