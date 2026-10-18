"""Small CTE graphs must not expand into exponential lineage work."""

import pytest

import sidewit.sql as sql
from sidewit.domain.context import ContextDefinition
from sidewit.validation.input_schema import input_schema_for
from sidewit.validation.sql_analysis import SqlAnalysis
from sidewit.validation.sql_validator import validate_sql_operation
from sidewit.validation.sql_values import ValueAnalysis

DEFINITION = ContextDefinition.model_validate(
    {
        "resources": [
            {
                "name": "orders",
                "fields": [{"name": "status", "type": "text", "allowed_values": ["A", "B"]}],
            }
        ]
    }
)


def repeated_ctes(first: str, column: str, levels: int = 24) -> str:
    ctes = [f"t0 AS ({first})"]
    ctes += [
        f"t{i} AS (SELECT {column} FROM t{i - 1} UNION ALL SELECT {column} FROM t{i - 1})"
        for i in range(1, levels + 1)
    ]
    return "WITH " + ", ".join(ctes)


@pytest.mark.parametrize("origins", [False, True])
def test_repeated_cte_branches_have_bounded_work(monkeypatch, origins):
    owner, method = (ValueAnalysis, "output") if origins else (SqlAnalysis, "_source_field")
    original = getattr(owner, method)
    calls = 0

    def counted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        # Fail promptly on regression rather than hanging the suite on exponential work.
        assert calls < 1000, "Lineage repeatedly expanded the same CTE"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(owner, method, counted)
    if origins:
        statement = repeated_ctes("SELECT :status AS v", "v")
        statement += " SELECT status FROM orders WHERE status = (SELECT v FROM t24)"
    else:
        statement = repeated_ctes("SELECT status FROM orders", "status")
        statement += " SELECT status FROM t24 WHERE status = :status"
    result = validate_sql_operation(
        statement, "postgresql", DEFINITION, {"status": "string"}, {"status": "A"}
    )
    assert result.valid, result.errors


def test_distinct_union_values_are_not_lost():
    statement = repeated_ctes("SELECT 'A' AS v UNION ALL SELECT 'OUTSIDE' AS v", "v")
    statement += " SELECT status FROM orders WHERE status = ANY(SELECT v FROM t24)"
    result = validate_sql_operation(statement, "postgresql", DEFINITION, {})
    assert not result.valid
    assert "value_not_allowed" in {e.code for e in result.errors}


@pytest.mark.parametrize("projection_value, valid", [("X", True), ("A", False)])
def test_cached_lineage_keeps_join_position_visibility(projection_value, valid):
    definition = ContextDefinition.model_validate(
        {
            "resources": [
                *DEFINITION.to_json()["resources"],
                {"name": "no_status", "fields": [{"name": "id", "type": "int"}]},
                {
                    "name": "inner_orders",
                    "fields": [{"name": "status", "type": "text", "allowed_values": ["X", "Y"]}],
                },
            ]
        }
    )
    # The first ON sees the outer status; the SELECT sees the later inner table's status.
    statement = (
        f"SELECT (SELECT status = '{projection_value}' FROM no_status a "
        "JOIN no_status b ON status = 'A' JOIN inner_orders c ON TRUE) FROM orders"
    )
    result = validate_sql_operation(statement, "postgresql", definition, {})
    assert result.valid is valid, result.errors


async def test_exhausted_analysis_budget_is_a_validation_result(client, monkeypatch):
    response = await client.post(
        "/api/v1/contexts",
        json={"name": "bounded", "dialect": "postgresql", "definition": DEFINITION.to_json()},
    )
    assert response.status_code == 201
    monkeypatch.setattr(sql, "MAX_ANALYSIS_WORK", 0)
    response = await client.post(
        "/api/v1/validate",
        json={
            "context_id": response.json()["id"],
            "operation": {"statement": "SELECT status FROM orders WHERE status = 'A'"},
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["valid"] is False
    assert result["errors"][0]["code"] == "sql_analysis_error"


async def test_runtime_analysis_failure_cannot_be_ignored_by_cached_tool_checks(
    client, monkeypatch
):
    context = await client.post(
        "/api/v1/contexts",
        json={"name": "cached", "dialect": "postgresql", "definition": DEFINITION.to_json()},
    )
    tool = await client.post(
        "/api/v1/tools",
        json={
            "context_id": context.json()["id"],
            "name": "status_query",
            "description": "Find orders by status",
            "operation": {"statement": "SELECT status FROM orders WHERE status = :status"},
            "input_schema": input_schema_for({"status": "string"}),
        },
    )
    assert tool.status_code == 201, tool.text
    monkeypatch.setattr(sql, "MAX_ANALYSIS_WORK", 0)
    response = await client.post(
        f"/api/v1/tools/{tool.json()['id']}/invoke", json={"arguments": {"status": "A"}}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_arguments"
    assert response.json()["details"]["errors"][0]["code"] == "sql_analysis_error"
