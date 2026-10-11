import pytest

from sidewit.domain.context import ContextDefinition
from sidewit.validation.sql_validator import (
    DEFAULT_POLICY,
    GENERATE_POLICY,
    validate_sql_operation,
)

CONTEXT = ContextDefinition.model_validate(
    {
        "resources": [
            {
                "name": "customers",
                "fields": [
                    {"name": "id", "type": "bigint"},
                    {"name": "region", "type": "text"},
                    {"name": "name", "type": "text"},
                ],
            },
            {
                "name": "orders",
                "fields": [
                    {"name": "id", "type": "bigint"},
                    {"name": "customer_id", "type": "bigint"},
                    {"name": "amount", "type": "numeric"},
                    {"name": "status", "type": "text"},
                    {"name": "created_at", "type": "timestamptz"},
                ],
            },
            {
                "name": "payments",
                "fields": [
                    {"name": "id", "type": "bigint"},
                    {"name": "order_id", "type": "bigint"},
                    {"name": "amount", "type": "numeric"},
                ],
            },
        ],
        "relations": [
            {"from": "orders.customer_id", "to": "customers.id"},
            {"from": "payments.order_id", "to": "orders.id"},
        ],
        "rules": [{"id": "rule_1", "text": "Revenue only includes COMPLETED orders."}],
    }
)

ALL_CHECKS = [
    "syntax",
    "statement_policy",
    "resources",
    "fields",
    "relations",
    "parameters",
    "placeholder_safety",
]


def validate(sql, params=None, values=None, policy=DEFAULT_POLICY):
    return validate_sql_operation(sql, "postgresql", CONTEXT, params or {}, values, policy)


def error_codes(result):
    return [e.code for e in result.errors]


def assert_valid(result):
    assert result.valid, result.errors
    assert result.errors == []


def assert_only_error(result, code, ref=None):
    assert not result.valid
    assert error_codes(result) == [code], result.errors
    if ref is not None:
        assert result.errors[0].ref == ref


# valid operations -----------------------------------------------------------------------


def test_typical_parameterized_query_is_valid():
    result = validate(
        """
        SELECT c.region, SUM(o.amount) AS revenue
        FROM orders o
        JOIN customers c ON c.id = o.customer_id
        WHERE c.region = :region
          AND o.created_at >= :start_date
          AND o.status = 'COMPLETED'
        GROUP BY c.region
        ORDER BY revenue DESC
        """,
        {"region": "string", "start_date": "date-time"},
        {"region": "서울", "start_date": "2026-09-02T00:00:00+09:00"},
    )
    assert_valid(result)
    assert result.warnings == []
    assert result.checks == ALL_CHECKS


def test_business_rules_are_not_checked_structurally():
    """The context says revenue only includes COMPLETED orders. Omitting that filter is a
    semantic error, which structural validation does not and must not claim to catch."""
    assert_valid(validate("SELECT SUM(amount) FROM orders"))


@pytest.mark.parametrize(
    "sql",
    [
        "WITH totals AS (SELECT customer_id, SUM(amount) AS total FROM orders "
        "GROUP BY customer_id) SELECT t.total, c.region FROM totals t "
        "JOIN customers c ON c.id = t.customer_id",
        "SELECT sub.n FROM (SELECT id AS n FROM orders) AS sub",
        "SELECT id FROM orders UNION SELECT id FROM payments",
        "(SELECT id FROM orders)",
        "SELECT * FROM customers c WHERE EXISTS "
        "(SELECT 1 FROM orders o WHERE o.customer_id = c.id)",
        "SELECT c.id FROM customers c WHERE c.id IN (SELECT customer_id FROM orders)",
        "SELECT region AS r, COUNT(*) FROM customers GROUP BY r ORDER BY r",
        "SELECT o.* FROM orders o",
        "SELECT g FROM generate_series(1, 3) AS g",
        "SELECT ORDERS.Amount FROM Orders",
        "SELECT created_at::date AS day FROM orders WHERE status = 'X'",
        "SELECT * FROM orders WHERE created_at >= NOW() - :days * INTERVAL '1 day'",
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 5) "
        "SELECT n FROM t",
        "SELECT v.x FROM (VALUES (1), (2)) AS v(x)",
        "SELECT o.id, l.k FROM orders o, LATERAL (SELECT o.amount AS k) l",
        "SELECT id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at) FROM orders",
        "SELECT c.id, (SELECT SUM(o.amount) FROM orders o WHERE o.customer_id = c.id) AS total "
        "FROM customers c",
        "SELECT id FROM customers c WHERE EXISTS "
        "(SELECT 1 FROM orders WHERE customer_id = c.id AND region = 'x')",
        "SELECT EXTRACT(YEAR FROM created_at) AS y, COUNT(*) FROM orders GROUP BY y",
        "SELECT COUNT(*) FILTER (WHERE status = 'X') FROM orders",
    ],
)
def test_valid_query_shapes(sql):
    params = {"days": "integer"} if ":days" in sql else {}
    assert_valid(validate(sql, params))


# syntax ---------------------------------------------------------------------------------


def test_sql_parse_error():
    result = validate("SELEC amount FROM orders")
    assert_only_error(result, "sql_parse_error")
    assert result.checks == ["syntax"]


def test_empty_statement_is_parse_error():
    assert_only_error(validate("  ;  "), "sql_parse_error")


def test_multiple_statements():
    result = validate("SELECT id FROM orders; SELECT id FROM customers")
    assert_only_error(result, "multiple_statements")
    assert result.checks == ["syntax"]


# statement_policy -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders (id) VALUES (1)",
        "UPDATE orders SET amount = 0",
        "DELETE FROM orders",
        "DROP TABLE orders",
        "CREATE TABLE x AS SELECT id FROM orders",
        "TRUNCATE orders",
        "VACUUM orders",
    ],
)
def test_non_select_statements_not_allowed(sql):
    result = validate(sql)
    assert_only_error(result, "statement_not_allowed")
    assert result.checks == ["syntax", "statement_policy"]


@pytest.mark.parametrize(
    "sql",
    [
        "WITH d AS (DELETE FROM orders RETURNING id) SELECT id FROM d",
        "SELECT id INTO backup FROM orders",
        "SELECT id FROM orders FOR UPDATE",
        "SELECT id FROM orders FOR SHARE",
    ],
)
def test_select_with_side_effects_not_allowed(sql):
    assert "statement_not_allowed" in error_codes(validate(sql))


# resources ------------------------------------------------------------------------------


def test_unknown_resource():
    assert_only_error(validate("SELECT id FROM refunds"), "unknown_resource", "refunds")


def test_unknown_resource_in_join_and_subquery():
    result = validate(
        "SELECT o.id FROM orders o JOIN refunds r ON r.order_id = o.id "
        "WHERE o.id IN (SELECT order_id FROM chargebacks)"
    )
    assert error_codes(result) == ["unknown_resource", "unknown_resource"]
    assert {e.ref for e in result.errors} == {"refunds", "chargebacks"}


def test_schema_qualified_resource_is_rejected():
    assert_only_error(validate("SELECT id FROM public.orders"), "unknown_resource")


def test_undefined_table_alias_is_unknown_reference():
    assert_only_error(validate("SELECT x.id FROM orders o"), "unknown_resource", "x")


def test_cte_names_are_not_resources():
    assert_valid(validate("WITH refunds AS (SELECT id FROM orders) SELECT id FROM refunds"))


# fields ---------------------------------------------------------------------------------


def test_unknown_qualified_field():
    assert_only_error(validate("SELECT o.total FROM orders o"), "unknown_field", "orders.total")


def test_unknown_unqualified_field():
    assert_only_error(validate("SELECT total FROM orders"), "unknown_field", "total")


def test_unknown_field_in_where_join_and_order_by():
    result = validate(
        "SELECT o.id FROM orders o JOIN customers c ON c.uid = o.customer_id "
        "WHERE o.deleted = false ORDER BY o.rank"
    )
    assert error_codes(result) == ["unknown_field"] * 3
    assert {e.ref for e in result.errors} == {"customers.uid", "orders.deleted", "orders.rank"}


def test_unknown_field_on_cte_and_derived_table():
    result = validate(
        "WITH t AS (SELECT customer_id FROM orders) "
        "SELECT t.amount, d.missing FROM t, (SELECT id FROM orders) d"
    )
    assert error_codes(result) == ["unknown_field", "unknown_field"]
    assert {e.ref for e in result.errors} == {"t.amount", "d.missing"}


def test_unknown_field_inside_correlated_subquery():
    result = validate(
        "SELECT c.id FROM customers c WHERE EXISTS "
        "(SELECT 1 FROM orders o WHERE o.customer_id = c.nope)"
    )
    assert_only_error(result, "unknown_field", "customers.nope")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders ORDER BY bogus",
        "SELECT id FROM orders GROUP BY bogus",
        "SELECT customer_id, SUM(amount) AS bogus FROM orders GROUP BY customer_id "
        "HAVING bogus > 1",
        "SELECT amount AS bogus FROM orders WHERE bogus > 1",
        "SELECT amount AS bogus, RANK() OVER (ORDER BY bogus) FROM orders",
    ],
)
def test_unknown_field_in_clauses_where_output_aliases_do_not_resolve(sql):
    """PostgreSQL resolves output aliases only in GROUP BY/ORDER BY of the same SELECT."""
    assert_only_error(validate(sql), "unknown_field", "bogus")


def test_unknown_resource_does_not_cascade_into_field_errors():
    assert error_codes(validate("SELECT r.anything FROM refunds r")) == ["unknown_resource"]


# ambiguous_field ------------------------------------------------------------------------


def test_ambiguous_unqualified_field():
    result = validate("SELECT amount FROM orders o JOIN payments p ON p.order_id = o.id")
    assert_only_error(result, "ambiguous_field", "amount")


def test_visible_but_unselected_cte_does_not_make_field_ambiguous():
    sql = (
        "WITH recent AS (SELECT id FROM orders) "
        "SELECT id FROM recent WHERE id IN (SELECT order_id FROM payments WHERE id > 1)"
    )
    assert_valid(validate(sql))


def test_cte_not_in_from_is_not_resolvable():
    sql = "WITH recent AS (SELECT id FROM orders) SELECT recent.id FROM payments"
    assert_only_error(validate(sql), "unknown_resource", "recent")


def test_ambiguous_field_with_join_using():
    result = validate("SELECT amount FROM orders o JOIN payments p USING (id)")
    assert_only_error(result, "ambiguous_field", "amount")


def test_qualified_field_is_not_ambiguous():
    assert_valid(validate("SELECT p.amount FROM orders o JOIN payments p ON p.order_id = o.id"))


# undeclared_relation (warning) ----------------------------------------------------------


def test_undeclared_relation_is_warning_not_error():
    result = validate("SELECT c.id FROM customers c JOIN payments p ON p.order_id = c.id")
    assert result.valid
    assert [(w.code, w.ref) for w in result.warnings] == [
        ("undeclared_relation", "payments.order_id = customers.id")
    ]


def test_declared_relation_in_either_direction_has_no_warning():
    for on in ("o.customer_id = c.id", "c.id = o.customer_id"):
        result = validate(f"SELECT o.id FROM orders o JOIN customers c ON {on}")
        assert result.warnings == []


# unsupported_placeholder_style ----------------------------------------------------------


@pytest.mark.parametrize(
    "placeholder",
    ["?", "$1", "%s", "%(region)s", ":Region", ":_x"],
)
def test_unsupported_placeholder_style(placeholder):
    result = validate(f"SELECT id FROM customers WHERE region = {placeholder}")
    assert "unsupported_placeholder_style" in error_codes(result), result.errors


def test_jsonb_question_mark_operator_is_not_a_placeholder():
    context = ContextDefinition.model_validate(
        {"resources": [{"name": "events", "fields": [{"name": "data", "type": "jsonb"}]}]}
    )
    result = validate_sql_operation(
        "SELECT data FROM events WHERE data ? 'key'", "postgresql", context, {}
    )
    assert result.valid, result.errors


# placeholder_in_literal -----------------------------------------------------------------


def test_placeholder_in_literal():
    result = validate(
        "SELECT id FROM orders WHERE created_at >= NOW() - INTERVAL ':days days'",
        {"days": "integer"},
    )
    assert error_codes(result) == ["unused_parameter", "placeholder_in_literal"]
    assert result.errors[1].ref == "days"


@pytest.mark.parametrize("literal", ["env:prod", "10:30:00", "a::b", "http://x", "key: value"])
def test_colons_in_ordinary_literals_are_not_placeholders(literal):
    assert_valid(validate(f"SELECT id FROM orders WHERE status = '{literal}'"))


# undeclared_parameter / unused_parameter ------------------------------------------------


def test_undeclared_parameter():
    result = validate("SELECT id FROM customers WHERE region = :region")
    assert_only_error(result, "undeclared_parameter", "region")


def test_unused_parameter():
    result = validate("SELECT id FROM customers", {"region": "string"})
    assert_only_error(result, "unused_parameter", "region")


def test_repeated_placeholder_counts_once():
    sql = "SELECT id FROM customers WHERE region = :r OR name = :r"
    assert_valid(validate(sql, {"r": "string"}, {"r": "서울"}))


def test_value_for_undeclared_parameter():
    result = validate(
        "SELECT id FROM customers WHERE region = :region",
        {"region": "string"},
        {"region": "서울", "extra": 1},
    )
    assert_only_error(result, "undeclared_parameter", "extra")


# parameter_type_mismatch ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("param_type", "good", "bad"),
    [
        ("string", "서울", 1),
        ("integer", 30, "30"),
        ("integer", 30, True),
        ("integer", 30, 1.5),
        ("number", 1.5, "1.5"),
        ("number", 2, False),
        ("boolean", True, 1),
        ("date", "2026-09-02", "2026-09-02T00:00:00+09:00"),
        ("date", "2026-09-02", "2026-02-30"),
        ("date", "2026-09-02", "20260902"),
        ("date-time", "2026-09-02T13:30:00+09:00", "2026-09-02T13:30:00"),
        ("date-time", "2026-09-02T04:30:00Z", "2026-09-02"),
        ("date-time", "2026-09-02T04:30:00Z", "yesterday"),
    ],
)
def test_parameter_type_mismatch(param_type, good, bad):
    sql = "SELECT id FROM orders WHERE amount = :p"
    assert_valid(validate(sql, {"p": param_type}, {"p": good}))
    assert_only_error(validate(sql, {"p": param_type}, {"p": bad}), "parameter_type_mismatch", "p")


def test_missing_value_is_type_mismatch():
    result = validate("SELECT id FROM orders WHERE amount = :p", {"p": "number"}, {})
    assert_only_error(result, "parameter_type_mismatch", "p")


def test_values_are_optional_for_tool_templates():
    result = validate("SELECT id FROM orders WHERE amount = :p", {"p": "number"})
    assert_valid(result)


# result contract ------------------------------------------------------------------------


def test_errors_are_deduplicated():
    result = validate("SELECT o.total, o.total FROM orders o WHERE o.total > 0")
    assert_only_error(result, "unknown_field", "orders.total")


def test_validator_reports_and_does_not_rewrite():
    sql = "SELECT total FROM orders"
    result = validate(sql)
    assert set(result.model_dump()) == {"valid", "errors", "warnings", "checks"}


# relative_time_function (Generate policy) -----------------------------------------------

RELATIVE_TIME_EXPRESSIONS = [
    "NOW()",
    "now()",
    "CURRENT_DATE",
    "CURRENT_TIME",
    "CURRENT_TIMESTAMP",
    "CURRENT_TIMESTAMP(0)",
    "LOCALTIME",
    "LOCALTIMESTAMP",
    "clock_timestamp()",
    "statement_timestamp()",
    "transaction_timestamp()",
    "timeofday()::timestamptz",
    "'now'::timestamptz",
    "'today'::date",
    "DATE 'yesterday'",
    "CAST('tomorrow' AS timestamp)",
]


@pytest.mark.parametrize("expression", RELATIVE_TIME_EXPRESSIONS)
def test_relative_time_function_rejected_under_generate_policy(expression):
    sql = f"SELECT id FROM orders WHERE created_at >= {expression} - INTERVAL '30 days'"
    result = validate(sql, policy=GENERATE_POLICY)
    assert_only_error(result, "relative_time_function")
    assert result.checks[-1] == "relative_time"


@pytest.mark.parametrize("expression", RELATIVE_TIME_EXPRESSIONS)
def test_relative_time_allowed_by_default_policy(expression):
    sql = f"SELECT id FROM orders WHERE created_at >= {expression} - INTERVAL '30 days'"
    result = validate(sql)
    assert_valid(result)
    assert "relative_time" not in result.checks


def test_single_argument_age_is_relative_but_two_argument_age_is_not():
    one = validate("SELECT age(created_at) FROM orders", policy=GENERATE_POLICY)
    assert_only_error(one, "relative_time_function")
    two = validate(
        "SELECT age(:as_of, created_at) FROM orders",
        {"as_of": "date-time"},
        policy=GENERATE_POLICY,
    )
    assert_valid(two)


def test_relative_time_found_in_subqueries_and_ctes():
    sql = (
        "WITH recent AS (SELECT id FROM orders WHERE created_at > CURRENT_DATE) "
        "SELECT id FROM recent WHERE id IN (SELECT order_id FROM payments "
        "WHERE id > EXTRACT(EPOCH FROM NOW()))"
    )
    result = validate(sql, policy=GENERATE_POLICY)
    assert error_codes(result) == ["relative_time_function", "relative_time_function"]


def test_parameterized_absolute_period_passes_generate_policy():
    result = validate(
        "SELECT SUM(amount) FROM orders WHERE created_at >= :start_date "
        "AND created_at < :end_date AND status = 'COMPLETED'",
        {"start_date": "date-time", "end_date": "date-time"},
        {"start_date": "2026-09-02T00:00:00+09:00", "end_date": "2026-10-02T00:00:00+09:00"},
        policy=GENERATE_POLICY,
    )
    assert_valid(result)


@pytest.mark.parametrize("literal", ["now", "today"])
def test_special_time_words_in_plain_strings_are_not_relative(literal):
    result = validate(f"SELECT id FROM orders WHERE status = '{literal}'", policy=GENERATE_POLICY)
    assert_valid(result)


def test_random_is_out_of_scope_for_relative_time_policy():
    assert_valid(validate("SELECT id FROM orders ORDER BY random()", policy=GENERATE_POLICY))
