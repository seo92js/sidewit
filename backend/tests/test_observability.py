"""Correlate real requests and failures without recording their contents."""

import asyncio
import json
import logging
from uuid import UUID

import pytest

from sidewit.generation.providers.base import LLMError, StructuredResult
from sidewit.generation.providers.observed import ObservedProvider
from sidewit.observability import JsonLogFormatter, current, observe
from tests.test_generate_api import ask, ok
from tests.test_generate_api import context as context
from tests.test_generate_api import fake as fake
from tests.test_tool_selection import saved_tool


@pytest.fixture
def events(caplog):
    caplog.set_level(logging.INFO, logger="sidewit.observability")

    def read(name="request_completed"):
        return [
            json.loads(JsonLogFormatter().format(r))
            for r in caplog.records
            if getattr(r, "event_name", None) == name
        ]

    return read


async def test_generation_logs_metadata_once_and_preserves_body(client, context, fake, events):
    provider = fake(ok())

    async def measured(*args):
        return StructuredResult(parsed=ok(), input_tokens=20, output_tokens=8)

    provider.generate_structured = measured
    response = await ask(client, context)
    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    UUID(request_id)
    assert "request_id" not in response.json()
    entries = [e for e in events() if e["request_id"] == request_id]
    assert len(entries) == 1
    entry = entries[0]
    assert (
        entry.items()
        >= {
            "action": "generate",
            "transport": "rest",
            "status": "ok",
            "status_code": 200,
            "context_id": context["id"],
            "context_version": context["version"],
            "generation_id": response.json()["generation_id"],
            "operation_count": 1,
            "provider": "fake",
            "model": "fake-model",
            "llm_calls": 1,
            "input_tokens": 20,
            "output_tokens": 8,
            "token_usage_complete": True,
            "route": "/generate",
            "method": "POST",
        }.items()
    )
    assert entry["duration_ms"] >= 0
    text = json.dumps(entry, ensure_ascii=False)
    for private in ("서울", "SELECT", "regional_revenue", "2026-09-02"):
        assert private not in text


@pytest.mark.parametrize(
    "path,body,status,code",
    [
        ("/api/v1/generate", {}, 422, "invalid_request"),
        ("/api/web/v1/generate", {}, 403, "web_header_required"),
        ("/api/v1/PRIVATE_PATH?key=PRIVATE_QUERY", {}, 404, "not_found"),
    ],
)
async def test_early_failures_have_ids_and_no_raw_paths(client, events, path, body, status, code):
    response = await client.post(path, json=body, headers={"X-Request-ID": "PRIVATE_CLIENT_ID"})
    assert response.status_code == status
    entry = events()[-1]
    assert entry["request_id"] == response.headers["x-request-id"] != "PRIVATE_CLIENT_ID"
    assert entry["status_code"] == status and entry["error_code"] == code
    assert entry["status"] == "error"
    assert "PRIVATE" not in json.dumps(entry)


async def test_auth_failure_is_observed_without_key(client, api_key, events):
    response = await client.get("/api/v1/contexts", headers={"Authorization": "Bearer PRIVATE_KEY"})
    assert response.status_code == 401
    entry = events()[-1]
    assert entry["error_code"] == "unauthorized"
    assert entry["request_id"] == response.headers["x-request-id"]
    assert "PRIVATE_KEY" not in json.dumps(entry)


async def test_body_limit_is_inside_observation(client, events):
    response = await client.post("/api/v1/generate", content=b"x" * (1024 * 1024 + 1))
    assert response.status_code == 413
    entry = events()[-1]
    assert entry["error_code"] == "request_too_large"
    assert entry["request_id"] == response.headers["x-request-id"]


async def test_unconfigured_provider_has_context_and_zero_calls(client, context, events):
    from sidewit.application.runtime import get_llm_provider
    from sidewit.main import app

    app.dependency_overrides[get_llm_provider] = lambda: None
    response = await ask(client, context)
    assert response.status_code == 503
    entry = events()[-1]
    assert entry["llm_calls"] == 0 and entry["error_code"] == "llm_not_configured"
    assert entry["context_version"] == context["version"]


async def test_unexpected_exception_returns_safe_500(client, context, fake, events, monkeypatch):
    fake(ok())

    async def fail(*args):
        raise RuntimeError("PRIVATE_SQL SELECT 'PRIVATE_VALUE'")

    monkeypatch.setattr("sidewit.api.generate.run_generate", fail)
    response = await ask(client, context)
    assert response.status_code == 500
    entry = events()[-1]
    assert entry["error_code"] == "internal_error" and entry["error_type"] == "RuntimeError"
    assert entry["request_id"] == response.headers["x-request-id"]
    assert "PRIVATE" not in json.dumps(entry) + response.text


async def test_timeout_retains_generation_id_and_inflight_call(
    client, context, fake, events, monkeypatch
):
    provider = fake()

    async def stall(*args):
        await asyncio.Event().wait()

    provider.generate_structured = stall
    monkeypatch.setattr("sidewit.application.generate.GENERATION_TIMEOUT_SECONDS", 0.01)
    response = await ask(client, context)
    assert response.status_code == 504
    entry = events()[-1]
    assert entry["error_code"] == "generation_timeout" and entry["llm_calls"] == 1
    assert entry["token_usage_complete"] is False
    UUID(entry["generation_id"])


async def test_invoke_logs_versions_without_arguments(client, context, events):
    tool = await saved_tool(client, context)
    response = await client.post(
        f"/api/v1/tools/{tool['id']}/invoke", json={"arguments": {"region": "PRIVATE_REGION"}}
    )
    assert response.status_code == 200
    entry = events()[-1]
    assert entry["tool_id"] == tool["id"] and entry["tool_version"] == tool["version"]
    assert entry["llm_calls"] == 0 and entry["action"] == "invoke_tool"
    assert entry["route"] == "/tools/{tool_id}/invoke"
    assert "PRIVATE_REGION" not in json.dumps(entry)


async def test_parallel_requests_do_not_share_metadata(client, context, fake, events):
    provider = fake()
    ready = asyncio.Event()
    entered = 0

    async def answer(*args):
        nonlocal entered
        entered += 1
        if entered == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 2)
        return StructuredResult(
            parsed=ok("SELECT id FROM customers", []), input_tokens=7, output_tokens=2
        )

    provider.generate_structured = answer
    responses = await asyncio.gather(
        ask(client, context, question="PRIVATE_QUESTION_A"),
        ask(client, context, question="PRIVATE_QUESTION_B"),
    )
    assert all(r.status_code == 200 for r in responses)
    entries = {e["request_id"]: e for e in events() if e["action"] == "generate"}
    assert len(entries) == 2
    for response in responses:
        entry = entries[response.headers["x-request-id"]]
        assert entry["generation_id"] == response.json()["generation_id"]
        assert (entry["llm_calls"], entry["input_tokens"], entry["output_tokens"]) == (1, 7, 2)
    assert current() is None


async def test_partial_usage_stays_incomplete_after_failed_call(events):
    class Provider:
        name, model = "fake", "fixture"
        calls = 0

        async def generate_structured(self, *args):
            self.calls += 1
            if self.calls == 2:
                raise LLMError("PRIVATE_PROVIDER_ERROR")
            return StructuredResult(parsed=ok(), input_tokens=4, output_tokens=1)

    with observe("request_completed", transport="rest", action="generate"):
        from sidewit.observability import annotate

        annotate(llm_calls=0)
        provider = ObservedProvider(Provider())
        await provider.generate_structured("", [], type(ok()))
        with pytest.raises(LLMError):
            await provider.generate_structured("", [], type(ok()))
        await provider.generate_structured("", [], type(ok()))
    entry = events()[-1]
    assert (entry["llm_calls"], entry["input_tokens"], entry["output_tokens"]) == (3, 8, 2)
    assert entry["token_usage_complete"] is False


def test_formatter_omits_messages_tracebacks_and_unknown_fields():
    try:
        raise RuntimeError("PRIVATE_EXCEPTION")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "httpx", logging.ERROR, "", 1, "PRIVATE_PROMPT %s", ("PRIVATE_KEY",), sys.exc_info()
        )
    record.event_fields = {"question": "PRIVATE_QUESTION", "error_code": "PRIVATE_CODE"}
    record.event_name = "PRIVATE_EVENT"
    text = JsonLogFormatter().format(record)
    assert "PRIVATE" not in text
    assert json.loads(text)["error_type"] == "RuntimeError"


@pytest.mark.parametrize("fail_second", [False, True])
async def test_concurrent_usage_reports_cannot_hide_a_failed_call(events, fail_second):
    ready = asyncio.Event()

    class Provider:
        name, model = "fake", "fixture"
        calls = 0

        async def generate_structured(self, *args):
            self.calls += 1
            if self.calls == 1:
                await ready.wait()
            else:
                ready.set()
                if fail_second:
                    raise LLMError("PRIVATE_FAILURE")
            return StructuredResult(parsed=ok(), input_tokens=4, output_tokens=1)

    with observe("request_completed", transport="rest", action="generate"):
        provider = ObservedProvider(Provider())
        await asyncio.gather(
            provider.generate_structured("", [], type(ok())),
            provider.generate_structured("", [], type(ok())),
            return_exceptions=True,
        )
    entry = events()[-1]
    assert entry["llm_calls"] == 2
    assert entry["token_usage_complete"] is not fail_second
    assert entry["input_tokens"] == (4 if fail_second else 8)


async def test_cancelled_request_logs_and_resets_context(events):
    with pytest.raises(asyncio.CancelledError):
        with observe("request_completed", transport="rest", action="generate"):
            raise asyncio.CancelledError()
    assert current() is None
    assert events()[-1]["status"] == "cancelled"


async def test_disconnect_while_reading_body_is_not_logged_as_success(events):
    from sidewit.api.limits import RequestSizeLimit
    from sidewit.api.observability import RequestObservation

    async def unused_app(*args):
        pytest.fail("Disconnected body must not reach the app")

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        pytest.fail("Do not send a response to the disconnected client")

    await RequestObservation(RequestSizeLimit(unused_app))(
        {"type": "http", "method": "POST", "path": "/api/v1/generate"}, receive, send
    )
    assert events()[-1]["error_code"] == "client_disconnected"
    assert events()[-1]["status_code"] == 499


@pytest.mark.parametrize(
    "destination",
    ["file", "stream_subclass", "named_logger", "muted_events", "record_exporter", "queue"],
)
def test_existing_handlers_are_safe_and_stdout_is_not_duplicated(tmp_path, destination):
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""
        import logging
        import logging.handlers
        import queue
        import sys
        from sidewit.observability import configure_logging, observe

        class Console(logging.StreamHandler):
            pass

        path, destination = sys.argv[1:]
        target = open(path, 'w')
        class RecordExporter(logging.Handler):
            def emit(self, record):
                # Like socket/telemetry exporters: do not call self.format().
                assert 'PRIVATE' not in str(record.__dict__)
                assert record.exc_info is None and record.exc_text is None
                target.write(record.getMessage() + '\\n')
                target.flush()

        handler = (logging.handlers.WatchedFileHandler(path) if destination == 'file' else
                   RecordExporter() if destination == 'record_exporter' else Console(target))
        listener = None
        if destination == 'queue':
            pending = queue.Queue()
            handler = logging.handlers.QueueHandler(pending)
            listener = logging.handlers.QueueListener(pending, Console(target))
            listener.start()
        handler.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
        logger = (logging.getLogger('httpx') if destination == 'named_logger'
                  else logging.getLogger())
        logger.addHandler(handler)
        if destination == 'muted_events':
            events = logging.getLogger('sidewit.observability')
            events.disabled = True
            events.setLevel(logging.CRITICAL)
            events.propagate = False
        configure_logging()
        configure_logging()
        try:
            raise ValueError('PRIVATE_EXCEPTION')
        except ValueError:
            logging.getLogger('httpx').warning('PRIVATE_PROMPT %s', 'PRIVATE_KEY', exc_info=True,
                                               extra={'private_payload': 'PRIVATE_EXTRA'})
        with observe('request_completed', transport='rest', action='test'):
            pass
        handler.flush()
        if listener:
            listener.stop()
        target.close()
    """)
    path = tmp_path / "operational.log"
    result = subprocess.run(
        [sys.executable, "-c", code, str(path), destination],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "PRIVATE" not in result.stdout + result.stderr + path.read_text()
    stdout = [json.loads(line) for line in result.stdout.splitlines()]
    file_entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert sum(e["event"] == "request_completed" for e in stdout) == 1
    assert any(e.get("error_type") == "ValueError" for e in file_entries)


@pytest.mark.parametrize("completed", [False, True])
async def test_failure_after_headers_keeps_wire_status_and_cause(events, completed):
    from sidewit.api.observability import RequestObservation

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}", "more_body": not completed})
        raise ValueError("PRIVATE_BACKGROUND_FAILURE")

    async def receive():
        return {"type": "http.request", "body": b""}

    sent = []

    async def send(message):
        sent.append(message)

    application = RequestObservation(app)
    scope = {"type": "http", "method": "GET", "path": "/api/v1/health"}
    if completed:
        await application(scope, receive, send)
    else:
        with pytest.raises(RuntimeError, match="Response interrupted"):
            await application(scope, receive, send)
    entry = events()[-1]
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [200]
    assert entry["status_code"] == 200
    assert entry["status"] == "error" and entry["level"] == "ERROR"
    assert entry["error_type"] == "ValueError"
    assert entry["response_complete"] is completed
    assert "PRIVATE" not in json.dumps(entry)
    assert current() is None


async def test_cancellation_after_headers_preserves_sent_status(events):
    from sidewit.api.observability import RequestObservation

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise asyncio.CancelledError()

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        pass

    with pytest.raises(asyncio.CancelledError):
        await RequestObservation(app)(
            {"type": "http", "method": "GET", "path": "/mcp"}, receive, send
        )
    entry = events()[-1]
    assert entry["status_code"] == 200
    assert entry["status"] == "cancelled" and entry["error_code"] == "request_cancelled"
    assert entry["response_complete"] is False
    assert current() is None


async def test_failed_header_send_does_not_claim_a_status_was_sent(events):
    from sidewit.api.observability import RequestObservation

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def receive():
        return {"type": "http.request", "body": b""}

    sent = []
    first = True

    async def send(message):
        nonlocal first
        if first:
            first = False
            raise OSError("PRIVATE_SEND_FAILURE")
        sent.append(message)

    await RequestObservation(app)(
        {"type": "http", "method": "GET", "path": "/api/v1/health"}, receive, send
    )
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [500]
    entry = events()[-1]
    assert entry["status_code"] == 500 and entry["error_type"] == "OSError"
    assert entry["response_complete"] is True
    assert "PRIVATE" not in json.dumps(entry) + str(sent)
