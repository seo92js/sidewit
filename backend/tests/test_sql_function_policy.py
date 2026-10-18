"""Review regressions; SQL is parsed only, never executed."""

import pytest

from sidewit.domain.context import ContextDefinition
from sidewit.validation.sql_validator import generate_policy, validate_sql_operation

DEFINITION = {
    "resources": [
        {
            "name": "orders",
            "fields": [
                {"name": "id", "type": "bigint"},
                {"name": "name", "type": "text"},
                {"name": "created_at", "type": "timestamptz"},
            ],
        }
    ],
}


def check(sql, *, extensions=(), generated=False):
    definition = ContextDefinition.model_validate(DEFINITION | {"sql_extensions": list(extensions)})
    kwargs = {"policy": generate_policy("서울")} if generated else {}
    return validate_sql_operation(sql, "postgresql", definition, {}, **kwargs)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_sleep(100)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_import('/tmp/example')",
        "SELECT set_config('statement_timeout', '0', false)",
        "SELECT pg_terminate_backend(1)",
        "SELECT nextval('s')",
        "SELECT pg_catalog.pg_sleep(5)",
        "SELECT evil.f(id) FROM orders",
        "SELECT query_to_xml('SELECT * FROM pg_shadow', false, false, '')",
        "SELECT * FROM dblink('host=evil', 'DELETE FROM orders') AS t(x int)",
        "SELECT * FROM pg_ls_dir('/')",
        "SELECT (SELECT pg_sleep(1)) FROM orders",
        "WITH x AS (SELECT pg_sleep(1)) SELECT * FROM x",
        "SELECT pg_catalog.count(*) FROM orders",
    ],
)
def test_unapproved_functions_fail_closed(sql):
    result = check(sql)
    assert not result.valid
    assert "function_not_allowed" in {e.code for e in result.errors}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*), SUM(id), COALESCE(MAX(id), 0) FROM orders",
        "SELECT lower(name), date_trunc('day', created_at) FROM orders",
        "SELECT row_number() OVER (ORDER BY id) FROM orders",
        "SELECT * FROM unnest(ARRAY[1,2]) AS t(x)",
    ],
)
def test_supported_functions_remain_available(sql):
    result = check(sql)
    assert result.valid, result.errors


def test_postgis_requires_explicit_extension_and_does_not_allow_any_st_prefix():
    sql = "SELECT ST_DWithin(ST_MakePoint(1,2), ST_MakePoint(3,4), 10)"
    assert not check(sql).valid
    result = check(sql, extensions=["postgis"])
    assert result.valid, result.errors
    assert not check("SELECT ST_evil()", extensions=["postgis"]).valid
    assert not check("SELECT evil.ST_MakePoint(1,2)", extensions=["postgis"]).valid


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_catalog.now()",
        "SELECT $$now$$::timestamptz",
        "SELECT 'yesterday 10:00'::timestamp",
        "SELECT id FROM orders WHERE created_at >= 'today'",
        "SELECT id FROM orders o WHERE o.created_at >= 'today'",
        "WITH o AS (SELECT created_at AS d FROM orders) SELECT d FROM o WHERE d < $$tomorrow$$",
        "SELECT id FROM orders WHERE created_at BETWEEN 'yesterday' AND 'today'",
    ],
)
def test_generated_sql_cannot_use_hidden_database_clock(sql):
    result = check(sql, generated=True)
    assert not result.valid
    assert "relative_time_function" in {e.code for e in result.errors}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'today'",
        "SELECT name FROM orders WHERE name = 'today'",
    ],
)
def test_plain_text_is_not_a_clock_expression(sql):
    assert check(sql, generated=True).valid


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ('today'::text)::date",
        "SELECT COALESCE(NULL, 'today')::date",
        "SELECT CASE WHEN true THEN 'today' ELSE '2001-01-01' END::date",
        "WITH d AS (SELECT 'today'::text AS v) SELECT v::date FROM d",
        "WITH d AS (SELECT COALESCE(NULL, 'today') AS v) SELECT v::date FROM d",
        "SELECT id FROM orders WHERE created_at >= (SELECT 'today')",
        "SELECT id FROM orders WHERE created_at IN (SELECT 'today')",
        "SELECT date_trunc('day', 'today')",
        "SELECT date('today')",
        "SELECT age('today', created_at) FROM orders",
        "SELECT ARRAY['today']::date[]",
        "SELECT id FROM orders WHERE date_trunc('day', created_at) >= 'today'",
        "WITH d AS (SELECT date_trunc('day', created_at) AS v FROM orders) "
        "SELECT v FROM d WHERE v >= 'today'",
    ],
)
def test_temporal_values_are_traced_through_branches_and_subqueries(sql):
    result = check(sql, generated=True)
    assert "relative_time_function" in {e.code for e in result.errors}


@pytest.mark.parametrize("value", ["today", "now", "yesterday 10:00", " Tomorrow "])
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT :boundary::timestamptz",
        "SELECT id FROM orders WHERE created_at >= :boundary",
        "WITH d AS (SELECT :boundary AS v) SELECT COALESCE(v, NULL)::date FROM d",
    ],
)
def test_temporal_parameter_values_cannot_use_database_clock(sql, value):
    result = validate_sql_operation(
        sql,
        "postgresql",
        ContextDefinition.model_validate(DEFINITION),
        {"boundary": "string"},
        {"boundary": value},
        generate_policy("Find records"),
    )
    assert "relative_time_function" in {e.code for e in result.errors}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ('to' || 'day')::date",
        "WITH d AS (SELECT upper('today') AS v) SELECT v::date FROM d",
        "SELECT '{today}'::date[]",
    ],
)
def test_unverifiable_computed_date_inputs_fail_closed(sql):
    result = check(sql, generated=True)
    assert "relative_time_unverifiable" in {e.code for e in result.errors}
    # Saved Tools deliberately retain the ordinary, database-clock-compatible policy.
    assert check(sql).valid


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COALESCE(created_at, '2001-01-01')::date FROM orders",
        "SELECT date_trunc('day', created_at)::date FROM orders",
        "SELECT (created_at AT TIME ZONE 'Asia/Seoul')::date FROM orders",
        "SELECT (created_at + INTERVAL '1 day')::date FROM orders",
        "SELECT CASE WHEN name = 'today' THEN created_at ELSE '2001-01-01' END::date FROM orders",
        "WITH d AS (SELECT :boundary AS v) SELECT v::timestamptz FROM d",
        "SELECT name FROM orders WHERE name = :boundary",
        "SELECT ARRAY['2001-01-01']::date[]",
    ],
)
def test_absolute_dates_database_values_and_plain_text_remain_supported(sql):
    params = {"boundary": "string"} if ":boundary" in sql else {}
    values = {"boundary": "2001-01-01T00:00:00+09:00"} if params else {}
    result = validate_sql_operation(
        sql,
        "postgresql",
        ContextDefinition.model_validate(DEFINITION),
        params,
        values,
        generate_policy("Find records"),
    )
    assert result.valid, result.errors


@pytest.mark.parametrize("literal", ["$$:x$$", "$tag$:x$tag$", "E':x'", "U&':x'"])
def test_alternative_strings_cannot_hide_placeholders(literal):
    assert not check(f"SELECT {literal}").valid


@pytest.mark.parametrize("literal", ["$$서울$$", "E'서울'", "U&'서울'"])
def test_alternative_strings_cannot_hide_request_values(literal):
    assert not check(f"SELECT id FROM orders WHERE name = {literal}", generated=True).valid


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
async def test_nonfinite_allowed_values_rejected_for_unknown_types(client, value):
    response = await client.post(
        "/api/v1/contexts",
        content='{"name":"bad","dialect":"postgresql","definition":{"resources":[{"name":"r",'
        '"fields":[{"name":"v","type":"custom_type","allowed_values":[' + value + "]}]}]}}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text


async def test_postgis_setting_survives_ddl_import(client):
    response = await client.post(
        "/api/v1/contexts",
        json={
            "name": "geo",
            "dialect": "postgresql",
            "definition": {"sql_extensions": ["postgis"]},
        },
    )
    assert response.status_code == 201, response.text
    context = response.json()
    from sidewit.ddl.importer import import_ddl

    # Both modes preserve non-schema Context policy.
    for mode in ("merge", "replace"):
        result = import_ddl(
            ContextDefinition.model_validate(context["definition"]),
            "CREATE TABLE points (id bigint)",
            "postgresql",
            mode=mode,
        )
        assert result.definition.sql_extensions == ["postgis"]


async def test_stale_tool_status_is_derived_without_overwriting_record(client, store):
    from sidewit.validation.input_schema import input_schema_for
    from tests.test_validation_boundaries import create_context, create_tool

    context = await create_context(client, DEFINITION)
    tool = await create_tool(client, context, "SELECT id FROM orders", input_schema_for({}))
    from uuid import UUID

    record = store.context(UUID(context["id"]))
    record.version += 1
    store.save_context(record)
    response = await client.get(f"/api/v1/tools/{tool['id']}")
    assert response.json()["status"] == "stale"
    assert response.json()["validation"] is None
    listing = await client.get("/api/v1/tools")
    assert listing.json()["items"][0]["status"] == "stale"
    assert store.tool(tool["name"]).status == "valid"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT bool_and(id > 0), bool_or(id > 0) FROM orders",
        "SELECT position('a' in name), strpos(name, 'a') FROM orders",
        "SELECT created_at::text = 'today' FROM orders",
        "SELECT to_char(created_at, 'YYYY-MM-DD') = 'today' FROM orders",
        "SELECT created_at::text IN ('today', 'yesterday') FROM orders",
        "SELECT created_at::text BETWEEN 'today' AND 'tomorrow' FROM orders",
        "WITH x AS (SELECT created_at::text AS d FROM orders) SELECT d = 'today' FROM x",
    ],
)
def test_supported_functions_and_text_results_are_not_false_positives(sql):
    result = check(sql, generated=True)
    assert result.valid, result.errors


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders WHERE 'today' <= created_at",
        "SELECT id FROM orders WHERE (created_at) >= ('today')",
        "SELECT id FROM orders WHERE created_at::date >= 'today'",
        "SELECT id FROM orders WHERE created_at IN ('today', 'yesterday')",
    ],
)
def test_temporal_operands_still_reject_database_clock(sql):
    result = check(sql, generated=True)
    assert "relative_time_function" in {e.code for e in result.errors}


def test_builtin_allowlist_contains_only_canonical_expression_names():
    from sqlglot import exp

    from sidewit.validation.functions import BUILTINS

    canonical = {
        node.sql_name().lower()
        for node in vars(exp).values()
        if isinstance(node, type) and issubclass(node, exp.Func) and node is not exp.Func
    }
    assert BUILTINS <= canonical
