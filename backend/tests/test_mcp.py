"""Protocol-level tests using the official SDK client over the complete ASGI app."""

import json
from contextlib import asynccontextmanager

import httpx
import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from sidewit.application.runtime import get_history, get_llm_provider, get_store
from sidewit.config import get_settings
from sidewit.main import create_app


@pytest.fixture
async def mcp_app(monkeypatch, store, history):
    monkeypatch.setenv("SIDEWIT_MCP_ENABLED", "true")
    monkeypatch.setenv("SIDEWIT_MCP_KEY", "mcp-secret")
    monkeypatch.setenv("SIDEWIT_API_KEY", "api-secret")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_history] = lambda: history
    app.dependency_overrides[get_llm_provider] = lambda: None
    try:
        yield app
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@asynccontextmanager
async def connect(app):
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"Authorization": "Bearer mcp-secret"},
            follow_redirects=True,
        ) as http,
    ):
        async with streamable_http_client("http://localhost/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1], read_timeout_seconds=5) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "Sidewit"
                yield session


def payload(result):
    assert json.loads(result.content[0].text) == result.structured_content
    return result.structured_content


async def test_handshake_lists_only_four_tools(mcp_app):
    async with connect(mcp_app) as session:
        listed = await session.list_tools()
        assert {t.name for t in listed.tools} == {
            "list_contexts",
            "list_tools",
            "generate_operations",
            "invoke_tool",
        }
        result = await session.call_tool("list_contexts", {})
        assert not result.is_error
        assert payload(result) == {"items": [], "next_offset": None}


@pytest.fixture
async def seed(mcp_app):
    from tests.test_tools_api import DEFINITION, tool_body

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app),
        base_url="http://localhost",
        headers={"Authorization": "Bearer api-secret"},
    ) as rest:
        response = await rest.post(
            "/api/v1/contexts",
            json={
                "name": "commerce",
                "dialect": "postgresql",
                "timezone": "Asia/Seoul",
                "definition": DEFINITION,
            },
        )
        assert response.status_code == 201, response.text
        context = response.json()
        response = await rest.post("/api/v1/tools", json=tool_body(context))
        assert response.status_code == 201, response.text
        yield context, response.json(), rest


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize(
    "authorization", [None, "Bearer api-secret", "Bearer wrong", "Basic mcp-secret"]
)
async def test_mcp_key_required_on_all_methods(mcp_app, method, path, authorization):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app), base_url="http://localhost"
    ) as http:
        response = await http.request(
            method, path, headers={"Authorization": authorization} if authorization else {}
        )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "mcp-secret" not in response.text


async def test_mcp_key_does_not_authenticate_rest_or_web(mcp_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app),
        base_url="http://localhost",
        headers={"Authorization": "Bearer mcp-secret"},
    ) as http:
        assert (await http.get("/api/v1/contexts")).status_code == 401
        assert (
            await http.get("/api/web/v1/contexts", headers={"X-Sidewit-Web": "1"})
        ).status_code == 401
        assert (await http.get("/api/v1/health")).status_code == 200
        schema = (await http.get("/openapi.json")).json()
        assert not any(p.startswith("/mcp") for p in schema["paths"])


async def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SIDEWIT_MCP_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as http:
            for path in ("/mcp", "/mcp/"):
                assert (await http.post(path, json={})).status_code == 404
    finally:
        get_settings.cache_clear()


async def test_explicit_no_auth(mcp_app, monkeypatch):
    monkeypatch.setenv("SIDEWIT_MCP_KEY", "")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides.update(mcp_app.dependency_overrides)
    async with connect(app) as session:
        result = await session.call_tool("list_contexts")
        assert not result.is_error
    # connect's supplied key was intentionally not required; verify absence too.
    app = create_app()
    app.dependency_overrides.update(mcp_app.dependency_overrides)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as http,
    ):
        response = await http.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Host": "attacker.example"}, 421),
        ({"Origin": "https://attacker.example"}, 403),
    ],
)
async def test_host_and_origin_validation(mcp_app, headers, status):
    async with (
        mcp_app.router.lifespan_context(mcp_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mcp_app),
            base_url="http://localhost",
            headers={
                "Authorization": "Bearer mcp-secret",
                "Accept": "application/json, text/event-stream",
            },
        ) as http,
    ):
        response = await http.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert response.status_code == status


async def test_discovery_and_deterministic_invoke_share_rest_store(mcp_app, seed):
    from tests.test_tools_api import ARGS

    context, tool, rest = seed
    async with connect(mcp_app) as session:
        contexts = payload(await session.call_tool("list_contexts"))
        assert contexts["items"][0]["id"] == context["id"]
        assert "definition" not in contexts["items"][0]
        tools = payload(await session.call_tool("list_tools", {"context_id": context["id"]}))
        assert tools["items"][0]["input_schema"] == tool["input_schema"]
        assert "operation" not in tools["items"][0]
        request = {"arguments": ARGS, "expected_version": tool["version"]}
        result = await session.call_tool("invoke_tool", {"tool_id": tool["id"], **request})
        expected = await rest.post(f"/api/v1/tools/{tool['id']}/invoke", json=request)
        assert not result.is_error
        assert payload(result) == expected.json()
        conflict = await session.call_tool(
            "invoke_tool", {"tool_id": tool["id"], **request, "expected_version": 999}
        )
        assert conflict.is_error
        assert payload(conflict)["error"] == "version_conflict"
        await rest.patch(
            f"/api/v1/tools/{tool['id']}",
            json={"expected_version": tool["version"], "enabled": False},
        )
        assert (
            payload(await session.call_tool("list_tools", {"context_id": context["id"]}))["items"]
            == []
        )
        disabled = await session.call_tool(
            "invoke_tool", {"tool_id": tool["id"], "arguments": ARGS}
        )
        assert disabled.is_error
        assert payload(disabled)["error"] == "tool_disabled"


async def test_generate_without_provider_has_clear_error(mcp_app, seed):
    context, _, _ = seed
    async with connect(mcp_app) as session:
        result = await session.call_tool(
            "generate_operations", {"context_id": context["id"], "question": "Count orders"}
        )
        assert result.is_error
        assert payload(result)["error"] == "llm_not_configured"


@pytest.mark.parametrize(
    "name,args",
    [
        ("list_contexts", {"limit": 0}),
        ("list_contexts", {"limit": 101}),
        ("list_contexts", {"offset": -1}),
        ("list_contexts", {"extra": True}),
        ("list_tools", {"context_id": "bad"}),
        ("invoke_tool", {"tool_id": "bad", "arguments": {}}),
        (
            "generate_operations",
            {"context_id": "12345678-1234-1234-1234-123456789abc", "question": " "},
        ),
        (
            "generate_operations",
            {
                "context_id": "12345678-1234-1234-1234-123456789abc",
                "question": "Count",
                "reference_time": "2026-10-01T00:00:00",
            },
        ),
    ],
)
async def test_invalid_inputs_are_sanitized(mcp_app, name, args):
    async with connect(mcp_app) as session:
        result = await session.call_tool(name, args)
        assert result.is_error
        error = payload(result)
        assert error["error"] == "invalid_request"
        assert all("input" not in e and "ctx" not in e for e in error["details"]["errors"])


async def test_generated_operation_and_history_shared(mcp_app, seed, history):
    from sidewit.generation.providers.fake import FakeProvider
    from tests.test_generate_api import ok

    context, tool, rest = seed
    await rest.delete(f"/api/v1/tools/{tool['id']}")
    mcp_app.dependency_overrides[get_llm_provider] = lambda: FakeProvider(
        [ok("SELECT id FROM customers", [])]
    )
    async with connect(mcp_app) as session:
        result = await session.call_tool(
            "generate_operations", {"context_id": context["id"], "question": "List customer IDs"}
        )
        assert not result.is_error
        body = payload(result)
        assert body["status"] == "ok"
        assert body["operations"][0]["validation"]["valid"]
        detail = (await rest.get(f"/api/v1/generations/{body['generation_id']}")).json()
        assert detail["operations"] is None  # Standalone generation uses the same history record.
        assert detail["operation"] == body["operations"][0]["operation"]
        assert len(history.recent(None, 100)) == 1


async def test_clarification_continuation_and_version_conflict(mcp_app, seed):
    from sidewit.generation.providers.fake import FakeProvider
    from tests.test_generate_api import not_ok, ok

    context, tool, rest = seed
    await rest.delete(f"/api/v1/tools/{tool['id']}")
    provider = FakeProvider(
        [
            not_ok("ambiguous", "Choose the region.", ["Which region?"]),
            ok(
                "SELECT id FROM customers WHERE region = :region",
                [{"name": "region", "type": "string", "value": "서울"}],
            ),
        ]
    )
    mcp_app.dependency_overrides[get_llm_provider] = lambda: provider
    async with connect(mcp_app) as session:
        first = payload(
            await session.call_tool(
                "generate_operations",
                {
                    "context_id": context["id"],
                    "question": "List customer IDs in a region",
                    "reference_time": "2026-10-16T09:00:00+09:00",
                },
            )
        )
        assert first["status"] == "ambiguous"
        request = first["continuation_request"]
        assert request["expected_context_version"] == context["version"]
        question = first["clarification_questions"][0]
        request["clarification_answers"] = [
            {"id": question["id"], "question": question["question"], "answer": "서울"}
        ]
        second = await session.call_tool("generate_operations", request)
        assert not second.is_error
        assert payload(second)["status"] == "ok"
        assert payload(second)["operations"][0]["operation"]["parameters"] == {"region": "서울"}
        # A Context update between rounds is rejected before another provider call.
        current = (await rest.get(f"/api/v1/contexts/{context['id']}")).json()
        updated = await rest.put(
            f"/api/v1/contexts/{context['id']}",
            json={
                k: current[k]
                for k in ("name", "dialect", "timezone", "definition", "allow_generated_sql")
            }
            | {"expected_version": current["version"], "name": "changed"},
        )
        assert updated.status_code == 200
        conflict = await session.call_tool("generate_operations", request)
        assert conflict.is_error and payload(conflict)["error"] == "context_changed"
        assert len(provider.calls) == 2


async def test_rejected_sql_and_internal_exceptions_not_exposed(mcp_app, seed, monkeypatch, caplog):
    from sidewit.generation.providers.fake import FakeProvider
    from tests.test_generate_api import ok

    context, tool, rest = seed
    await rest.delete(f"/api/v1/tools/{tool['id']}")
    sql = "SELECT id FROM secret_table"
    provider = FakeProvider([ok(sql, []), ok(sql, [])])
    mcp_app.dependency_overrides[get_llm_provider] = lambda: provider
    async with connect(mcp_app) as session:
        rejected = await session.call_tool(
            "generate_operations", {"context_id": context["id"], "question": "List IDs"}
        )
        assert rejected.is_error
        assert payload(rejected)["error"] == "generation_invalid"
        assert sql not in rejected.content[0].text
        assert "statement" not in payload(rejected)["details"]

        def explode(*args):
            raise RuntimeError("PRIVATE_EXCEPTION_WITH_SQL")

        monkeypatch.setattr("sidewit.mcp.server._list_contexts", explode)
        result = await session.call_tool("list_contexts")
        assert result.is_error and payload(result)["error"] == "internal_error"
        assert "PRIVATE_EXCEPTION_WITH_SQL" not in result.content[0].text
        assert "PRIVATE_EXCEPTION_WITH_SQL" not in caplog.text


async def test_generation_deadline_applies_to_mcp(mcp_app, seed, monkeypatch):
    import asyncio

    from sidewit.generation.providers.fake import FakeProvider

    context, _, _ = seed
    provider = FakeProvider([])
    cancelled = asyncio.Event()

    async def stalled(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    provider.generate_structured = stalled
    mcp_app.dependency_overrides[get_llm_provider] = lambda: provider
    monkeypatch.setattr("sidewit.application.generate.GENERATION_TIMEOUT_SECONDS", 0.01)
    async with connect(mcp_app) as session:
        result = await session.call_tool(
            "generate_operations", {"context_id": context["id"], "question": "Count orders"}
        )
        assert result.is_error and payload(result)["error"] == "generation_timeout"
        assert cancelled.is_set()


@pytest.mark.parametrize("version", ["2025-03-26", "2025-06-18", "2025-11-25"])
async def test_legacy_streamable_http_handshake(mcp_app, version):
    async with (
        mcp_app.router.lifespan_context(mcp_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mcp_app),
            base_url="http://localhost",
            headers={
                "Authorization": "Bearer mcp-secret",
                "Accept": "application/json, text/event-stream",
            },
        ) as http,
    ):
        response = await http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": version,
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["protocolVersion"] == version
        assert "mcp-session-id" not in response.headers
        response = await http.post(
            "/mcp",
            headers={"MCP-Protocol-Version": version},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert len(response.json()["result"]["tools"]) == 4


async def test_body_limit(mcp_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app),
        base_url="http://localhost",
        headers={"Authorization": "Bearer mcp-secret"},
    ) as http:
        response = await http.post("/mcp", content=b"x" * (1024 * 1024 + 1))
        assert response.status_code == 413


async def test_discovery_pagination_and_invalid_stale_tools(mcp_app, seed, store):
    context, tool, rest = seed
    for name in ("second", "third"):
        response = await rest.post("/api/v1/contexts", json={"name": name, "dialect": "postgresql"})
        assert response.status_code == 201
    async with connect(mcp_app) as session:
        ids, offset = [], 0
        while offset is not None:
            page = payload(await session.call_tool("list_contexts", {"limit": 1, "offset": offset}))
            ids.extend(c["id"] for c in page["items"])
            offset = page["next_offset"]
        assert len(set(ids)) == 3
        record = store.tools[tool["name"]]
        record.context_version = 0
        assert (
            payload(await session.call_tool("list_tools", {"context_id": context["id"]}))["items"]
            == []
        )
        record.context_version = context["version"]
        record.status = "invalid"
        assert (
            payload(await session.call_tool("list_tools", {"context_id": context["id"]}))["items"]
            == []
        )
        unknown = await session.call_tool("delete_context", {})
        assert unknown.is_error and payload(unknown)["error"] == "tool_not_found"


@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_discovery_does_not_prevent_persisting_failed_revalidation(
    mcp_app, seed, store, transport
):
    from uuid import UUID

    from sidewit.store import ToolRecord

    context, _, rest = seed
    # Simulate a file accepted by an earlier release, before function restrictions.
    legacy = ToolRecord(
        context_id=UUID(context["id"]),
        name="legacy_sleep",
        description="Previously accepted function",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        operation={"type": "sql", "statement": "SELECT pg_sleep(1)"},
        context_version=context["version"],
        status="valid",
    )
    store.save_tool(legacy)
    store.restore()
    path = f"/api/v1/tools/{legacy.id}"
    async with connect(mcp_app) as session:
        for _ in range(2):
            listed = payload(await session.call_tool("list_tools", {"context_id": context["id"]}))
            assert str(legacy.id) not in {item["id"] for item in listed["items"]}
        # Discovery is read-only; invoking must still persist the new validation verdict.
        assert (await rest.get(path)).json()["status"] == "valid"
        if transport == "rest":
            response = await rest.post(path + "/invoke", json={"arguments": {}})
            assert response.status_code == 409
            assert response.json()["error"] == "tool_invalid"
        else:
            result = await session.call_tool(
                "invoke_tool", {"tool_id": str(legacy.id), "arguments": {}}
            )
            assert result.is_error and payload(result)["error"] == "tool_invalid"
        updated = (await rest.get(path)).json()
        assert updated["status"] == "invalid"
        assert updated["validation"]["valid"] is False
        assert updated["validation_errors"]
        store.restore()
        assert (await rest.get(path)).json()["status"] == "invalid"


async def test_integer_parameter_precision(mcp_app, seed):
    context, _, rest = seed
    response = await rest.post(
        "/api/v1/tools",
        json={
            "context_id": context["id"],
            "name": "find_id",
            "description": "Find a customer by ID",
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
                "additionalProperties": False,
            },
            "operation": {"type": "sql", "statement": "SELECT id FROM customers WHERE id = :id"},
        },
    )
    assert response.status_code == 201, response.text
    tool = response.json()
    async with connect(mcp_app) as session:
        result = await session.call_tool(
            "invoke_tool", {"tool_id": tool["id"], "arguments": {"id": 9007199254740993}}
        )
        assert not result.is_error
        assert payload(result)["operation"]["parameters"]["id"] == 9007199254740993


async def test_storage_failure_recovers_without_leaking_details(
    mcp_app, seed, store, monkeypatch, caplog
):
    from sidewit.store import StoreError
    from tests.test_tools_api import ARGS

    _, tool, rest = seed
    record = store.tools[tool["name"]]
    record.context_version = 0  # Invocation must persist revalidation.
    store._tool_checks.clear()

    def fail(*args):
        raise StoreError("PRIVATE_FILE_CONTENT", operation="write")

    monkeypatch.setattr(store, "save_tool", fail)
    async with connect(mcp_app) as session:
        result = await session.call_tool("invoke_tool", {"tool_id": tool["id"], "arguments": ARGS})
        assert result.is_error and payload(result)["error"] == "storage_unavailable"
        assert "PRIVATE_FILE_CONTENT" not in result.content[0].text + caplog.text
        assert (await rest.get("/api/v1/health")).status_code == 200


async def test_mixed_tool_and_generated_operations(mcp_app, seed):
    from sidewit.generation.providers.fake import FakeProvider
    from tests.test_generate_api import ok
    from tests.test_mixed_plan import mixed

    context, tool, _ = seed
    provider = FakeProvider(
        [
            mixed(
                ["List customer IDs"],
                calls=[
                    {
                        "tool_name": "regional_revenue",
                        "parameters": [
                            {"name": "region", "type": "string", "value": "서울"},
                            {
                                "name": "start_at",
                                "type": "date-time",
                                "value": "2026-09-01T00:00:00+09:00",
                            },
                        ],
                    }
                ],
            ),
            ok("SELECT id FROM customers", []),
        ]
    )
    mcp_app.dependency_overrides[get_llm_provider] = lambda: provider
    async with connect(mcp_app) as session:
        result = await session.call_tool(
            "generate_operations",
            {"context_id": context["id"], "question": "서울 매출과 고객 ID 목록"},
        )
        assert not result.is_error, payload(result)
        steps = payload(result)["operations"]
        assert [s["source"] for s in steps] == ["tool", "generated_sql"]
        assert steps[0]["selected_tool"]["id"] == tool["id"]


async def test_current_sdk_discovery_and_call(mcp_app):
    from mcp import Client

    async with (
        mcp_app.router.lifespan_context(mcp_app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=mcp_app),
            headers={"Authorization": "Bearer mcp-secret"},
        ) as http,
        Client(streamable_http_client("http://localhost/mcp", http_client=http)) as client,
    ):
        assert len((await client.list_tools()).tools) == 4
        result = await client.call_tool("list_contexts")
        assert not result.is_error and payload(result)["items"] == []
