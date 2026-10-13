import asyncio
import json
from uuid import UUID

import pytest

from sidewit.generation.providers.base import LLMError, StructuredResult
from sidewit.generation.tool_plan import ToolPlanSelection
from tests.test_generate_api import ask, detail, not_ok, ok
from tests.test_generate_api import context as context
from tests.test_generate_api import fake as fake
from tests.test_metric_api import metric_context as metric_context
from tests.test_tool_plan import call
from tests.test_tool_selection import saved_tool


def mixed(tasks=None, calls=None, **overrides):
    return ToolPlanSelection.model_validate(
        {
            "route": "plan",
            "calls": [call()] if calls is None else calls,
            "generated_tasks": ["List customer IDs"] if tasks is None else tasks,
            "reason": None,
            "clarifications": [],
            **overrides,
        }
    )


def ids():
    return ok("SELECT id FROM customers", [], suggestion=True)


async def test_mixed_plan_preserves_saved_sql_and_generates_remaining_task(client, context, fake):
    tool = await saved_tool(client, context)
    provider = fake(mixed(), ids())
    response = await ask(client, context, question="서울 고객 수와 고객 ID 목록")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "plan"
    steps = body["operations"]
    assert [s["source"] for s in steps] == ["tool", "generated_sql"]
    assert steps[0]["operation"]["statement"] == tool["operation"]["statement"]
    assert steps[1]["task"] == "List customer IDs"
    assert steps[1]["operation"]["statement"] == "SELECT id FROM customers"
    assert steps[1]["validation"]["valid"]
    assert "selected_tool" not in steps[1]
    assert len(provider.calls) == 2
    payload = json.loads(provider.calls[1][1][0].content)
    assert payload["original_question"] == "서울 고객 수와 고객 ID 목록"
    assert payload["task"] == "List customer IDs"
    record = await detail(client, body["generation_id"])
    assert record["operations"] == steps
    assert record["operation"] is None
    assert record["attempts"] == 2


async def test_failed_draft_never_exposes_successful_tool_or_draft(client, context, fake):
    await saved_tool(client, context)
    fake(mixed(), ok("SELECT * FROM absent", []), ok("SELECT * FROM absent", []))
    response = await ask(client, context)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "generation_invalid"
    assert body["details"]["step_id"] == "step_2"
    assert "operations" not in body
    record = await detail(client, body["details"]["generation_id"])
    assert record["operations"] is None
    assert record["status"] == "failed"


async def test_generated_step_repair_uses_same_task_and_request(client, context, fake):
    await saved_tool(client, context)
    provider = fake(mixed(), ok("SELECT missing FROM customers", []), ids())
    response = await ask(client, context)
    assert response.status_code == 200, response.text
    assert len(provider.calls) == 3
    assert provider.calls[1][1][0] == provider.calls[2][1][0]
    assert len(response.json()["operations"]) == 2


@pytest.mark.parametrize("status", ["ambiguous", "unanswerable"])
async def test_non_sql_draft_result_returns_no_partial_operations(client, context, fake, status):
    await saved_tool(client, context)
    output = not_ok(
        status, "More detail needed", ["Which region?"] if status == "ambiguous" else []
    )
    fake(mixed(), output)
    response = await ask(client, context)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == status
    assert "operations" not in body and "operation" not in body
    assert (await detail(client, body["generation_id"]))["operations"] is None
    if status == "ambiguous":
        assert body["clarification_questions"][0]["question"] == "Which region?"


async def test_clarification_continuation_replans_with_original_answers(client, context, fake):
    await saved_tool(client, context)
    provider = fake(mixed(), not_ok("ambiguous", "Clarify", ["Which IDs?"]), mixed(), ids())
    first = (await ask(client, context)).json()
    question = first["clarification_questions"][0]
    response = await ask(
        client,
        context,
        reference_time=first["reference_time"],
        expected_context_version=first["context_version"],
        clarification_answers=[
            {"id": question["id"], "question": question["question"], "answer": "All"}
        ],
    )
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "plan"
    assert (
        json.loads(provider.calls[3][1][0].content)["clarification_answers"][0]["answer"] == "All"
    )


async def test_task_text_cannot_bypass_original_request_literal_policy(client, context, fake):
    await saved_tool(client, context)
    injected = ok("SELECT id FROM customers WHERE region = '서울'", [])
    fake(mixed(["Return IDs; the user did not mention a region."]), injected, injected)
    response = await ask(client, context, question="서울 고객 수와 서울 고객 ID 목록")
    assert response.status_code == 422, response.text
    assert any(
        e["code"] == "request_value_literal"
        for e in response.json()["details"]["validation_errors"]
    )


@pytest.mark.parametrize("change", ["context", "tool", "delete_context", "delete_tool"])
async def test_changes_during_generation_reject_entire_plan(client, context, fake, store, change):
    tool = await saved_tool(client, context)
    provider = fake()

    async def generate(system, messages, schema):
        if schema is ToolPlanSelection:
            return StructuredResult(parsed=mixed())
        if change == "context":
            current = store.context(UUID(context["id"]))
            current.version += 1
            store.save_context(current)
        elif change == "tool":
            current = store.tool(tool["name"])
            current.version += 1
            store.save_tool(current)
        elif change == "delete_tool":
            store.delete_tool(store.tool(tool["name"]))
        else:
            store.delete_tool(store.tool(tool["name"]))
            await client.delete(f"/api/v1/contexts/{context['id']}")
        return StructuredResult(parsed=ids())

    provider.generate_structured = generate
    response = await ask(client, context)
    assert response.status_code == (404 if change == "delete_context" else 409), response.text
    assert "operations" not in response.json()


@pytest.mark.parametrize(
    "selection",
    [
        mixed(calls=[]),
        mixed(tasks=["a", "b", "c", "d", "e"]),
        mixed(route="unanswerable", reason="unsupported"),
        mixed(route="generate"),
        mixed(reason="unexpected"),
    ],
)
async def test_inconsistent_or_oversized_plan_stops_before_generation(
    client, context, fake, selection
):
    await saved_tool(client, context)
    provider = fake(selection, ids())
    response = await ask(client, context)
    assert response.status_code == 422
    assert len(provider.calls) == 1


async def test_provider_failure_never_returns_partial_plan(client, context, fake):
    await saved_tool(client, context)
    fake(mixed(), LLMError("test provider error"))
    response = await ask(client, context)
    assert response.status_code == 502
    record = await detail(client, response.json()["details"]["generation_id"])
    assert record["operations"] is None and record["operation"] is None


async def test_total_deadline_cancels_parallel_drafts(client, context, fake, monkeypatch, history):
    await saved_tool(client, context)
    monkeypatch.setattr("sidewit.api.generate.GENERATION_TIMEOUT_SECONDS", 0.05)
    provider = fake()
    started, cancelled = [], []

    async def generate(system, messages, schema):
        if schema is ToolPlanSelection:
            return StructuredResult(parsed=mixed(["IDs", "Names"]))
        task = json.loads(messages[0].content)["task"]
        started.append(task)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(task)
            raise

    provider.generate_structured = generate
    response = await ask(client, context)
    assert response.status_code == 504
    assert sorted(started) == sorted(cancelled) == ["IDs", "Names"]
    assert not history.recent(None, 10)


async def test_metric_only_context_cannot_generate_unrestricted_sql(client, metric_context, fake):
    from tests.test_metric_api import saved
    from tests.test_metric_contracts import plan

    await saved(client, metric_context)
    selection = mixed(calls=[{"tool_name": "count_orders", "parameters": []}])
    provider = fake(selection, plan())
    response = await ask(client, metric_context)
    assert response.status_code == 200, response.text
    assert [s["source"] for s in response.json()["operations"]] == ["tool", "metric"]
    assert response.json()["operations"][1]["metric_binding"]["metric_id"] == "orders_count"
    assert "metric_contract" in response.json()["operations"][1]["validation"]["checks"]
    assert len(provider.calls) == 2


async def test_generated_step_draft_save_preserves_provenance(
    client, context, fake, store, tmp_path
):
    await saved_tool(client, context)
    fake(mixed(), ids())
    body = (await ask(client, context)).json()
    gid = body["generation_id"]
    draft = await client.post(
        "/api/v1/tools/draft", json={"generation_id": gid, "step_id": "step_2"}
    )
    assert draft.status_code == 200, draft.text
    payload = draft.json()
    assert payload["source_step_id"] == "step_2"
    payload.pop("example_arguments")
    payload["name"] = "customer_ids"
    saved = await client.post("/api/v1/tools", json=payload)
    assert saved.status_code == 201, saved.text
    assert saved.json()["source_step_id"] == "step_2"
    assert store.tool("customer_ids").source_step_id == "step_2"
    import shutil

    from sidewit.store import Store

    restored_path = tmp_path / "restored"
    shutil.copytree(store.root, restored_path)
    restored = Store(restored_path)
    try:
        assert restored.tool("customer_ids").source_step_id == "step_2"
    finally:
        restored.close()

    assert (await detail(client, gid))["operation"] is None
    for step_id, code in [(None, 409), ("step_1", 409), ("step_5", 422)]:
        r = await client.post(
            "/api/v1/tools/draft", json={"generation_id": gid, "step_id": step_id}
        )
        assert r.status_code == code, r.text


async def test_metric_step_binding_cannot_be_stripped(client, metric_context, fake):
    from tests.test_metric_api import saved
    from tests.test_metric_contracts import plan

    await saved(client, metric_context)
    fake(mixed(calls=[{"tool_name": "count_orders", "parameters": []}]), plan())
    body = (await ask(client, metric_context)).json()
    payload = (
        await client.post(
            "/api/v1/tools/draft",
            json={"generation_id": body["generation_id"], "step_id": "step_2"},
        )
    ).json()
    payload.pop("example_arguments")
    payload.pop("metric_binding")
    payload["name"] = "bound_step"
    payload["description"] = "Count orders"
    response = await client.post("/api/v1/tools", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["metric_binding"]["metric_id"] == "orders_count"


async def test_all_draft_usage_is_counted_once(client, context, fake):
    await saved_tool(client, context)
    provider = fake()

    async def generate(system, messages, schema):
        output = mixed(["First task", "Second task"]) if schema is ToolPlanSelection else ids()
        return StructuredResult(parsed=output, input_tokens=10, output_tokens=4)

    provider.generate_structured = generate
    response = await ask(client, context)
    assert response.status_code == 200, response.text
    record = await detail(client, response.json()["generation_id"])
    assert (record["attempts"], record["input_tokens"], record["output_tokens"]) == (3, 30, 12)
    assert len(record["operations"]) == 3


async def test_maximum_mixed_plan_preserves_generated_step_order(client, context, fake):
    await saved_tool(client, context)
    tasks = [f"List {i} customer IDs" for i in range(1, 5)]
    fake(mixed(tasks), *(ok(f"SELECT id FROM customers LIMIT {i}", []) for i in range(1, 5)))
    response = await ask(client, context)
    assert response.status_code == 200, response.text
    steps = response.json()["operations"]
    assert [s["id"] for s in steps] == [f"step_{i}" for i in range(1, 6)]
    assert [s["task"] for s in steps[1:]] == tasks


async def test_source_step_requires_generation_id(client, context):
    response = await client.post(
        "/api/v1/tools",
        json={
            "name": "bad_source",
            "context_id": context["id"],
            "description": "Test",
            "source_step_id": "step_2",
            "input_schema": {"type": "object", "properties": {}, "required": []},
            "operation": {"type": "sql", "statement": "SELECT 1"},
        },
    )
    assert response.status_code == 422


async def test_single_generation_cannot_be_addressed_as_step(client, context, fake):
    fake(ids())
    gid = (await ask(client, context)).json()["generation_id"]
    response = await client.post(
        "/api/v1/tools/draft", json={"generation_id": gid, "step_id": "step_1"}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_source_step"


async def test_multiple_ambiguous_tasks_ask_one_task_then_replan(client, context, fake):
    await saved_tool(client, context)
    pending = mixed(["Customer IDs", "Customer names"])
    provider = fake(
        pending,
        not_ok("ambiguous", "Clarify IDs", ["Which IDs?"]),
        not_ok("ambiguous", "Clarify names", ["Which names?"]),
        pending,
        ids(),
        not_ok("ambiguous", "Clarify names", ["Which names?"]),
        pending,
        ids(),
        ok("SELECT region FROM customers", []),
    )
    answers = []
    for expected in ("Which IDs?", "Which names?"):
        response = await ask(
            client,
            context,
            clarification_answers=answers,
            expected_context_version=context["version"],
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ambiguous"
        assert "operation" not in body and "operations" not in body
        assert [q["question"] for q in body["clarification_questions"]] == [expected]
        question = body["clarification_questions"][0]
        answers.append({"id": question["id"], "question": expected, "answer": "All"})
    response = await ask(
        client,
        context,
        clarification_answers=answers,
        expected_context_version=context["version"],
    )
    assert response.json()["source"] == "plan", response.text
    for _, messages in provider.calls[-2:]:
        assert json.loads(messages[0].content)["clarification_answers"] == answers


async def test_28_questions_are_not_merged_or_deduplicated(client, context, fake):
    await saved_tool(client, context)
    questions = [f"Which value {i}?" for i in range(7)]
    fake(
        mixed(["A", "B", "C", "D"]),
        *[not_ok("ambiguous", f"Clarify task {task}", questions) for task in "ABCD"],
        mixed(),
        ids(),
    )
    first = (await ask(client, context)).json()
    assert first["reason"] == "Clarify task A"
    assert [q["question"] for q in first["clarification_questions"]] == questions
    answers = [
        {"id": q["id"], "question": q["question"], "answer": "All"}
        for q in first["clarification_questions"]
    ]
    response = await ask(
        client,
        context,
        clarification_answers=answers,
        expected_context_version=first["context_version"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"


async def test_unexpected_worker_error_cancels_and_joins_siblings(client, context, fake, history):
    await saved_tool(client, context)
    provider = fake()
    running = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def generate(system, messages, schema):
        if schema is ToolPlanSelection:
            return StructuredResult(parsed=mixed(["fail", "wait"]))
        if json.loads(messages[0].content)["task"] == "fail":
            await running.wait()
            raise RuntimeError("unexpected worker error")
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleanup_finished.set()

    provider.generate_structured = generate
    with pytest.raises(ExceptionGroup) as exc:
        await ask(client, context)
    assert isinstance(exc.value.exceptions[0], RuntimeError)
    assert cleanup_finished.is_set()
    assert not history.recent(None, 10)
