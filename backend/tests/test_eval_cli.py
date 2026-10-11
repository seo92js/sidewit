import json

import httpx
import pytest

from sidewit.eval import cli
from sidewit.eval.runner import CaseResult, EvalSetupError, SuiteReport
from sidewit.eval.scoring import Failure


@pytest.fixture
def report(golden_suite):
    return SuiteReport(
        suite="commerce",
        api_url="http://fixture/api/v1/",
        context_id="fixture",
        context_version=1,
        provider="fake",
        model="fake-model",
        prompt_version="generate_v1",
        started_at=golden_suite.reference_time,
        total=1,
        passed=1,
        failed=0,
        cases=[
            CaseResult(
                id="total_revenue",
                category="business_rule",
                question="전체 매출 알려줘",
                reference_time=golden_suite.reference_time,
                passed=True,
                expected_status="ok",
                actual_status="ok",
                generation_id="fixture",
                failures=[],
            )
        ],
    )


@pytest.mark.parametrize("failed", [False, True])
def test_cli_exit_codes_and_json_report(tmp_path, monkeypatch, capsys, report, failed):
    if failed:
        report.passed, report.failed = 0, 1
        report.cases[0].passed = False
        report.cases[0].failures = [Failure(expectation="constraints", detail="Missing rule")]

    async def run(args, suite, headers):
        assert args.cases == ["total_revenue"]
        assert args.timeout == 30
        assert headers == {"Authorization": "Bearer fixture-secret"}
        return report

    monkeypatch.setattr(cli, "_run", run)
    monkeypatch.setattr(cli, "load_suite", lambda path: object())
    monkeypatch.setenv("SIDEWIT_API_KEY", "fixture-secret")
    output = tmp_path / "report.json"
    code = cli.main(
        ["run", "fixture", "--case", "total_revenue", "--timeout", "30", "--out", str(output)]
    )
    assert code == int(failed)
    result = json.loads(output.read_text())
    assert result["failed"] == int(failed)
    assert result["cases"][0]["reference_time"]
    stdout = capsys.readouterr().out
    assert ("FAIL" if failed else "PASS") in stdout
    assert "fixture-secret" not in stdout + output.read_text()
    assert ("constraints" in stdout) == failed


@pytest.mark.parametrize("kind", ["setup", "http", "file"])
def test_cli_setup_and_io_errors_return_two(tmp_path, monkeypatch, capsys, kind):
    async def run(args, suite, headers):
        if kind == "http":
            raise httpx.ConnectError("http://host/?key=fixture-secret")
        raise EvalSetupError("Fixture setup failure")

    monkeypatch.setattr(cli, "_run", run)
    if kind != "file":
        monkeypatch.setattr(cli, "load_suite", lambda path: object())
    assert cli.main(["run", str(tmp_path / "missing")]) == 2
    output = capsys.readouterr()
    assert "error:" in output.err
    assert "fixture-secret" not in output.err


def test_cli_report_write_error_returns_two(tmp_path, monkeypatch, capsys, report):
    async def run(*args):
        return report

    monkeypatch.setattr(cli, "_run", run)
    monkeypatch.setattr(cli, "load_suite", lambda path: object())
    assert cli.main(["run", "fixture", "--out", str(tmp_path / "missing" / "report.json")]) == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "bad"])
def test_timeout_must_be_positive(timeout):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "fixture", "--timeout", timeout])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "url",
    [
        "localhost:8000",
        "ftp://host",
        "https://user:pass@host",
        "https://host/?key=secret",
        "https://host/#secret",
        "http://[",
    ],
)
def test_api_url_rejects_invalid_or_credential_bearing_urls(url):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "fixture", "--api-url", url])
    assert exc.value.code == 2


def test_cli_json_parse_error_is_readable(tmp_path, capsys):
    (tmp_path / "context.json").write_text("{")
    assert cli.main(["run", str(tmp_path)]) == 2
    assert "error:" in capsys.readouterr().err


def test_cli_yaml_parse_error_is_readable(tmp_path, golden_suite, capsys):
    (tmp_path / "context.json").write_text(golden_suite.context.model_dump_json(by_alias=True))
    (tmp_path / "cases.yaml").write_text("cases: [")
    assert cli.main(["run", str(tmp_path)]) == 2
    assert "error:" in capsys.readouterr().err


def test_render_reports_status_validation_and_metadata(report):
    report.passed, report.failed = 0, 1
    case = report.cases[0]
    case.passed = False
    case.actual_status = "generation_invalid"
    case.failures = [Failure(expectation="generation", detail="422 generation_invalid")]
    case.validation_errors = [{"code": "unknown_field", "message": "missing field"}]
    rendered = cli.render(report)
    assert "generation_invalid (expected ok)" in rendered
    assert "unknown_field" in rendered
    assert "fake-model" in rendered and "generate_v1" in rendered
    assert "0/1 passed, 1 failed" in rendered


async def test_cli_http_configuration_uses_bearer_and_api_prefix(monkeypatch, golden_suite):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/contexts"):
            return httpx.Response(
                200 if request.method == "GET" else 201,
                json={"items": []} if request.method == "GET" else {"id": "fixture", "version": 1},
            )
        if request.url.path.endswith("/generate"):
            return httpx.Response(
                200,
                json={
                    "status": "unanswerable",
                    "reason": "Missing data",
                    "generation_id": "fixture",
                },
            )
        return httpx.Response(
            200, json={"provider": "fake", "model": "fake-model", "prompt_version": "generate_v1"}
        )

    original_client = httpx.AsyncClient

    def with_transport(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli.httpx, "AsyncClient", with_transport)
    from argparse import Namespace

    args = Namespace(api_url="http://fixture:8000/", timeout=10, cases=["churn_rate"])
    result = await cli._run(args, golden_suite, {"Authorization": "Bearer fixture-secret"})
    assert result.passed == 1
    assert all(r.url.path.startswith("/api/v1/") for r in requests)
    assert all(r.headers["Authorization"] == "Bearer fixture-secret" for r in requests)
    assert "fixture-secret" not in result.model_dump_json()
