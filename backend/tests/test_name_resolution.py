"""PostgreSQL name resolution and value-conversion boundaries, each checked against what
PostgreSQL itself accepts or rejects."""

import pytest

from sidewit.domain.context import ContextDefinition
from sidewit.generation.parameters import convert_parameter
from sidewit.validation.sql_validator import validate_sql_operation

CONTEXT = ContextDefinition.model_validate(
    {
        "resources": [
            {
                "name": "orders",
                "fields": [
                    {"name": "id", "type": "bigint"},
                    {"name": "status", "type": "text", "allowed_values": ["ACTIVE"]},
                    {"name": "flag", "type": "boolean", "allowed_values": [True]},
                    {"name": "amount", "type": "numeric"},
                ],
            },
            {
                "name": "customers",
                "fields": [{"name": "id", "type": "bigint"}, {"name": "name", "type": "text"}],
            },
        ]
    }
)


def codes(sql, types=None, values=None):
    result = validate_sql_operation(sql, "postgresql", CONTEXT, types or {}, values)
    return [e.code for e in result.errors]


# valid in PostgreSQL ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # USING merges id into one column: 5 columns, unqualified id is not ambiguous.
        "SELECT * FROM orders o JOIN customers c USING (id) "
        "UNION ALL SELECT o.id, status, flag, amount, name FROM orders o, customers",
        "SELECT id FROM orders JOIN customers USING (id)",
        "SELECT id, name FROM orders NATURAL JOIN customers",
        "SELECT o.id, c.id FROM orders o JOIN customers c USING (id)",
        # Correlated subqueries and LATERAL may reference the enclosing query.
        "SELECT 1 FROM orders o WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = o.id)",
        "SELECT 1 FROM orders o JOIN LATERAL (SELECT o.id) q ON true",
        "SELECT 1 FROM orders o WHERE EXISTS (SELECT 1 FROM (SELECT o.id) q)",
        # Boolean text cast and quoted boolean literals.
        "SELECT id FROM orders WHERE flag::text = 'true'",
        "SELECT id FROM orders WHERE flag = 'true'",
        "SELECT id FROM orders WHERE flag = 't'",
        "SELECT o.* FROM orders o",
    ],
)
def test_postgres_accepts(sql):
    assert codes(sql) == []


# rejected by PostgreSQL ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT missing.* FROM orders", ["unknown_resource"]),
        ("SELECT 1 FROM orders o JOIN customers c USING (missing)", ["unknown_field"]),
        ("SELECT 1 FROM orders o JOIN customers c USING (status)", ["unknown_field"]),
        (
            "SELECT 1 FROM orders a JOIN orders b ON true JOIN customers c USING (id)",
            ["ambiguous_field"],
        ),
        ("SELECT 1 FROM orders o JOIN (SELECT o.id) q ON true", ["unknown_resource"]),
        ("SELECT 1 FROM orders o JOIN (SELECT id) q ON true", ["unknown_field"]),
        (
            "WITH q AS (SELECT o.id FROM customers) SELECT 1 FROM orders o, q",
            ["unknown_resource"],
        ),
        ("SELECT id FROM orders, customers", ["ambiguous_field"]),
    ],
)
def test_postgres_rejects(sql, expected):
    assert codes(sql) == expected


def test_boolean_values_keep_their_domain():
    assert codes("SELECT id FROM orders WHERE flag = 'false'") == ["value_not_allowed"]
    assert codes("SELECT id FROM orders WHERE flag::text = 'false'") == []  # text, no domain


def test_text_cast_of_a_string_field_keeps_its_domain_through_using():
    sql = "SELECT s FROM (SELECT status::text AS s FROM orders JOIN customers USING (id)) q "
    assert codes(sql + "WHERE s = 'X'") == ["value_not_allowed"]


# value conversion ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        ("9007199254740993", "integer", 9007199254740993),
        ("9007199254740993.0", "number", 9007199254740993),
        ("0.1", "number", 0.1),
        ("1e2", "number", 100),
        # A float would round these: kept as text, so validation rejects them for repair.
        ("9007199254740993.5", "number", "9007199254740993.5"),
        ("1.0000000000000001", "number", "1.0000000000000001"),
        ("1.5", "integer", "1.5"),
        ("1e999", "number", "1e999"),
        ("1e30", "number", 1e30),
    ],
)
def test_provider_numbers_convert_only_when_exact(text, kind, expected):
    value = convert_parameter(text, kind)
    assert value == expected and type(value) is type(expected)


async def test_generate_never_returns_a_rounded_model_number(client):
    from sidewit.generation.dto import LLMParameter
    from tests.test_generate_api import ok
    from tests.test_metric_api import ask, install

    res = await client.post(
        "/api/v1/contexts",
        json={"name": "n", "dialect": "postgresql", "definition": CONTEXT.to_json()},
    )
    context = res.json()
    sql = "SELECT id FROM orders WHERE amount = :amount"
    rounded = ok(sql, [LLMParameter(name="amount", type="number", value="9007199254740993.5")])
    exact = ok(sql, [LLMParameter(name="amount", type="number", value="9007199254740993")])
    provider = install(rounded, exact)
    res = await ask(client, context, question="amount 조회")
    assert res.status_code == 200, res.text
    # The inexact value went to repair; the answer carries the exact integer.
    assert res.json()["operations"][0]["operation"]["parameters"] == {"amount": 9007199254740993}
    assert "parameter_type_mismatch" in provider.calls[1][1][2].content


def test_all_comparison_constrains_values_and_parameters():
    from sidewit.validation.sql_validator import allowed_values_by_parameter

    sql = "SELECT id FROM orders WHERE status != ALL(ARRAY[:s])"
    assert allowed_values_by_parameter(sql, "postgresql", CONTEXT) == {"s": ["ACTIVE"]}
    assert codes(sql, {"s": "string"}, {"s": "OUTSIDE"}) == ["value_not_allowed"]


@pytest.mark.parametrize("text", ["1e99999999999999999999999", "1e-99999999999999999999999"])
def test_extreme_exponents_are_not_numbers_and_never_raise(text):
    from sidewit.domain.params import exact_number

    assert exact_number(text) is None
    assert convert_parameter(text, "number") == text


async def test_ddl_with_an_extreme_numeric_check_is_a_client_error(client):
    res = await client.post(
        "/api/v1/contexts", json={"name": "d", "dialect": "postgresql", "definition": {}}
    )
    client._transport.raise_app_exceptions = False
    res = await client.post(
        f"/api/v1/contexts/{res.json()['id']}/import-ddl",
        json={"ddl": "CREATE TABLE t (n numeric CHECK (n IN (1e99999999999999999999999)));"},
    )
    assert res.status_code == 422, res.text


@pytest.mark.parametrize(
    "sql",
    [
        # ON sees the join as it stands there: status is still orders.status.
        "SELECT o.id FROM orders o JOIN customers c ON status = :s JOIN orders x USING (status)",
        # After the merge, status is the USING column; both sides are orders.status.
        "SELECT o.id FROM orders o JOIN orders x USING (status) JOIN customers c ON status = :s",
        "SELECT o.id FROM orders o JOIN orders x USING (status) WHERE status = :s",
    ],
)
def test_values_follow_the_join_as_it_stands_at_each_position(sql):
    from sidewit.validation.sql_validator import allowed_values_by_parameter

    assert allowed_values_by_parameter(sql, "postgresql", CONTEXT) == {"s": ["ACTIVE"]}
    assert codes(sql, {"s": "string"}, {"s": "OUTSIDE"}) == ["value_not_allowed"]


def test_aliased_parenthesized_join_keeps_value_lineage():
    from sidewit.validation.sql_validator import allowed_values_by_parameter

    sql = "SELECT j.status FROM (orders JOIN customers USING (id)) AS j WHERE j.status = :s"
    assert allowed_values_by_parameter(sql, "postgresql", CONTEXT) == {"s": ["ACTIVE"]}
    assert codes(sql, {"s": "string"}, {"s": "OUTSIDE"}) == ["value_not_allowed"]
    renamed = "SELECT j.x FROM (orders JOIN customers USING (id)) AS j(i, x) WHERE j.x = 'NO'"
    assert codes(renamed) == ["value_not_allowed"]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # A value-changing cast is applied, as PostgreSQL does: 'ACTIVEX'::varchar(6) is ACTIVE.
        ("SELECT id FROM orders WHERE status = 'ACTIVEX'::varchar(6)", []),
        ("SELECT id FROM orders WHERE status = 'ZZ'::varchar(1)", ["value_not_allowed"]),
        ("SELECT id FROM orders WHERE status = 'NOPE'::text", ["value_not_allowed"]),
    ],
)
def test_casts_on_compared_values_are_applied(sql, expected):
    assert codes(sql) == expected


def test_a_truncating_cast_does_not_constrain_the_parameter():
    from sidewit.validation.sql_validator import allowed_values_by_parameter

    sql = "SELECT id FROM orders WHERE status = CAST(:s AS varchar(1))"
    assert allowed_values_by_parameter(sql, "postgresql", CONTEXT) == {}
