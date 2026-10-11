"""Stored names follow the database's identifier rules, and SQL built from the Context resolves
to the same objects (PostgreSQL folds unquoted identifiers to lower case)."""

import copy

import pytest
import sqlglot

from sidewit.ddl.importer import import_ddl
from sidewit.domain.context import ContextDefinition, definition_issues
from sidewit.domain.dialect import identifier, render_sql
from sidewit.domain.metric import MetricBinding, MetricDefinition
from sidewit.generation.prompt import render_context
from sidewit.validation.metric_contract import binding_issues, metric_template
from tests.test_metric_contracts import DEFINITION


def imported(ddl, existing=None, mode="replace"):
    return import_ddl(existing or ContextDefinition(), ddl, "postgresql", mode).definition


def names(definition):
    return [(r.name, [f.name for f in r.fields]) for r in definition.resources]


def test_unquoted_identifiers_are_stored_case_folded_and_quoted_ones_verbatim():
    d = imported('CREATE TABLE Orders (Id bigint PRIMARY KEY, "MixedCase" int, PaidAt date);')
    assert names(d) == [("orders", ["id", "MixedCase", "paidat"])]
    d = imported('CREATE TABLE "Orders" (id int);')
    assert names(d) == [("Orders", ["id"])]


def test_foreign_keys_and_primary_keys_use_stored_names():
    d = imported(
        "CREATE TABLE Customers (ID int PRIMARY KEY);"
        "CREATE TABLE Orders (ID int, CustomerID int REFERENCES Customers(ID), PRIMARY KEY (ID));"
    )
    assert [(r.from_, r.to) for r in d.relations] == [("orders.customerid", "customers.id")]
    assert all(f.primary_key for r in d.resources for f in r.fields if f.name == "id")


def test_merge_adopts_the_database_name_and_keeps_descriptions():
    existing = ContextDefinition.model_validate(
        {
            "resources": [
                {
                    "name": "Orders",
                    "description": "주문",
                    "fields": [{"name": "Amount", "type": "int", "description": "금액"}],
                }
            ]
        }
    )
    d = imported("CREATE TABLE Orders (Amount numeric);", existing, "merge")
    assert names(d) == [("orders", ["amount"])]
    assert d.resources[0].description == "주문"
    assert d.resources[0].fields[0].description == "금액"


@pytest.mark.parametrize(
    ("name", "sql"),
    [
        ("orders", "orders"),
        ("Orders", '"Orders"'),
        ("order", '"order"'),
        ("user", '"user"'),
        ("my-table", '"my-table"'),
        ("amount_2", "amount_2"),
    ],
)
def test_identifiers_are_quoted_only_when_needed(name, sql):
    assert identifier(name, "postgresql").sql(dialect="postgres") == sql


def test_render_sql_uses_canonical_named_placeholders():
    query = sqlglot.parse_one("SELECT 1 FROM t WHERE a = :a AND b < :b_2", read="postgres")
    assert render_sql(query, "postgresql") == "SELECT 1 FROM t WHERE a = :a AND b < :b_2"


def metric_definition(ddl):
    d = imported(ddl)
    metric = MetricDefinition.model_validate(
        {
            "id": "revenue",
            "description": "Revenue",
            "resource": d.resources[0].name,
            "aggregate": {"kind": "sum", "field": f"{d.resources[0].name}.amount"},
            "time_fields": [f"{d.resources[0].name}.paidat"],
            "default_time_field": f"{d.resources[0].name}.paidat",
        }
    )
    return d.model_copy(update={"metrics": [metric]})


def test_metric_sql_from_unquoted_ddl_resolves_in_postgres():
    d = metric_definition("CREATE TABLE Orders (Id bigint, Amount numeric, PaidAt timestamptz);")
    assert definition_issues(d) == []
    sql, _ = metric_template(d, d.metrics[0], "orders.paidat", "postgresql")
    assert sql == (
        "SELECT SUM(orders.amount) AS value FROM orders "
        "WHERE orders.paidat >= :start_at AND orders.paidat < :end_at"
    )


def test_metric_sql_quotes_case_sensitive_tables():
    d = metric_definition('CREATE TABLE "Orders" (amount numeric, paidat date);')
    sql, _ = metric_template(d, d.metrics[0], None, "postgresql")
    assert sql == 'SELECT SUM("Orders".amount) AS value FROM "Orders"'


def test_tools_saved_with_fully_quoted_sql_still_match_their_contract():
    """Tools created before minimal quoting stored `"orders"."id"`-style SQL."""
    d = ContextDefinition.model_validate(copy.deepcopy(DEFINITION))
    binding = MetricBinding(metric_id="regional_revenue", time_field="orders.paid_at")
    sql, types = metric_template(d, d.metrics[1], "orders.paid_at", "postgresql")
    legacy = sqlglot.parse_one(sql, read="postgres").sql(dialect="postgres", identify=True)
    legacy = legacy.replace("%(region)s", ":region")
    legacy = legacy.replace("%(start_at)s", ":start_at").replace("%(end_at)s", ":end_at")
    assert '"orders"."amount"' in legacy
    assert binding_issues(d, binding, legacy, types, "postgresql") == []
    # A different object is still a mismatch.
    renamed = legacy.replace('"customers"', '"Customers"')
    assert binding_issues(d, binding, renamed, types, "postgresql")


def test_metric_issue_refs_point_at_the_offending_part():
    raw = copy.deepcopy(DEFINITION)
    raw["metrics"][1]["inputs"][0]["type"] = "integer"
    raw["metrics"][1]["aggregate"]["field"] = "orders.status"
    issues = definition_issues(ContextDefinition.model_validate(raw))
    assert {i.ref for i in issues} == {
        "regional_revenue.inputs.region",
        "regional_revenue.aggregate.field",
    }


def test_audit_timestamps_do_not_make_a_period_ambiguous():
    def rendered(fields):
        definition = ContextDefinition.model_validate(
            {"resources": [{"name": "tickets", "fields": fields}]}
        )
        return render_context(definition)

    audit = [
        {"name": "created_at", "type": "timestamptz"},
        {"name": "updated_at", "type": "timestamptz"},
        {"name": "deleted_at", "type": "timestamptz"},
    ]
    assert "Period choice is unresolved" not in rendered(audit)
    business = [
        {"name": "created_at", "type": "timestamptz"},
        {"name": "closed_at", "type": "timestamptz"},
    ]
    assert "Period choice is unresolved by field descriptions: created_at, closed_at" in rendered(
        business
    )
