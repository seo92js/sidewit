"""Regression coverage for API trust boundaries and PostgreSQL value/identifier semantics."""

import copy

import pytest

from sidewit.ddl.importer import import_ddl
from sidewit.domain.context import ContextDefinition
from sidewit.validation.input_schema import input_schema_for
from sidewit.validation.metric_contract import metric_template
from sidewit.validation.sql_validator import validate_sql_operation


async def create_context(client, definition):
    response = await client.post(
        "/api/v1/contexts",
        json={"name": "review", "dialect": "postgresql", "definition": definition},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def update_context(client, context, definition):
    response = await client.put(
        "/api/v1/contexts/" + context["id"],
        json={
            **{k: context[k] for k in ["name", "dialect", "timezone", "allow_generated_sql"]},
            "expected_version": context["version"],
            "definition": definition,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def create_tool(client, context, sql, schema, binding=None):
    response = await client.post(
        "/api/v1/tools",
        json={
            "context_id": context["id"],
            "name": "review_tool",
            "description": "review",
            "operation": {"type": "sql", "statement": sql},
            "input_schema": schema,
            "metric_binding": binding,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


DEFINITION = {
    "resources": [
        {
            "name": "orders",
            "fields": [{"name": "status", "type": "text", "allowed_values": ["A", "B"]}],
        }
    ]
}


async def test_manual_tool_cannot_invoke_value_outside_context(client):
    context = await create_context(client, DEFINITION)
    await create_tool(
        client,
        context,
        "SELECT status FROM orders WHERE status = :status",
        input_schema_for({"status": "string"}),
    )
    response = await client.post(
        "/api/v1/tools/review_tool/invoke", json={"arguments": {"status": "OUTSIDE"}}
    )
    assert response.status_code == 422, (
        f"forbidden value returned: {response.status_code} {response.text}"
    )


async def test_context_shrink_invalidates_old_enum(client):
    context = await create_context(client, DEFINITION)
    schema = input_schema_for({"status": "string"})
    schema["properties"]["status"]["enum"] = ["A", "B"]
    await create_tool(client, context, "SELECT status FROM orders WHERE status = :status", schema)
    definition = copy.deepcopy(DEFINITION)
    definition["resources"][0]["fields"][0]["allowed_values"] = ["A"]
    updated = await update_context(client, context, definition)
    response = await client.post(
        "/api/v1/tools/review_tool/invoke", json={"arguments": {"status": "B"}}
    )
    assert updated["revalidation"]["invalid"][0]["errors"][0]["code"] == "input_enum_conflict"
    assert response.status_code == 409


async def test_refresh_removed_bound_time_field_returns_conflict(client):
    definition = {
        "resources": [{"name": "orders", "fields": [{"name": "created_at", "type": "date"}]}],
        "metrics": [
            {
                "id": "count_orders",
                "description": "count orders",
                "resource": "orders",
                "aggregate": {"kind": "row_count"},
                "time_fields": ["orders.created_at"],
                "default_time_field": "orders.created_at",
            }
        ],
    }
    context = await create_context(client, definition)
    parsed = ContextDefinition.model_validate(definition)
    sql, types = metric_template(parsed, parsed.metrics[0], "orders.created_at", "postgresql")
    await create_tool(
        client,
        context,
        sql,
        input_schema_for(types),
        {"metric_id": "count_orders", "time_field": "orders.created_at"},
    )
    definition["metrics"][0]["time_fields"] = []
    definition["metrics"][0]["default_time_field"] = None
    await update_context(client, context, definition)
    response = await client.post(
        "/api/v1/tools/review_tool/refresh-contract", json={"expected_version": 1}
    )
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("sql", ["SELECT id FROM orders", 'SELECT "ID" FROM "Orders"'])
def test_wrong_case_identifiers_are_rejected(sql):
    definition = import_ddl(
        ContextDefinition(), 'CREATE TABLE "Orders" (id bigint);', "postgresql", "replace"
    ).definition
    result = validate_sql_operation(sql, "postgresql", definition, {})
    assert not result.valid, f"Nonexistent PostgreSQL object accepted: {sql}"


def test_ddl_bigint_value_is_preserved_exactly():
    definition = import_ddl(
        ContextDefinition(),
        "CREATE TABLE t (code bigint CHECK (code IN (9007199254740993)));",
        "postgresql",
        "replace",
    ).definition
    assert definition.resources[0].fields[0].allowed_values == [9007199254740993]


@pytest.mark.parametrize(
    "properties",
    [
        [{"type": "string"}],
        {"status": {"type": ["string", "null"]}},
        {"status": {"type": {}}},
        {"status": {"type": "string", "format": []}},
        {"status": {"type": "string", "enum": [["A"]]}},
    ],
)
async def test_malformed_input_schema_is_422_not_500(client, properties):
    context = await create_context(client, DEFINITION)
    response = await client.post(
        "/api/v1/validate",
        json={
            "context_id": context["id"],
            "operation": {
                "type": "sql",
                "statement": "SELECT status FROM orders WHERE status = :status",
            },
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": ["status"],
                "additionalProperties": False,
            },
        },
    )
    assert response.status_code == 200, response.text
    assert not response.json()["valid"]
    assert response.json()["errors"][0]["code"] == "invalid_input_schema"


def test_adjacent_large_integer_is_not_allowed():
    definition = ContextDefinition.model_validate(
        {
            "resources": [
                {
                    "name": "t",
                    "fields": [
                        {"name": "code", "type": "bigint", "allowed_values": [9007199254740993]}
                    ],
                }
            ]
        }
    )
    result = validate_sql_operation(
        "SELECT code FROM t WHERE code = 9007199254740992", "postgresql", definition, {}
    )
    assert not result.valid, "Adjacent integer outside allowed_values was accepted"


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT id FROM "Orders"',
        'SELECT o.ID FROM "Orders" O',
        'SELECT "O".id FROM "Orders" AS "O"',
        'WITH "C"("X") AS (SELECT id FROM "Orders") SELECT "X" FROM "C"',
        'SELECT q."X" FROM (SELECT id AS "X" FROM "Orders") q',
        'SELECT id AS "X" FROM "Orders" ORDER BY "X"',
    ],
)
def test_quoted_and_unquoted_identifiers_follow_postgres_rules(sql):
    definition = import_ddl(
        ContextDefinition(), 'CREATE TABLE "Orders" (id bigint);', "postgresql", "replace"
    ).definition
    result = validate_sql_operation(sql, "postgresql", definition, {})
    assert result.valid, result.errors


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT o.id FROM "Orders" AS "O"',
        'WITH "C"("X") AS (SELECT id FROM "Orders") SELECT x FROM "C"',
        'SELECT q.x FROM (SELECT id AS "X" FROM "Orders") q',
        'SELECT id AS "X" FROM "Orders" ORDER BY x',
    ],
)
def test_wrong_case_aliases_are_rejected(sql):
    definition = import_ddl(
        ContextDefinition(), 'CREATE TABLE "Orders" (id bigint);', "postgresql", "replace"
    ).definition
    assert not validate_sql_operation(sql, "postgresql", definition, {}).valid


@pytest.mark.parametrize(
    ("field_type", "param_type", "allowed", "good", "bad"),
    [
        ("bigint", "integer", [9007199254740993], 9007199254740993, 9007199254740992),
        ("boolean", "boolean", [True], True, False),
        ("text", "string", ["001", "true"], "001", "1"),
    ],
)
async def test_context_values_enforced_without_tool_enum(
    client, field_type, param_type, allowed, good, bad
):
    definition = {
        "resources": [
            {"name": "t", "fields": [{"name": "v", "type": field_type, "allowed_values": allowed}]}
        ]
    }
    context = await create_context(client, definition)
    await create_tool(
        client, context, "SELECT v FROM t WHERE v = :v", input_schema_for({"v": param_type})
    )
    for _ in range(2):
        response = await client.post(
            "/api/v1/tools/review_tool/invoke", json={"arguments": {"v": good}}
        )
        assert response.status_code == 200, response.text
        assert response.json()["operation"]["parameters"]["v"] == good
    response = await client.post("/api/v1/tools/review_tool/invoke", json={"arguments": {"v": bad}})
    assert response.status_code == 422
    assert response.json()["details"]["errors"][0]["code"] == "value_not_allowed"


async def test_shared_parameter_uses_intersection_of_field_values(client):
    from sidewit.validation.sql_validator import allowed_values_by_parameter

    definition = {
        "resources": [
            {
                "name": "t",
                "fields": [
                    {"name": "a", "type": "text", "allowed_values": ["A", "B"]},
                    {"name": "b", "type": "text", "allowed_values": ["B", "C"]},
                ],
            }
        ]
    }
    context = await create_context(client, definition)
    statement = "SELECT a FROM t WHERE a = :v AND b = :v"
    assert allowed_values_by_parameter(
        statement, "postgresql", ContextDefinition.model_validate(definition)
    ) == {"v": ["B"]}
    await create_tool(client, context, statement, input_schema_for({"v": "string"}))
    for value, code in [("A", 422), ("B", 200), ("C", 422)]:
        response = await client.post(
            "/api/v1/tools/review_tool/invoke", json={"arguments": {"v": value}}
        )
        assert response.status_code == code, response.text


@pytest.mark.parametrize("literal", ["9007199254740993", "9007199254740993e0"])
def test_integer_check_constraints_never_round(literal):
    definition = import_ddl(
        ContextDefinition(),
        f"CREATE TABLE t (v bigint CHECK (v IN ({literal})));",
        "postgresql",
        "replace",
    ).definition
    assert definition.resources[0].fields[0].allowed_values == [9007199254740993]


def test_unrepresentable_fractional_check_is_rejected_without_rounding():
    from sidewit.ddl.importer import DdlParseError

    with pytest.raises(DdlParseError, match="representable exactly"):
        import_ddl(
            ContextDefinition(),
            "CREATE TABLE t (v numeric CHECK (v IN (0.10000000000000001)));",
            "postgresql",
            "replace",
        )
