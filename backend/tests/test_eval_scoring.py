import copy

import pytest
from pydantic import ValidationError

from sidewit.domain.operation import SqlOperation
from sidewit.eval.analysis import analyze
from sidewit.eval.cases import (
    AggregateExpectation,
    ConstraintExpectation,
    Expectations,
    ParameterExpectation,
    load_suite,
)
from sidewit.eval.scoring import score_operation


def operation(statement, parameters=None, types=None):
    return SqlOperation(
        dialect="postgresql",
        statement=statement,
        parameters=parameters or {},
        parameter_types=types or {},
    )


def score(suite, expect, statement, parameters=None, types=None):
    return score_operation(
        expect,
        operation(statement, parameters, types),
        suite.context.definition,
        suite.context.timezone,
    )


def test_commerce_suite_has_required_coverage(golden_suite):
    cases = golden_suite.cases
    assert len(cases) >= 15
    assert sum(c.expect.status == "ambiguous" for c in cases) >= 3
    assert sum(c.expect.status == "unanswerable" for c in cases) >= 3
    assert {
        "single_resource",
        "join",
        "business_rule",
        "relative_time",
        "date_column_by_description",
        "ambiguous_date_column",
        "parameterization",
        "unanswerable",
    } <= {c.category for c in cases}
    assert len({c.id for c in cases}) == len(cases)
    assert all(c.reference is not None for c in cases if c.expect.status == "ok")
    fields = {
        f.name: f.description
        for r in golden_suite.context.definition.resources
        if r.name == "orders"
        for f in r.fields
    }
    assert "매출" in fields["paid_at"]
    assert "주문 건수" in fields["ordered_at"]
    assert all(
        "paid_at" not in r.text and "ordered_at" not in r.text
        for r in golden_suite.context.definition.rules
    )


@pytest.mark.parametrize(
    "case_id",
    [
        "customers_in_region",
        "customer_list",
        "revenue_by_region",
        "shipment_carriers_with_amount",
        "total_revenue",
        "order_count_excludes_cancelled",
        "revenue_last_30_days",
        "revenue_last_month",
        "order_count_last_week_uses_ordered_at",
        "revenue_yesterday_uses_paid_at",
        "busan_revenue",
        "orders_over_amount",
    ],
)
def test_authored_reference_is_valid_and_satisfies_expectations(golden_suite, case_id):
    case = next(c for c in golden_suite.cases if c.id == case_id)
    op = SqlOperation(dialect=golden_suite.context.dialect, **case.reference.model_dump())
    assert (
        score_operation(
            case.expect, op, golden_suite.context.definition, golden_suite.context.timezone
        )
        == []
    )


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT SUM(o.amount) FROM orders AS o WHERE o.status = 'COMPLETED'",
        "WITH t AS (SELECT amount, status FROM orders) "
        "SELECT SUM(amount) FROM t WHERE status = 'COMPLETED'",
        "SELECT SUM(x.amount) FROM (SELECT amount, status FROM orders) x "
        "WHERE x.status = 'COMPLETED'",
        "WITH t(a, s) AS (SELECT amount, status FROM orders) "
        "SELECT SUM(a) FROM t WHERE s = 'COMPLETED'",
    ],
)
def test_alias_cte_and_derived_table_variants(golden_suite, statement):
    expect = Expectations(
        status="ok",
        resources=["orders"],
        fields=["orders.amount"],
        constraints=[ConstraintExpectation(field="orders.status", op="=", value="COMPLETED")],
    )
    assert score(golden_suite, expect, statement) == []


def test_unused_cte_cannot_supply_expected_predicate(golden_suite):
    expect = Expectations(
        status="ok",
        resources=["orders"],
        constraints=[ConstraintExpectation(field="orders.status", op="=", value="COMPLETED")],
    )
    failures = score(
        golden_suite,
        expect,
        "WITH unused AS (SELECT id FROM orders WHERE status = 'COMPLETED') "
        "SELECT SUM(amount) FROM orders",
    )
    assert [f.expectation for f in failures] == ["constraints"]


def test_reused_alias_in_nested_query_resolves_in_each_scope(golden_suite):
    facts = analyze(
        "SELECT x.region FROM customers x WHERE EXISTS "
        "(SELECT x.id FROM orders x WHERE x.amount >= :minimum)",
        "postgresql",
        golden_suite.context.definition,
    )
    assert facts.resources == {"customers", "orders"}
    assert {"customers.region", "orders.id", "orders.amount"} <= facts.fields
    assert "orders.region" not in facts.fields
    assert "customers.amount" not in facts.fields


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT SUM(amount) FROM orders WHERE NOT (status = 'COMPLETED')",
        "SELECT SUM(amount) FROM orders WHERE status = 'COMPLETED' OR amount > 0",
        "SELECT SUM(amount) FROM orders WHERE status = 'COMPLETED' OR TRUE",
        "SELECT CASE WHEN status = 'COMPLETED' THEN amount ELSE 0 END FROM orders",
        "SELECT SUM(amount) FROM orders WHERE COALESCE(status, 'COMPLETED') = 'COMPLETED'",
    ],
)
def test_missing_or_bypassed_rule_is_not_a_pass(golden_suite, statement):
    expect = Expectations(
        status="ok",
        constraints=[ConstraintExpectation(field="orders.status", op="=", value="COMPLETED")],
    )
    assert "constraints" in {f.expectation for f in score(golden_suite, expect, statement)}


@pytest.mark.parametrize(
    "condition",
    [
        "status <> 'CANCELLED'",
        "NOT (status = 'CANCELLED')",
        "status NOT IN ('CANCELLED')",
        "(status <> 'CANCELLED' AND amount > 10) OR (status <> 'CANCELLED' AND amount < 0)",
        "NOT (status = 'CANCELLED' OR amount < 0)",
    ],
)
def test_equivalent_negation_and_common_or_predicates(golden_suite, condition):
    expect = Expectations(
        status="ok",
        constraints=[
            ConstraintExpectation(field="orders.status", op=["<>", "NOT IN"], value="CANCELLED")
        ],
    )
    assert score(golden_suite, expect, f"SELECT COUNT(*) FROM orders WHERE {condition}") == []


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT SUM(o.amount) FROM orders o JOIN customers c ON o.customer_id = c.id",
        "SELECT SUM(o.amount) FROM orders o, customers c WHERE c.id = o.customer_id",
        "WITH t AS (SELECT customer_id, amount FROM orders) "
        "SELECT SUM(t.amount) FROM t JOIN customers c ON c.id = t.customer_id",
    ],
)
def test_join_direction_and_direct_cte_lineage(golden_suite, statement):
    expect = Expectations(status="ok", joins=[("orders.customer_id", "customers.id")])
    assert score(golden_suite, expect, statement) == []


@pytest.mark.parametrize(
    "condition",
    [
        "o.id = c.id",
        "o.customer_id != c.id",
        "NOT (o.customer_id = c.id)",
        "o.customer_id = c.id OR TRUE",
        "o.customer_id + 1 = c.id",
    ],
)
def test_incorrect_join_fails(golden_suite, condition):
    expect = Expectations(status="ok", joins=[("orders.customer_id", "customers.id")])
    assert "joins" in {
        f.expectation
        for f in score(
            golden_suite,
            expect,
            f"SELECT o.amount FROM orders o JOIN customers c ON {condition}",
        )
    }


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT id FROM orders WHERE amount >= :minimum",
        "SELECT id FROM orders WHERE :minimum <= amount",
        "SELECT id FROM orders WHERE CAST(amount AS NUMERIC) >= CAST(:minimum AS NUMERIC)",
        "SELECT id FROM orders WHERE amount BETWEEN :minimum AND :maximum",
    ],
)
def test_reversed_cast_and_between_comparisons(golden_suite, statement):
    expect = Expectations(
        status="ok",
        constraints=[ConstraintExpectation(field="orders.amount", op=">=", parameter_value=100000)],
    )
    parameters, types = {"minimum": 100000}, {"minimum": "number"}
    if ":maximum" in statement:
        parameters["maximum"], types["maximum"] = 200000, "number"
    assert score(golden_suite, expect, statement, parameters, types) == []


def test_date_values_compare_as_context_timezone_instants(golden_suite):
    expect = Expectations(
        status="ok",
        constraints=[
            ConstraintExpectation(
                field="orders.paid_at", op=">=", parameter_value="2026-10-01T00:00:00+09:00"
            )
        ],
        parameters=[ParameterExpectation(value="2026-10-01T00:00:00+09:00")],
    )
    for value, kind in [("2026-10-01", "date"), ("2026-09-30T15:00:00Z", "date-time")]:
        assert (
            score(
                golden_suite,
                expect,
                "SELECT id FROM orders WHERE paid_at >= :start",
                {"start": value},
                {"start": kind},
            )
            == []
        )
    failures = score(
        golden_suite,
        expect,
        "SELECT id FROM orders WHERE paid_at >= :start",
        {"start": "2026-10-01T00:00:00Z"},
        {"start": "date-time"},
    )
    assert {f.expectation for f in failures} == {"constraints", "parameters"}


@pytest.mark.parametrize(
    ("case_id", "replacement", "target", "expected_failure"),
    [
        ("total_revenue", "status = 'COMPLETED'", "status = 'PENDING'", "constraints"),
        ("revenue_by_region", "c.id = o.customer_id", "c.id = o.id", "joins"),
        ("revenue_yesterday_uses_paid_at", "paid_at", "ordered_at", "forbidden_fields"),
        ("order_count_last_week_uses_ordered_at", "ordered_at", "paid_at", "forbidden_fields"),
        ("customer_list", "name, region", "name", "fields"),
    ],
)
def test_golden_mutations_fail(golden_suite, case_id, replacement, target, expected_failure):
    case = next(c for c in golden_suite.cases if c.id == case_id)
    op = SqlOperation(dialect="postgresql", **case.reference.model_dump())
    op.statement = op.statement.replace(replacement, target)
    failures = score_operation(
        case.expect, op, golden_suite.context.definition, golden_suite.context.timezone
    )
    assert expected_failure in {f.expectation for f in failures}


def test_parameters_are_value_based_and_rule_constants_stay_literals(golden_suite):
    case = next(c for c in golden_suite.cases if c.id == "busan_revenue")
    reference = case.reference
    assert (
        score(
            golden_suite,
            case.expect,
            reference.statement.replace(":region", ":area"),
            {"area": "부산"},
            {"area": "string"},
        )
        == []
    )
    failures = score(
        golden_suite,
        case.expect,
        reference.statement.replace(":region", "'부산'").replace("'COMPLETED'", ":status"),
        {"status": "COMPLETED"},
        {"status": "string"},
    )
    assert {"constraints", "parameters", "forbidden_literals", "forbidden_parameter_values"} <= {
        f.expectation for f in failures
    }


def test_expected_type_and_large_numeric_values_are_not_coerced(golden_suite):
    expect = Expectations(
        status="ok", parameters=[ParameterExpectation(value=9007199254740993, type="integer")]
    )
    failures = score(
        golden_suite,
        expect,
        "SELECT id FROM orders WHERE id = :value",
        {"value": 9007199254740992},
        {"value": "integer"},
    )
    assert [f.expectation for f in failures] == ["parameters"]
    expect.parameters = [ParameterExpectation(value=100000, type="integer")]
    failures = score(
        golden_suite,
        expect,
        "SELECT id FROM orders WHERE amount >= :value",
        {"value": 100000},
        {"value": "number"},
    )
    assert [f.expectation for f in failures] == ["parameters"]


@pytest.mark.parametrize("statement", ["SELECT * FROM orders", "SELECT o.* FROM orders o"])
def test_projection_stars_are_detected(golden_suite, statement):
    expect = Expectations(status="ok", forbidden_constructs=["select_star"])
    assert "forbidden_constructs" in {f.expectation for f in score(golden_suite, expect, statement)}
    assert score(golden_suite, expect, "SELECT COUNT(*) FROM orders") == []


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT id FROM orders WHERE paid_at >= NOW()",
        "SELECT id FROM orders WHERE paid_at >= 'today'::date",
        "SELECT id FROM orders WHERE paid_at >= CURRENT_DATE",
    ],
)
def test_database_clock_is_invalid_for_generate(golden_suite, statement):
    expect = Expectations(status="ok", forbidden_constructs=["relative_time_function"])
    failures = score(golden_suite, expect, statement)
    assert {"validity", "forbidden_constructs"} <= {f.expectation for f in failures}


@pytest.mark.parametrize(
    "statement",
    ["", "SELECT FROM", "DELETE FROM orders", "SELECT id FROM orders; SELECT id FROM customers"],
)
def test_invalid_sql_does_not_crash_the_evaluator(golden_suite, statement):
    assert "validity" in {
        f.expectation for f in score(golden_suite, Expectations(status="ok"), statement)
    }


def test_parameter_contract_is_rechecked(golden_suite):
    expect = Expectations(
        status="ok",
        constraints=[ConstraintExpectation(field="orders.amount", op=">=", parameter=True)],
    )
    failures = score(golden_suite, expect, "SELECT id FROM orders WHERE amount >= :missing")
    assert {"validity", "constraints"} <= {f.expectation for f in failures}


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) FROM orders",
        "SELECT COUNT(1) FROM orders",
        "SELECT COUNT(id) FROM orders",
    ],
)
def test_equivalent_row_counts(golden_suite, statement):
    expect = Expectations(
        status="ok", aggregates=[AggregateExpectation(function="count", field="*")]
    )
    assert score(golden_suite, expect, statement) == []


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(amount) FROM orders",
        "SELECT COUNT(DISTINCT id) FROM orders",
        "SELECT COUNT(*) OVER () FROM orders",
    ],
)
def test_other_counts_do_not_satisfy_row_count(golden_suite, statement):
    expect = Expectations(
        status="ok", aggregates=[AggregateExpectation(function="count", field="*")]
    )
    assert "aggregates" in {f.expectation for f in score(golden_suite, expect, statement)}


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(c.id) FROM orders o LEFT JOIN customers c ON c.id = o.customer_id",
        "SELECT COUNT(o.id) FROM customers c RIGHT JOIN orders o ON c.id = o.customer_id",
    ],
)
def test_outer_join_count_does_not_assume_schema_non_null(golden_suite, statement):
    expect = Expectations(
        status="ok", aggregates=[AggregateExpectation(function="count", field="*")]
    )
    assert "aggregates" in {f.expectation for f in score(golden_suite, expect, statement)}


def test_revenue_requires_sum_and_region_grouping(golden_suite):
    case = next(c for c in golden_suite.cases if c.id == "revenue_by_region")
    for replacement in ("COUNT", "AVG"):
        failures = score(
            golden_suite, case.expect, case.reference.statement.replace("SUM", replacement)
        )
        assert "aggregates" in {f.expectation for f in failures}
    failures = score(
        golden_suite,
        case.expect,
        case.reference.statement.replace("c.region, ", "").replace(" GROUP BY c.region", ""),
    )
    assert "group_by" in {f.expectation for f in failures}


def test_incorrect_relative_time_boundary_fails(golden_suite):
    case = next(c for c in golden_suite.cases if c.id == "revenue_last_30_days")
    args = case.reference.parameters | {"start_at": "2026-09-01T09:00:00+09:00"}
    failures = score(
        golden_suite, case.expect, case.reference.statement, args, case.reference.parameter_types
    )
    assert "constraints" in {f.expectation for f in failures}
    failures = score(
        golden_suite,
        case.expect,
        case.reference.statement.replace("< :end_at", "<= :end_at"),
        case.reference.parameters,
        case.reference.parameter_types,
    )
    assert "constraints" in {f.expectation for f in failures}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"op": []},
        {"op": "LIKE"},
        {"op": "=", "value": "COMPLETED", "parameter": True},
        {"op": "=", "value": 1, "parameter_value": 1},
    ],
)
def test_inconsistent_constraint_expectations_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        ConstraintExpectation(field="orders.status", **kwargs)


def test_loader_rejects_empty_duplicate_and_invalid_context(tmp_path, golden_suite):
    import json

    (tmp_path / "context.json").write_text(golden_suite.context.model_dump_json(by_alias=True))
    (tmp_path / "cases.yaml").write_text('reference_time: "2026-10-02T09:00:00+09:00"\ncases: []\n')
    with pytest.raises(ValidationError):
        load_suite(tmp_path)
    (tmp_path / "cases.yaml").write_text(
        'reference_time: "2026-10-02T09:00:00+09:00"\ncases:\n'
        "  - {id: a, question: q, category: sample, expect: {status: ambiguous}}\n"
        "  - {id: a, question: q, category: sample, expect: {status: ambiguous}}\n"
    )
    with pytest.raises(ValueError, match="Duplicate case ids"):
        load_suite(tmp_path)
    body = copy.deepcopy(golden_suite.context.model_dump(mode="json", by_alias=True))
    body["timezone"] = "invalid/timezone"
    (tmp_path / "context.json").write_text(json.dumps(body))
    with pytest.raises(ValidationError, match="timezone"):
        load_suite(tmp_path)


def _all_suite_references():
    import os
    from pathlib import Path

    root = Path(os.environ.get("SIDEWIT_EVALS_DIR", Path(__file__).parents[2] / "evals"))
    for directory in sorted(p for p in root.iterdir() if (p / "cases.yaml").exists()):
        suite = load_suite(directory)
        for case in suite.cases:
            if case.reference is not None:
                yield pytest.param(suite, case, id=f"{suite.name}:{case.id}")


@pytest.mark.parametrize(("suite", "case"), list(_all_suite_references()))
def test_every_suite_reference_satisfies_its_expectations(suite, case):
    """Proves each authored case is satisfiable, in every suite (dev and held-out)."""
    op = SqlOperation(dialect=suite.context.dialect, **case.reference.model_dump())
    failures = score_operation(case.expect, op, suite.context.definition, suite.context.timezone)
    assert failures == []


@pytest.mark.parametrize(
    ("condition", "op"),
    [
        ("state IS DISTINCT FROM 'VOID'", "!="),
        ("NOT state IS NOT DISTINCT FROM 'VOID'", "!="),
        ("state IS NOT DISTINCT FROM 'VOID'", "="),
    ],
)
def test_null_safe_comparisons_count_as_constraints(condition, op):
    from sidewit.domain.context import ContextDefinition

    context = ContextDefinition.model_validate(
        {"resources": [{"name": "rentals", "fields": [{"name": "state", "type": "text"}]}]}
    )
    facts = analyze(f"SELECT COUNT(*) FROM rentals WHERE {condition}", "postgresql", context)
    assert [(c.field, c.op, c.literals) for c in facts.comparisons] == [
        ("rentals.state", op, ("VOID",))
    ]
