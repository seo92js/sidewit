"""Runs a golden suite through the public Sidewit API (the same path applications use)."""

from datetime import datetime
from time import perf_counter
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError

from sidewit.domain.operation import SqlOperation
from sidewit.eval.cases import GoldenCase, Suite
from sidewit.eval.plans import score_plan
from sidewit.eval.scoring import Failure


class ExecutionComparison(BaseModel):
    matched: bool
    expected_row_count: int
    actual_row_count: int | None = None
    error: str | None = None


class ExecutionEvaluator(Protocol):
    async def prepare(self, cases: list[GoldenCase]) -> None: ...

    async def compare(self, case_id: str, operation: SqlOperation) -> ExecutionComparison: ...


class CaseResult(BaseModel):
    id: str
    category: str
    question: str
    reference_time: datetime
    passed: bool
    expected_status: str
    # ok | ambiguous | unanswerable | generation_invalid | <api error code>
    actual_status: str
    # ok only: metric | generated_sql | plan
    actual_source: str | None = None
    generation_id: str | None
    failures: list[Failure]
    validation_errors: list[dict[str, Any]] = []
    execution: ExecutionComparison | None = None
    request_latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class SuiteReport(BaseModel):
    suite: str
    api_url: str
    context_id: str
    context_version: int
    provider: str | None
    model: str | None
    prompt_version: str | None
    started_at: datetime
    total: int
    passed: int
    failed: int
    cases: list[CaseResult]
    execution_enabled: bool = False


class EvalSetupError(Exception):
    pass


async def run_suite(
    client: httpx.AsyncClient,
    suite: Suite,
    case_ids: list[str] | None = None,
    execution: ExecutionEvaluator | None = None,
) -> SuiteReport:
    started = datetime.now().astimezone()
    unknown = set(case_ids or []) - {c.id for c in suite.cases}
    if unknown:
        raise EvalSetupError(f"Unknown case ids: {', '.join(sorted(unknown))}")
    cases = [c for c in suite.cases if not case_ids or c.id in case_ids]
    if not cases:
        raise EvalSetupError("No cases selected.")
    if execution is not None:
        await execution.prepare(cases)
    context = await _sync_context(client, suite)

    results = [await _run_case(client, suite, context["id"], case, execution) for case in cases]
    meta = await _generation_meta(client, results)
    passed = sum(r.passed for r in results)
    return SuiteReport(
        suite=suite.name,
        api_url=str(client.base_url),
        context_id=context["id"],
        context_version=context["version"],
        provider=meta.get("provider"),
        model=meta.get("model"),
        prompt_version=meta.get("prompt_version"),
        started_at=started,
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        cases=results,
        execution_enabled=execution is not None,
    )


async def _sync_context(client: httpx.AsyncClient, suite: Suite) -> dict[str, Any]:
    """Creates the suite's context, or brings an existing one up to date with the fixture."""
    body = suite.context.model_dump(mode="json", by_alias=True)
    res = await client.get("/contexts")
    _ensure(res, "list contexts")
    existing = next((c for c in res.json()["items"] if c["name"] == suite.context.name), None)
    if existing is None:
        res = await client.post("/contexts", json=body)
        _ensure(res, "create the suite context")
        return res.json()
    if all(existing[key] == value for key, value in body.items()):
        return existing
    res = await client.put(
        f"/contexts/{existing['id']}", json=body | {"expected_version": existing["version"]}
    )
    _ensure(res, "update the suite context")
    return res.json()["context"]


async def _run_case(
    client: httpx.AsyncClient,
    suite: Suite,
    context_id: str,
    case: GoldenCase,
    execution: ExecutionEvaluator | None = None,
) -> CaseResult:
    reference_time = case.reference_time or suite.reference_time
    common = {
        "id": case.id,
        "category": case.category,
        "question": case.question,
        "reference_time": reference_time,
        "expected_status": case.expect.status,
    }
    started = perf_counter()
    try:
        res = await client.post(
            "/generate",
            json={
                "context_id": context_id,
                "question": case.question,
                "reference_time": reference_time.isoformat(),
            },
        )
    except httpx.HTTPError as e:
        common["request_latency_ms"] = round((perf_counter() - started) * 1000)
        # Exception text may include a credential-bearing URL; record the class only.
        return CaseResult(
            **common,
            passed=False,
            actual_status="transport_error",
            generation_id=None,
            failures=[Failure(expectation="generation", detail=type(e).__name__)],
        )
    common["request_latency_ms"] = round((perf_counter() - started) * 1000)
    try:
        body = res.json()
        if not isinstance(body, dict):
            raise ValueError("Response must be an object.")
    except ValueError:
        return CaseResult(
            **common,
            passed=False,
            actual_status=f"http_{res.status_code}",
            generation_id=None,
            failures=[Failure(expectation="generation", detail="Response was not a JSON object.")],
        )

    if res.status_code != 200:
        details = body.get("details", {})
        error = body.get("error", f"http_{res.status_code}")
        return CaseResult(
            **common,
            passed=False,
            actual_status=error,
            generation_id=details.get("generation_id"),
            failures=[Failure(expectation="generation", detail=f"{res.status_code} {error}")],
            validation_errors=details.get("validation_errors", []),
        )

    status = body.get("status")
    if status not in {"ok", "ambiguous", "unanswerable"} or not body.get("generation_id"):
        return CaseResult(
            **common,
            passed=False,
            actual_status="invalid_response",
            generation_id=None,
            failures=[Failure(expectation="generation", detail="Missing generation id or status.")],
        )
    failures: list[Failure] = []
    validation_errors: list[dict[str, Any]] = []
    comparison = None
    if status != case.expect.status:
        failures.append(
            Failure(expectation="status", detail=f"expected {case.expect.status}, got {status}")
        )
    elif status == "ok":
        failures, validation_errors = score_plan(case.expect, body, suite.context)
        source = body.get("source")
        if case.expect.source is not None and source != case.expect.source:
            failures.append(
                Failure(expectation="source", detail=f"expected {case.expect.source}, got {source}")
            )
        if execution is not None and not failures:
            comparison = await _compare_rows(execution, case.id, body, failures)
    return CaseResult(
        **common,
        passed=not failures,
        actual_status=status,
        actual_source=body.get("source"),
        generation_id=body["generation_id"],
        failures=failures,
        validation_errors=validation_errors,
        execution=comparison,
    )


async def _compare_rows(
    execution: ExecutionEvaluator, case_id: str, body: dict[str, Any], failures: list[Failure]
) -> ExecutionComparison | None:
    try:
        operation = SqlOperation.model_validate(body["operations"][0]["operation"])
    except (KeyError, IndexError, TypeError, ValidationError):
        detail = "Response operation was malformed."
        failures.append(Failure(expectation="execution", detail=detail))
        return None
    comparison = await execution.compare(case_id, operation)
    if comparison.error:
        failures.append(Failure(expectation="execution", detail=comparison.error))
    elif not comparison.matched:
        if comparison.expected_row_count == comparison.actual_row_count:
            detail = f"Result values differ ({comparison.expected_row_count} rows each)."
        else:
            detail = (
                "Result row counts differ "
                f"(expected {comparison.expected_row_count}, got {comparison.actual_row_count})."
            )
        failures.append(Failure(expectation="execution", detail=detail))
    return comparison


async def _generation_meta(client: httpx.AsyncClient, results: list[CaseResult]) -> dict:
    first = {}
    for result in results:
        if result.generation_id is None:
            continue
        try:
            res = await client.get(f"/generations/{result.generation_id}")
            record = res.json() if res.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if not first:
            first = record
        result.input_tokens = record.get("input_tokens")
        result.output_tokens = record.get("output_tokens")
    return first


def _ensure(res: httpx.Response, action: str) -> None:
    if res.status_code >= 400:
        raise EvalSetupError(f"Could not {action}: HTTP {res.status_code}")
