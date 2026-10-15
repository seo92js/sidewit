import pytest

from sidewit.domain.context import ContextDefinition
from sidewit.validation.input_schema import input_schema_for
from sidewit.validation.sql_validator import validate_sql_operation
from tests.test_validation_boundaries import DEFINITION, create_context, create_tool
from tests.tool_urls import tool_path


@pytest.mark.parametrize(
    "statement",
    [
        "WITH q AS (SELECT status FROM orders) SELECT status FROM q WHERE status = :status",
        "SELECT q.status FROM (SELECT status FROM orders) q WHERE q.status = :status",
    ],
)
async def test_cte_and_subquery_preserve_context_value_constraints(client, statement):
    ctx = await create_context(client, DEFINITION)
    await create_tool(client, ctx, statement, input_schema_for({"status": "string"}))
    response = await client.post(
        tool_path("review_tool", "/invoke"), json={"arguments": {"status": "OUTSIDE"}}
    )
    assert response.status_code == 422, response.text


async def test_duplicate_alias_is_validation_failure_not_server_error(client):
    ctx = await create_context(client, DEFINITION)
    client._transport.raise_app_exceptions = False
    response = await client.post(
        "/api/v1/validate",
        json={
            "context_id": ctx["id"],
            "operation": {
                "type": "sql",
                "statement": "SELECT a.status FROM orders a JOIN orders a ON a.status = a.status",
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["valid"] is False


async def test_unsupported_ddl_identifier_is_client_error(client):
    ctx = await create_context(client, DEFINITION)
    client._transport.raise_app_exceptions = False
    response = await client.post(
        "/api/v1/contexts/" + ctx["id"] + "/import-ddl",
        json={"ddl": 'CREATE TABLE "Order Items" ("Unit Price" numeric);'},
    )
    assert response.status_code == 422, response.text


def test_derived_star_does_not_hide_unknown_column():
    result = validate_sql_operation(
        "SELECT q.missing FROM (SELECT * FROM orders) q",
        "postgresql",
        ContextDefinition.model_validate(DEFINITION),
        {},
    )
    assert not result.valid, result


@pytest.mark.parametrize(
    "source",
    [
        "orders",
        "(SELECT status FROM orders) q",
        "(SELECT * FROM orders) q",
        "(SELECT x.status FROM (SELECT * FROM orders) x) q",
        "(SELECT status FROM orders UNION ALL SELECT status FROM orders) q",
    ],
)
def test_equivalent_projections_preserve_value_domain(source):
    from sidewit.validation.sql_validator import allowed_values_by_parameter

    context = ContextDefinition.model_validate(DEFINITION)
    sql = f"SELECT status FROM {source} WHERE status = :status"
    assert allowed_values_by_parameter(sql, "postgresql", context) == {"status": ["A", "B"]}
    assert validate_sql_operation(
        sql, "postgresql", context, {"status": "string"}, {"status": "A"}
    ).valid
    assert not validate_sql_operation(
        sql, "postgresql", context, {"status": "string"}, {"status": "OUTSIDE"}
    ).valid


def test_computed_output_does_not_inherit_value_domain():
    context = ContextDefinition.model_validate(DEFINITION)
    sql = "SELECT n FROM (SELECT COUNT(status) AS n FROM orders) q WHERE n = :n"
    assert validate_sql_operation(sql, "postgresql", context, {"n": "integer"}, {"n": 42}).valid


def test_heterogeneous_union_column_has_no_value_domain():
    """Valid PostgreSQL; the column mixes orders.status with a computed value, so no single
    field's allowed values apply to it."""
    context = ContextDefinition.model_validate(DEFINITION)
    sql = "SELECT status FROM (SELECT status FROM orders UNION ALL SELECT 'C') q WHERE status = :s"
    result = validate_sql_operation(sql, "postgresql", context, {"s": "string"}, {"s": "C"})
    assert result.valid, result.errors


def test_ambiguous_derived_output_is_rejected():
    context = ContextDefinition.model_validate(DEFINITION)
    result = validate_sql_operation(
        "SELECT q.status FROM (SELECT status, status FROM orders) q", "postgresql", context, {}
    )
    assert not result.valid


async def test_duplicate_alias_enters_generate_repair(client):
    from sidewit.application.runtime import get_llm_provider
    from sidewit.generation.providers.fake import FakeProvider
    from sidewit.main import app
    from tests.test_generate_api import ok

    ctx = await create_context(client, DEFINITION)
    provider = FakeProvider(
        [
            ok("SELECT a.status FROM orders a JOIN orders a ON a.status = a.status", []),
            ok("SELECT status FROM orders", []),
        ]
    )
    app.dependency_overrides[get_llm_provider] = lambda: provider
    response = await client.post(
        "/api/v1/generate",
        json={
            "context_id": ctx["id"],
            "question": "List statuses",
            "reference_time": "2026-10-08T00:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    assert len(provider.calls) == 2


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders CROSS JOIN LATERAL unnest(tags) AS t",
        "SELECT o.id, t FROM orders o CROSS JOIN LATERAL unnest(o.tags) AS t",
        "SELECT t.x FROM unnest(ARRAY[1, 2]) AS t(x)",
        "SELECT id, x FROM orders, jsonb_array_elements('[]'::jsonb) AS x",
        "SELECT e.key FROM orders, jsonb_each('{}'::jsonb) AS e",
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
        "SELECT n FROM r",
    ],
)
def test_table_functions_and_recursive_ctes_are_valid(sql):
    context = ContextDefinition.model_validate(
        {
            "resources": [
                {
                    "name": "orders",
                    "fields": [
                        {"name": "id", "type": "bigint"},
                        {"name": "tags", "type": "text[]"},
                    ],
                }
            ]
        }
    )
    result = validate_sql_operation(sql, "postgresql", context, {})
    assert result.valid, result.errors


def test_column_list_of_a_table_function_is_checked():
    context = ContextDefinition.model_validate(DEFINITION)
    result = validate_sql_operation(
        "SELECT t.y FROM unnest(ARRAY[1]) AS t(x)", "postgresql", context, {}
    )
    assert [e.code for e in result.errors] == ["unknown_field"]


def test_duplicate_output_column_message_names_the_source_once():
    context = ContextDefinition.model_validate(DEFINITION)
    result = validate_sql_operation(
        "SELECT status FROM (SELECT status, status FROM orders) q", "postgresql", context, {}
    )
    assert [(e.code, e.message) for e in result.errors] == [
        ("ambiguous_field", "'q' has more than one 'status'.")
    ]


ACTIVE_ONLY = ContextDefinition.model_validate(
    {
        "resources": [
            {
                "name": "orders",
                "fields": [
                    {"name": "id", "type": "bigint"},
                    {"name": "status", "type": "text", "allowed_values": ["ACTIVE"]},
                ],
            }
        ]
    }
)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders WHERE status::text = :s",
        "SELECT s FROM (SELECT status::text AS s FROM orders) q WHERE s = :s",
        "WITH q AS (SELECT CAST(status AS varchar) AS s FROM orders) SELECT s FROM q WHERE s = :s",
    ],
)
def test_text_cast_keeps_the_value_domain_directly_and_through_lineage(sql):
    """Comparison and lineage follow the same value-preserving cast rule."""
    result = validate_sql_operation(sql, "postgresql", ACTIVE_ONLY, {"s": "string"}, {"s": "X"})
    assert [e.code for e in result.errors] == ["value_not_allowed"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders WHERE status::varchar(1) = 'A'",
        "SELECT id FROM orders WHERE status::char = 'A'",
        "SELECT s FROM (SELECT status::varchar(1) AS s FROM orders) q WHERE s = 'A'",
        "SELECT id FROM orders WHERE lower(status) = 'active'",
    ],
)
def test_value_changing_casts_and_functions_drop_the_value_domain(sql):
    result = validate_sql_operation(sql, "postgresql", ACTIVE_ONLY, {})
    assert result.valid, result.errors


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 UNION ALL SELECT 1, 2",
        "SELECT id FROM orders UNION SELECT id, status FROM orders",
        "SELECT id FROM (SELECT 1 AS id UNION ALL SELECT 1, 2) q",
    ],
)
def test_set_operation_arms_of_different_width_are_rejected(sql):
    result = validate_sql_operation(sql, "postgresql", ACTIVE_ONLY, {})
    assert [e.code for e in result.errors] == ["sql_analysis_error", "value_unverifiable"]


async def test_tool_invoke_rejects_value_outside_domain_through_text_cast_subquery(client):
    ctx = await create_context(client, ACTIVE_ONLY.to_json())
    sql = "SELECT s FROM (SELECT status::text AS s FROM orders) q WHERE s = :s"
    await create_tool(client, ctx, sql, input_schema_for({"s": "string"}))
    bad = await client.post(tool_path("review_tool", "/invoke"), json={"arguments": {"s": "X"}})
    assert bad.status_code == 422, bad.text
    assert bad.json()["details"]["errors"][0]["code"] == "value_not_allowed"
    ok = await client.post(tool_path("review_tool", "/invoke"), json={"arguments": {"s": "ACTIVE"}})
    assert ok.status_code == 200, ok.text


async def test_tool_invoke_enforces_context_values_through_all(client):
    ctx = await create_context(client, ACTIVE_ONLY.to_json())
    sql = "SELECT status FROM orders WHERE status != ALL(ARRAY[:s])"
    await create_tool(client, ctx, sql, input_schema_for({"s": "string"}))
    bad = await client.post(tool_path("review_tool", "/invoke"), json={"arguments": {"s": "X"}})
    assert bad.status_code == 422, bad.text
    assert bad.json()["details"]["errors"][0]["code"] == "value_not_allowed"
