"""Enum names are shared by import, value policy and isolated PostgreSQL planning."""

import pytest
from sqlglot import exp

from sidewit.ddl.importer import import_ddl
from sidewit.domain.context import ContextDefinition
from sidewit.domain.dialect import user_type_identity
from sidewit.validation.input_schema import input_schema_for
from sidewit.validation.sql_validator import (
    allowed_values_by_parameter,
    validate_sql_operation,
)
from tests.tool_urls import tool_path


def definition(type_name="public.mood"):
    return import_ddl(
        ContextDefinition(),
        f"CREATE TYPE {type_name} AS ENUM ('ok', 'bad', 'meh'); "
        f"CREATE TABLE t (id bigint, m {type_name} CHECK (m IN ('ok', 'bad')))",
        "postgresql",
        "replace",
    ).definition


@pytest.mark.parametrize(
    "declared, expected",
    [
        ("mood", ("public", "mood")),
        ("PUBLIC.Mood", ("public", "mood")),
        ('"Public"."Mood"', ("Public", "Mood")),
        ("other.mood", ("other", "mood")),
        ("text", None),
        ("varchar(10)", None),
        ("integer", None),
        ("mood[]", None),
    ],
)
def test_type_identity_matches_field_and_cast(declared, expected):
    assert user_type_identity(declared) == expected
    assert (
        user_type_identity(exp.DataType.build(declared, dialect="postgres", udt=True)) == expected
    )


@pytest.mark.parametrize(
    "rhs",
    [
        "'ok'::mood",
        "'ok'::public.mood",
        "CAST('ok' AS PUBLIC.Mood)",
        "('ok'::text)::public.mood",
        "(SELECT 'ok'::public.mood)",
        "ANY(ARRAY['ok'::public.mood, 'bad'::mood])",
    ],
)
def test_same_enum_cast_is_a_known_value(rhs):
    result = validate_sql_operation(
        f"SELECT id FROM t WHERE m = {rhs}", "postgresql", definition(), {}
    )
    assert result.valid, result.errors


@pytest.mark.parametrize(
    "rhs, code",
    [
        ("'meh'::public.mood", "value_unverifiable"),
        ("('okay'::varchar(2))::mood", None),
        ("('meh'::mood)::text::mood", "value_unverifiable"),
        ("'okay'::mood::varchar(2)::mood", "value_unverifiable"),
        ("'ok'::other.mood", "value_unverifiable"),
        ("'ok'::unknown_type", "value_unverifiable"),
        ("'ok'::mood::char(1)::mood", "value_unverifiable"),
    ],
)
def test_enum_support_does_not_skip_value_changing_or_foreign_casts(rhs, code):
    result = validate_sql_operation(
        f"SELECT id FROM t WHERE m = {rhs}", "postgresql", definition(), {}
    )
    assert [e.code for e in result.errors] == ([] if code is None else [code])


@pytest.mark.parametrize("rhs", [":m::public.mood", "CAST(:m AS mood)", "(SELECT :m::mood)"])
def test_enum_parameter_keeps_context_constraints(rhs):
    sql = f"SELECT id FROM t WHERE m = {rhs}"
    context = definition()
    assert allowed_values_by_parameter(sql, "postgresql", context) == {"m": ["ok", "bad"]}
    for value, valid in [("ok", True), ("meh", False)]:
        result = validate_sql_operation(sql, "postgresql", context, {"m": "string"}, {"m": value})
        assert result.valid == valid, result.errors


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM t WHERE m::mood = :m::mood",
        "SELECT id FROM t WHERE (m::text)::public.mood = :m::mood",
        "WITH q AS (SELECT m::mood AS x FROM t) SELECT x FROM q WHERE x = :m::mood",
    ],
)
def test_column_enum_cast_keeps_field_policy(sql):
    context = definition()
    assert allowed_values_by_parameter(sql, "postgresql", context) == {"m": ["ok", "bad"]}
    result = validate_sql_operation(sql, "postgresql", context, {"m": "string"}, {"m": "meh"})
    assert not result.valid


async def test_intermediate_enum_cast_cannot_hide_an_invalid_parameter(client):
    created = await client.post(
        "/api/v1/contexts",
        json={
            "name": "enum_chain",
            "dialect": "postgresql",
            "definition": definition().model_dump(),
        },
    )
    saved = await client.post(
        "/api/v1/tools",
        json={
            "context_id": created.json()["id"],
            "name": "enum_chain",
            "description": "Ordered casts",
            "operation": {
                "type": "sql",
                "statement": "SELECT id FROM t WHERE m = :m::mood::varchar(2)::mood",
            },
            "input_schema": input_schema_for({"m": "string"}),
        },
    )
    assert saved.status_code == 201, saved.text
    # Truncating 'okay' first would yield 'ok', but the first enum cast must fail.
    for value, expected in [("ok", 200), ("okay", 422)]:
        invoked = await client.post(
            tool_path("enum_chain", "/invoke"), json={"arguments": {"m": value}}
        )
        assert invoked.status_code == expected, invoked.text


@pytest.mark.parametrize("type_name", ["public.mood", "mood", '"SomeSchema"."Mood"'])
def test_dump_check_casts_intersect_enum_values(type_name):
    result = import_ddl(
        ContextDefinition(),
        f"CREATE TYPE {type_name} AS ENUM ('ok', 'bad', 'meh'); "
        f"CREATE TABLE t (m {type_name}, "
        f"CHECK (m = ANY (ARRAY['ok'::{type_name}, 'bad'::{type_name}])))",
        "postgresql",
        "replace",
    )
    assert result.definition.resources[0].fields[0].allowed_values == ["ok", "bad"]
    assert not [warning.message for warning in result.warning_details]


@pytest.mark.parametrize("cast", ["'ok'::missing", "'nope'::public.mood"])
def test_import_does_not_invent_unknown_cast_values(cast):
    result = import_ddl(
        ContextDefinition(),
        "CREATE TYPE public.mood AS ENUM ('ok', 'bad'); "
        f"CREATE TABLE t (m public.mood CHECK (m IN ({cast})))",
        "postgresql",
        "replace",
    )
    assert result.definition.resources[0].fields[0].allowed_values == ["ok", "bad"]
    assert any(
        "not plain literals" in w for w in [warning.message for warning in result.warning_details]
    )


@pytest.mark.parametrize(
    "type_name, rhs, parameters",
    [
        ("public.mood", "'ok'::mood", {}),
        ("public.mood", "'ok'::public.mood", {}),
        ("mood", "CAST('ok' AS public.mood)", {}),
        ('"SomeSchema"."Mood"', '\'ok\'::"SomeSchema"."Mood"', {}),
        ("public.mood", ":m::public.mood", {"m": "string"}),
        ("public.mood", "ANY(ARRAY['ok'::mood, 'bad'::public.mood])", {}),
    ],
)
async def test_enum_cast_tool_save_invoke_preserves_application_sql(
    client, type_name, rhs, parameters
):
    created = await client.post(
        "/api/v1/contexts",
        json={
            "name": "enum_cast",
            "dialect": "postgresql",
            "definition": definition(type_name).model_dump(),
        },
    )
    assert created.status_code == 201, created.text
    sql = f"SELECT id FROM t WHERE m = {rhs}"
    saved = await client.post(
        "/api/v1/tools",
        json={
            "context_id": created.json()["id"],
            "name": "enum_cast",
            "description": "Enum cast",
            "operation": {"type": "sql", "statement": sql},
            "input_schema": input_schema_for(parameters),
        },
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["operation"]["statement"] == sql
    invoked = await client.post(
        tool_path("enum_cast", "/invoke"), json={"arguments": {"m": "ok"} if parameters else {}}
    )
    assert invoked.status_code == 200, invoked.text
    assert invoked.json()["operation"]["statement"] == sql
    if parameters:
        rejected = await client.post(
            tool_path("enum_cast", "/invoke"), json={"arguments": {"m": "meh"}}
        )
        assert rejected.status_code == 422, rejected.text
