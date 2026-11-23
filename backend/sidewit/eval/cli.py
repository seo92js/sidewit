"""sidewit-eval: run a golden suite against a running Sidewit API.

    sidewit-eval run ../evals/commerce --api-url http://localhost:8000 [--out report.json]

The provider and model are whatever the server is configured with (SIDEWIT_LLM_*); the
report records them. Exit code 1 if any case fails.
"""

import argparse
import asyncio
import math
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from sidewit.eval.cases import load_suite
from sidewit.eval.runner import EvalSetupError, SuiteReport, run_suite
from sidewit.security import redact


def _api_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        # Accessing port also validates numeric syntax and range.
        _ = parsed.port
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError) as e:
        raise argparse.ArgumentTypeError("API URL is malformed.") from e
    if url.scheme not in {"http", "https"} or not url.host:
        raise argparse.ArgumentTypeError("API URL must be an absolute HTTP(S) URL.")
    if url.userinfo or url.query or url.fragment:
        raise argparse.ArgumentTypeError("API URL must not include credentials, query or fragment.")
    return value


def _timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from e
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive number")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sidewit-eval")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a golden suite")
    run.add_argument("suite", type=Path, help="directory with context.json and cases.yaml")
    run.add_argument("--api-url", type=_api_url, default="http://localhost:8000")
    run.add_argument("--api-key", default=os.environ.get("SIDEWIT_API_KEY"))
    run.add_argument("--case", action="append", dest="cases", help="run only this case id")
    run.add_argument("--out", type=Path, help="write the JSON report here")
    run.add_argument("--timeout", type=_timeout, default=120.0)
    run.add_argument(
        "--execution-url",
        help="compare result rows in an isolated PostgreSQL _eval database",
    )
    args = parser.parse_args(argv)

    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    try:
        suite = load_suite(args.suite)
        report = asyncio.run(_run(args, suite, headers))
        if args.out:
            args.out.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    except httpx.HTTPError as e:
        print(f"error: API request failed ({type(e).__name__})", file=sys.stderr)
        return 2
    except (EvalSetupError, OSError, ValueError, yaml.YAMLError) as e:
        print(f"error: {redact(str(e), [args.api_key])}", file=sys.stderr)
        return 2

    print(render(report))
    if args.out:
        print(f"\nreport: {args.out}")
    return 0 if report.failed == 0 else 1


async def _run(args, suite, headers) -> SuiteReport:
    async with httpx.AsyncClient(
        base_url=args.api_url.rstrip("/") + "/api/v1", headers=headers, timeout=args.timeout
    ) as client:
        if not getattr(args, "execution_url", None):
            return await run_suite(client, suite, args.cases)
        try:
            from sidewit.eval.execution import PostgresExecutionEvaluator
        except ImportError as e:
            raise EvalSetupError(
                "Execution evaluation requires the backend dev dependencies."
            ) from e
        async with PostgresExecutionEvaluator(args.execution_url) as execution:
            return await run_suite(client, suite, args.cases, execution)


def render(report: SuiteReport) -> str:
    lines = [
        f"suite {report.suite} · context v{report.context_version} · "
        f"provider {report.provider} · model {report.model} · prompt {report.prompt_version}"
        + (" · execution rows checked" if report.execution_enabled else ""),
        "",
    ]
    width = max((len(c.id) for c in report.cases), default=10)
    for case in report.cases:
        mark = "PASS" if case.passed else "FAIL"
        actual = case.actual_status + (f"/{case.actual_source}" if case.actual_source else "")
        status = actual if case.passed else f"{actual} (expected {case.expected_status})"
        actual_rows = case.execution.actual_row_count if case.execution else None
        rows = f"  rows={actual_rows}" if actual_rows is not None else ""
        lines.append(f"{mark}  {case.id:<{width}}  {status}  {case.generation_id or '-'}{rows}")
        for failure in case.failures:
            lines.append(f"      ✗ {failure.expectation}: {failure.detail}")
        for error in case.validation_errors:
            lines.append(f"      ✗ validation {error.get('code')}: {error.get('message')}")
    lines += ["", f"{report.passed}/{report.total} passed, {report.failed} failed"]
    reported = [
        case
        for case in report.cases
        if case.input_tokens is not None and case.output_tokens is not None
    ]
    if reported:
        lines.append(
            "reported tokens: "
            f"input {sum(case.input_tokens or 0 for case in reported)}, "
            f"output {sum(case.output_tokens or 0 for case in reported)} "
            f"({len(reported)}/{report.total} cases)"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
