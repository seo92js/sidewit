"""Registered constants: Rule conditions and metric fixed filters, enforced by field and value
(never by matching rule prose)."""

import copy

import pytest

from sidewit.domain.context import ContextDefinition, definition_issues, registered_constants
from sidewit.generation.dto import LLMParameter
from sidewit.generation.prompt import render_context
from sidewit.validation.sql_validator import (
    DEFAULT_POLICY,
    generate_policy,
    validate_sql_operation,
)
from tests.test_generate_api import ok
from tests.test_metric_api import ask, install

BASE = {
    "resources": [
        {
            "name": "rentals",
            "fields": [
                {"name": "id", "type": "bigint"},
                {"name": "fee", "type": "numeric"},
                {
                    "name": "state",
                    "type": "text",
                    "allowed_values": ["SETTLED", "VOID", "OPEN"],
                },
            ],
        }
    ],
    # The prose names no code: only the condition says which value it is.
    "rules": [
        {
            "id": "revenue",
            "text": "대여료 합계는 결제가 끝난 대여만 포함한다.",
            "condition": {"field": "rentals.state", "op": "=", "value": "SETTLED"},
        }
    ],
}


def definition(**changes):
    data = copy.deepcopy(BASE) | changes
    return ContextDefinition.model_validate(data)


def errors(sql, values, question="결제된 대여료 합계", context=None, policy=None):
    declared = {name: "string" if isinstance(v, str) else "number" for name, v in values.items()}
    result = validate_sql_operation(
        sql,
        "postgresql",
        context or definition(),
        declared,
        values,
        policy or generate_policy(question),
    )
    return [(e.code, e.ref) for e in result.errors]


# domain ---------------------------------------------------------------------------------


def test_rule_conditions_and_metric_fixed_filters_are_registered_constants():
    data = copy.deepcopy(BASE)
    data["metrics"] = [
        {
            "id": "open_count",
            "description": "Open rentals",
            "resource": "rentals",
            "aggregate": {"kind": "row_count"},
            "fixed_filters": [{"field": "rentals.state", "value": "OPEN"}],
        }
    ]
    constants = registered_constants(ContextDefinition.model_validate(data))
    assert [(c.field, c.value, c.source) for c in constants] == [
        ("rentals.state", "SETTLED", "rule revenue"),
        ("rentals.state", "OPEN", "metric open_count"),
    ]


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ({"field": "rentals.status", "value": "SETTLED"}, "does not exist"),
        ({"field": "rentals.fee", "value": "high"}, "known field type"),
        ({"field": "rentals.state", "value": "PAID"}, "allowed values"),
        ({"field": "rentals.id", "value": ""}, "must not be empty"),
    ],
)
def test_invalid_rule_conditions_are_rejected(condition, message):
    rules = [{"id": "r", "text": "rule", "condition": condition}]
    issues = definition_issues(definition(rules=rules))
    assert [(i.code, i.ref) for i in issues] == [("invalid_rule_condition", "rules.r.condition")]
    assert message in issues[0].message


# validator ------------------------------------------------------------------------------


def test_parameter_bound_to_a_rule_constant_is_rejected():
    sql = "SELECT SUM(fee) FROM rentals WHERE state = :state"
    assert errors(sql, {"state": "SETTLED"}) == [("constant_as_parameter", "state")]


def test_parameter_bound_to_a_metric_fixed_filter_is_rejected():
    data = copy.deepcopy(BASE) | {"rules": []}
    data["metrics"] = [
        {
            "id": "settled_fees",
            "description": "Settled fees",
            "resource": "rentals",
            "aggregate": {"kind": "sum", "field": "rentals.fee"},
            "fixed_filters": [{"field": "rentals.state", "value": "SETTLED"}],
        }
    ]
    context = ContextDefinition.model_validate(data)
    sql = "SELECT SUM(fee) FROM rentals WHERE state IN (:state)"
    found = errors(sql, {"state": "SETTLED"}, context=context)
    assert found == [("constant_as_parameter", "state")]


@pytest.mark.parametrize(
    ("sql", "values"),
    [
        # The constant written as a literal is exactly what the rule asks for.
        ("SELECT SUM(fee) FROM rentals WHERE state = 'SETTLED'", {}),
        # A different value on the same field is a genuine user choice.
        ("SELECT id FROM rentals WHERE state = :state", {"state": "OPEN"}),
        # Another field is unaffected.
        ("SELECT id FROM rentals WHERE fee >= :minimum", {"minimum": 3000}),
    ],
)
def test_literal_constants_and_other_values_pass(sql, values):
    assert errors(sql, values) == []


def test_tools_and_templates_are_not_checked():
    sql = "SELECT SUM(fee) FROM rentals WHERE state = :state"
    assert errors(sql, {"state": "SETTLED"}, policy=DEFAULT_POLICY) == [], "manual Tool"
    template = validate_sql_operation(
        sql, "postgresql", definition(), {"state": "string"}, None, generate_policy("q")
    )
    assert template.valid, "no values, nothing to compare"


def test_structural_constant_is_not_a_request_value_even_if_the_user_names_it():
    """The prose never mentions SETTLED, so only the condition identifies it as a constant."""
    sql = "SELECT SUM(fee) FROM rentals WHERE state = 'SETTLED'"
    assert errors(sql, {}, question="SETTLED 대여료 합계") == []
    without_condition = definition(rules=[{"id": "revenue", "text": "결제가 끝난 대여만"}])
    found = errors(sql, {}, question="SETTLED 대여료 합계", context=without_condition)
    assert found == [("request_value_literal", "state")], "prose-only fallback"


# prompt ---------------------------------------------------------------------------------


def test_conditions_are_rendered_and_rules_without_one_are_unchanged():
    rendered = render_context(definition())
    assert (
        "- [revenue] 대여료 합계는 결제가 끝난 대여만 포함한다. (fixed: rentals.state = 'SETTLED'; "
        "always a SQL literal, never an input)" in rendered
    )
    plain = render_context(definition(rules=[{"id": "r", "text": "Some rule."}]))
    assert "- [r] Some rule.\n" in plain + "\n"


# Generate -------------------------------------------------------------------------------


@pytest.fixture
async def rule_context(client):
    res = await client.post(
        "/api/v1/contexts",
        json={"name": "rules", "dialect": "postgresql", "definition": BASE},
    )
    assert res.status_code == 201, res.text
    return res.json()


async def test_parameterized_constant_is_repaired_into_a_literal(client, rule_context):
    parameterized = ok(
        "SELECT SUM(fee) FROM rentals WHERE fee >= :minimum AND state = :state",
        [
            LLMParameter(name="minimum", type="number", value="3000"),
            LLMParameter(name="state", type="string", value="SETTLED"),
        ],
    )
    literal = ok(
        "SELECT SUM(fee) FROM rentals WHERE fee >= :minimum AND state = 'SETTLED'",
        [LLMParameter(name="minimum", type="number", value="3000")],
    )
    provider = install(parameterized, literal)
    res = await ask(client, rule_context, question="대여료 3000원 이상인 결제 대여료 합계")
    assert res.status_code == 200, res.text
    assert res.json()["operation"]["parameters"] == {"minimum": 3000}
    repair = provider.calls[1][1][2].content
    assert "[constant_as_parameter]" in repair and "rule revenue" in repair


async def test_context_api_rejects_an_invalid_condition(client):
    bad = copy.deepcopy(BASE)
    bad["rules"][0]["condition"]["value"] = "PAID"
    res = await client.post(
        "/api/v1/contexts", json={"name": "bad", "dialect": "postgresql", "definition": bad}
    )
    assert res.status_code == 422
    assert res.json()["details"]["errors"][0]["code"] == "invalid_rule_condition"


# coverage of every comparison position and operator ---------------------------------


def test_range_constant_bound_to_a_parameter_is_rejected():
    rules = [
        {
            "id": "big",
            "text": "큰 대여만 본다.",
            "condition": {"field": "rentals.fee", "op": ">=", "value": 3000},
        }
    ]
    context = definition(rules=rules)
    sql = "SELECT COUNT(*) FROM rentals WHERE fee >= :minimum"
    found = errors(sql, {"minimum": 3000}, context=context)
    assert found == [("constant_as_parameter", "minimum")]
    flipped = "SELECT COUNT(*) FROM rentals WHERE :minimum <= fee"
    assert errors(flipped, {"minimum": 3000}, context=context) == [
        ("constant_as_parameter", "minimum")
    ]
    # Another operator on the same value is a different condition.
    assert errors("SELECT id FROM rentals WHERE fee = :fee", {"fee": 3000}, context=context) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FILTER (WHERE state = :state) FROM rentals",
        "SELECT SUM(CASE WHEN state = :state THEN fee END) FROM rentals",
        "SELECT id FROM rentals WHERE state::text = :state",
        "SELECT id FROM rentals WHERE state = ANY(ARRAY[:state])",
    ],
)
def test_constants_are_enforced_outside_where_too(sql):
    assert errors(sql, {"state": "SETTLED"}) == [("constant_as_parameter", "state")]


def test_contract_sql_may_bind_a_field_another_metric_fixes():
    """Server-compiled metric SQL declares its inputs; another metric's fixed filter on the
    same field must not reject it (CONTRACT_POLICY)."""
    from sidewit.validation.sql_validator import CONTRACT_POLICY

    sql = "SELECT COUNT(*) FROM rentals WHERE state = :state"
    assert errors(sql, {"state": "SETTLED"}, policy=CONTRACT_POLICY) == []
