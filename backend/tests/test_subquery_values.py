"""Value constraints survive SQL query wrappers without discarding transformations."""

import pytest

from sidewit.domain.context import ContextDefinition
from sidewit.validation.input_schema import input_schema_for
from sidewit.validation.sql_validator import validate_sql_operation
from tests.test_validation_boundaries import DEFINITION, create_context, create_tool


@pytest.mark.parametrize(
    "comparison",
    [
        "= (SELECT :status)",
        "= ANY(SELECT :status)",
        "= (VALUES (:status))",
        "IN (SELECT :status)",
        "IN (VALUES (:status), ('A'))",
        "IN (SELECT :status UNION ALL SELECT 'A')",
        "= (SELECT (SELECT :status) AS result)",
        "= CAST((SELECT :status) AS text)",
    ],
)
async def test_query_parameter_domain_at_save_and_invoke(client, comparison):
    ctx = await create_context(client, DEFINITION)
    statement = "SELECT status FROM orders WHERE status " + comparison
    await create_tool(client, ctx, statement, input_schema_for({"status": "string"}))
    # Exercise both initial validation and cached invocation, with both legal values.
    for value, expected in [("A", 200), ("OUTSIDE", 422), ("B", 200), ("OUTSIDE", 422)]:
        response = await client.post(
            "/api/v1/tools/review_tool/invoke", json={"arguments": {"status": value}}
        )
        assert response.status_code == expected, response.text
        if expected == 422:
            assert response.json()["details"]["errors"][0]["code"] == "value_not_allowed"


@pytest.mark.parametrize(
    ("expression", "valid"),
    [
        ("CAST((SELECT 'AX') AS varchar(1))", True),
        ("CAST((SELECT 'ZX') AS varchar(1))", False),
        ("(SELECT CAST('AX' AS varchar(1)))", True),
        ("(SELECT CAST('ZX' AS varchar(1)))", False),
        ("CAST((SELECT CAST('AX' AS varchar(2))) AS varchar(1))", True),
    ],
)
def test_casts_inside_and_outside_query_keep_their_meaning(expression, valid):
    result = validate_sql_operation(
        "SELECT status FROM orders WHERE status = " + expression,
        "postgresql",
        ContextDefinition.model_validate(DEFINITION),
        {},
    )
    assert result.valid is valid, result
    if not valid:
        assert any(issue.code == "value_not_allowed" for issue in result.errors)


async def test_unmodelled_projection_fails_explicitly_at_save(client):
    ctx = await create_context(client, DEFINITION)
    response = await client.post(
        "/api/v1/tools",
        json={
            "context_id": ctx["id"],
            "name": "unmodelled",
            "description": "probe",
            "operation": {
                "type": "sql",
                "statement": "SELECT status FROM orders WHERE status = (SELECT upper(:status))",
            },
            "input_schema": input_schema_for({"status": "string"}),
        },
    )
    assert response.status_code == 422, response.text
    assert any(
        e["code"] == "value_unverifiable" for e in response.json()["details"]["validation_errors"]
    )
