import pytest

from sidewit.validation.input_schema import input_schema_for
from tests.test_validation_boundaries import create_context

D = {
    "resources": [
        {
            "name": "orders",
            "fields": [{"name": "status", "type": "text", "allowed_values": ["A", "B"]}],
        }
    ]
}


@pytest.mark.parametrize(
    "rhs",
    [
        "(SELECT v FROM (VALUES (:s)) AS q(v))",
        "(WITH q AS (SELECT :s AS v) SELECT v FROM q)",
        "(WITH q(v) AS (VALUES (:s)) SELECT * FROM q)",
        "(SELECT z FROM (SELECT :s AS v) q(z))",
        "(SELECT v FROM (SELECT :s AS v UNION ALL SELECT 'A') q LIMIT 1)",
        "CAST((WITH q AS (SELECT :s AS v) SELECT v FROM q) AS text)",
    ],
)
async def test_projected_parameter_still_constrained(client, rhs):
    ctx = await create_context(client, D)
    sql = "SELECT status FROM orders WHERE status = " + rhs
    r = await client.post(
        "/api/v1/tools",
        json={
            "context_id": ctx["id"],
            "name": "probe",
            "description": "probe",
            "operation": {"type": "sql", "statement": sql},
            "input_schema": input_schema_for({"s": "string"}),
        },
    )
    assert r.status_code == 201, r.text
    for value, expected in [("A", 200), ("OUTSIDE", 422), ("B", 200)]:
        r = await client.post("/api/v1/tools/probe/invoke", json={"arguments": {"s": value}})
        assert r.status_code == expected, r.text
        checked = await client.post(
            "/api/v1/validate",
            json={
                "context_id": ctx["id"],
                "operation": {
                    "statement": sql,
                    "parameter_types": {"s": "string"},
                    "parameters": {"s": value},
                },
            },
        )
        assert checked.status_code == 200, checked.text
        assert checked.json()["valid"] == (expected == 200), checked.text


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("16777217::real", 16777216),
    ],
)
async def test_numeric_cast_import_matches_postgres(client, expr, expected):
    ctx = await create_context(client, {"resources": []})
    r = await client.post(
        "/api/v1/contexts/" + ctx["id"] + "/import-ddl",
        json={"ddl": "CREATE TABLE t(code numeric CHECK(code IN (" + expr + ")))"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    actual = body["definition"]["resources"][0]["fields"][0]["allowed_values"]
    assert actual == [expected] or (actual == [] and body["warnings"]), r.text


@pytest.mark.parametrize(
    "rhs",
    [
        "(WITH q AS (SELECT :s AS v) SELECT upper(v) FROM q)",
        "(SELECT upper(v) FROM (VALUES (:s)) q(v))",
        "(SELECT CAST(v AS real) FROM (VALUES (:s)) q(v))",
    ],
)
async def test_unknown_input_transform_rejected_at_save(client, rhs):
    ctx = await create_context(client, D)
    r = await client.post(
        "/api/v1/tools",
        json={
            "context_id": ctx["id"],
            "name": "unknown",
            "description": "probe",
            "operation": {
                "type": "sql",
                "statement": "SELECT status FROM orders WHERE status=" + rhs,
            },
            "input_schema": input_schema_for({"s": "string"}),
        },
    )
    assert r.status_code == 422, r.text
    assert any(e["code"] == "value_unverifiable" for e in r.json()["details"]["validation_errors"])


async def test_database_column_comparison_needs_no_input_evaluation(client):
    ctx = await create_context(client, D)
    r = await client.post(
        "/api/v1/validate",
        json={
            "context_id": ctx["id"],
            "operation": {
                "statement": "SELECT a.status FROM orders a JOIN orders b ON a.status=b.status"
            },
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["valid"], r.text
