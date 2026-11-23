"""Validator verdicts that disagreed with PostgreSQL 16."""

import pytest

from sidewit.domain.context import ContextDefinition
from sidewit.domain.params import value_matches_type
from sidewit.validation.sql_validator import (
    GENERATE_POLICY,
    generate_policy,
    validate_sql_operation,
)

DEFINITION = {
    "resources": [
        {
            "name": "orders",
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "status", "type": "text", "allowed_values": ["PAID", "CANCELLED"]},
                {"name": "region", "type": "text"},
                {"name": "amount", "type": "numeric"},
                {"name": "paid_at", "type": "timestamptz"},
                {"name": "tags", "type": "text[]"},
                {"name": "doc", "type": "jsonb"},
            ],
        },
        {
            "name": "customers",
            "fields": [{"name": "id", "type": "integer"}, {"name": "region", "type": "text"}],
        },
    ],
    "rules": [
        {
            "id": "kr",
            "text": "Korean orders only",
            "condition": {"field": "orders.region", "op": "=", "value": "KR"},
        },
        {
            "id": "big",
            "text": "Large orders",
            "condition": {"field": "orders.amount", "op": ">=", "value": 100},
        },
    ],
}


def check(sql, params=None, values=None, policy=None, definition=DEFINITION):
    context = ContextDefinition.model_validate(definition)
    kwargs = {"policy": policy} if policy is not None else {}
    return validate_sql_operation(sql, "postgresql", context, params or {}, values, **kwargs)


def codes(result):
    return {e.code for e in result.errors}


# relative time


@pytest.mark.parametrize(
    "literal", ["00:00 yesterday", "10:00yesterday", "10:00 TODAY", "today Asia/Seoul"]
)
def test_special_date_word_after_time_is_relative(literal):
    result = check(f"SELECT id FROM orders WHERE paid_at >= '{literal}'", policy=GENERATE_POLICY)
    assert "relative_time_function" in codes(result)
    cast = check(f"SELECT '{literal}'::timestamptz", policy=GENERATE_POLICY)
    assert "relative_time_function" in codes(cast)


def test_special_date_word_in_parameter_value_after_time_is_relative():
    result = check(
        "SELECT id FROM orders WHERE paid_at >= :since::timestamptz",
        {"since": "string"},
        {"since": "00:00 yesterday"},
        GENERATE_POLICY,
    )
    assert "relative_time_function" in codes(result)


@pytest.mark.parametrize("literal", ["todayx", "nowhere", "2024-01-01T00:00:00+09:00"])
def test_words_containing_special_words_are_not_relative(literal):
    result = check(f"SELECT id FROM orders WHERE paid_at >= '{literal}'", policy=GENERATE_POLICY)
    assert "relative_time_function" not in codes(result)


# registered constants


@pytest.mark.parametrize(
    "rhs", ["upper(:s)", ":s || ''", "trim(:s)", "COALESCE(:s, 'x')", "lower(:s)::text"]
)
def test_wrapped_parameter_on_constant_field_is_unverifiable(rhs):
    result = check(
        f"SELECT SUM(amount) FROM orders WHERE region = {rhs}",
        {"s": "string"},
        {"s": "KR"},
        GENERATE_POLICY,
    )
    assert "value_unverifiable" in codes(result), result.errors


def test_direct_parameter_on_constant_field_is_still_constant_as_parameter():
    result = check(
        "SELECT SUM(amount) FROM orders WHERE region = :s",
        {"s": "string"},
        {"s": "KR"},
        GENERATE_POLICY,
    )
    assert codes(result) == {"constant_as_parameter"}


def test_constant_literal_and_database_values_remain_valid():
    sql = (
        "SELECT SUM(o.amount) FROM orders o JOIN customers c ON o.region = c.region "
        "WHERE o.region = 'KR' AND o.region = upper('kr')"
    )
    assert check(sql, policy=GENERATE_POLICY).valid is False  # upper('kr') is computed text
    assert check(sql.replace(" AND o.region = upper('kr')", ""), policy=GENERATE_POLICY).valid


# comparison forms


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CASE status WHEN 'NOPE' THEN 1 END FROM orders",
        "SELECT NULLIF(status, 'NOPE') FROM orders",
        "SELECT id FROM orders WHERE (status, id) = ('NOPE', 1)",
        "SELECT id FROM orders WHERE (id, status) IN ((1, 'NOPE'))",
        # Leading row elements are compared by equality as well: (s, i) > (v, 1) has s = v.
        "SELECT id FROM orders WHERE (status, id) > ('NOPE', 1)",
        "SELECT id FROM orders WHERE status IS NOT DISTINCT FROM 'NOPE'",
    ],
)
def test_allowed_values_apply_to_every_comparison_form(sql):
    assert "value_not_allowed" in codes(check(sql)), sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CASE status WHEN 'PAID' THEN 1 END FROM orders",
        "SELECT NULLIF(status, 'PAID') FROM orders",
        "SELECT id FROM orders WHERE (status, id) = ('PAID', 1)",
        "SELECT id FROM orders WHERE (id, status) IN ((1, 'PAID'), (2, 'CANCELLED'))",
        "SELECT id FROM orders WHERE (id, status) < (1, 'P')",
    ],
)
def test_allowed_comparison_forms_remain_valid(sql):
    result = check(sql)
    assert result.valid, result.errors


def test_row_comparison_against_subquery_is_traced_by_column():
    bad = "SELECT id FROM orders WHERE (id, status) IN (SELECT 1, 'NOPE')"
    good = "SELECT id FROM orders WHERE (id, status) IN (SELECT id, status FROM orders)"
    assert "value_not_allowed" in codes(check(bad))
    assert check(good).valid


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CASE region WHEN :s THEN 1 END FROM orders",
        "SELECT id FROM orders WHERE (region, id) = (:s, 1)",
        "SELECT id FROM orders WHERE (id, region) IN ((1, :s))",
    ],
)
def test_constants_apply_to_every_comparison_form(sql):
    result = check(sql, {"s": "string"}, {"s": "KR"}, GENERATE_POLICY)
    assert "constant_as_parameter" in codes(result), result.errors


def test_row_ordering_compares_leading_elements_by_equality_too():
    # (amount, id) >= (:x, 0) is amount > :x OR (amount = :x AND id >= 0).
    result = check(
        "SELECT id FROM orders WHERE (amount, id) >= (:x, 0)",
        {"x": "number"},
        {"x": 100},
        GENERATE_POLICY,
    )
    assert "constant_as_parameter" not in codes(result)  # amount is compared by > and =
    direct = check(
        "SELECT id FROM orders WHERE amount >= :x", {"x": "number"}, {"x": 100}, GENERATE_POLICY
    )
    assert "constant_as_parameter" in codes(direct)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CASE paid_at WHEN 'today' THEN 1 END FROM orders",
        "SELECT NULLIF(paid_at, 'yesterday') FROM orders",
        "SELECT id FROM orders WHERE (paid_at, id) = ('today', 1)",
    ],
)
def test_relative_time_applies_to_every_comparison_form(sql):
    assert "relative_time_function" in codes(check(sql, policy=GENERATE_POLICY)), sql


# request literals


@pytest.mark.parametrize(
    "where",
    [
        "region = '서울'",
        "region = ANY(ARRAY['서울'])",
        "region IS NOT DISTINCT FROM '서울'",
        "region = '서울' COLLATE \"C\"",
        "region IN (VALUES ('서울'))",
        "region IN (SELECT '서울')",
        "(region, id) = ('서울', 1)",
    ],
)
def test_request_values_must_be_parameters_in_every_comparison_form(where):
    result = check(
        f"SELECT SUM(amount) FROM orders WHERE {where}", policy=generate_policy("서울 매출")
    )
    assert codes(result) == {"request_value_literal"}, result.errors


def test_request_value_in_simple_case_must_be_parameter():
    result = check(
        "SELECT SUM(CASE region WHEN '서울' THEN amount END) FROM orders",
        policy=generate_policy("서울 매출"),
    )
    assert codes(result) == {"request_value_literal"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT t.x FROM unnest(ARRAY[1, 2]) AS t(x)",
        "SELECT t FROM orders o, unnest(o.tags) AS t",
        "SELECT t.x FROM unnest(ARRAY[1]) WITH ORDINALITY AS t(x, n)",
    ],
)
def test_table_functions_under_request_policy(sql):
    result = check(sql, policy=generate_policy("서울 매출"))
    assert result.valid, result.errors


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders LIMIT 10",
        "SELECT id FROM orders OFFSET 10",
        "SELECT id FROM orders FETCH FIRST 10 ROWS ONLY",
        "SELECT id FROM (SELECT id FROM orders LIMIT 10) q",
    ],
)
def test_request_values_in_row_limits_must_be_parameters(sql):
    result = check(sql, policy=generate_policy("상위 10개 주문"))
    assert codes(result) == {"request_value_literal"}, result.errors


def test_request_literal_outside_comparisons_is_not_data():
    result = check(
        "SELECT '서울' AS label, SUM(amount) FROM orders", policy=generate_policy("서울 매출")
    )
    assert result.valid, result.errors


# function policy


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "LOWER"(region) FROM orders',
        'SELECT "AGE"(paid_at) FROM orders',
        'SELECT "JSON_AGG"(id) FROM orders',
        'SELECT "Count"(*) FROM orders',
        "SELECT median(amount) FROM orders",
        "SELECT timestamp_trunc('day', paid_at) FROM orders",
    ],
)
def test_functions_postgresql_does_not_provide_are_rejected(sql):
    assert "function_not_allowed" in codes(check(sql)), sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT truncate(amount) FROM orders",
        "SELECT if(id > 1, 1) FROM orders",
        "SELECT glob(region, 'x') FROM orders",
        'SELECT "Truncate"(amount) FROM orders',
    ],
)
def test_sqlglot_only_function_names_are_rejected(sql):
    assert "function_not_allowed" in codes(check(sql)), sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CAST(region AS varchar(10)), amount::numeric(10, 2) FROM orders",
        "SELECT paid_at::timestamp(3), paid_at::time(0), region::char(1) FROM orders",
        "SELECT COUNT(*) FILTER (WHERE id > 1) FROM orders",
        "SELECT EXISTS (SELECT 1), ARRAY(SELECT id FROM orders) FROM orders",
        "SELECT CURRENT_TIMESTAMP(0), LOCALTIME(2) FROM orders",
        "SELECT date(paid_at), uuid(region) FROM orders",
    ],
)
def test_grammar_and_type_names_with_parentheses_are_not_calls(sql):
    result = check(sql)
    assert "function_not_allowed" not in codes(result), result.errors


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT unnest(tags) FROM orders",
        "SELECT json_agg(id), jsonb_agg(id) FROM orders",
        "SELECT var_pop(amount), var_samp(amount), variance(amount) FROM orders",
        "SELECT id FROM orders WHERE tags @> ARRAY['a']",
        "SELECT id FROM orders WHERE doc @> '{\"k\": 1}'::jsonb",
        'SELECT "lower"(region), "unnest"(tags), "date_trunc"(\'day\', paid_at) FROM orders',
        "SELECT string_agg(region, ',') FROM orders",
        'SELECT a FROM orders AS "T"(a)',
        'WITH "Q"(x) AS (SELECT id FROM orders) SELECT x FROM "Q"',
    ],
)
def test_documented_postgresql_functions_are_allowed(sql):
    result = check(sql)
    assert "function_not_allowed" not in codes(result), result.errors


# unsupported syntax


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (TABLE secret) s",
        "SELECT (TABLE secret) FROM orders",
        "WITH s AS (TABLE secret) SELECT * FROM s",
    ],
)
def test_table_command_is_not_read_as_a_column(sql):
    with_table_resource = {
        **DEFINITION,
        "resources": [
            *DEFINITION["resources"],
            {"name": "table", "fields": [{"name": "x", "type": "text"}]},
        ],
    }
    for definition in (DEFINITION, with_table_resource):
        result = check(sql, definition=definition)
        assert not result.valid
        assert "sql_parse_error" in codes(result), result.errors


def test_reserved_words_after_a_dot_are_names():
    definition = {
        "resources": [
            {
                "name": "orders",
                "fields": [{"name": "order", "type": "integer"}, {"name": "user", "type": "text"}],
            }
        ]
    }
    result = check("SELECT o.order, o.user FROM orders o", definition=definition)
    assert result.valid, result.errors


def test_reserved_words_remain_usable_as_output_labels_and_quoted_names():
    result = check('SELECT id AS "table", region AS "select" FROM orders AS "where"')
    assert result.valid, result.errors


# subqueries sqlglot does not scope


UNSCOPED = "SELECT id FROM orders UNION SELECT id FROM orders LIMIT (SELECT max(id) FROM orders)"


def test_unscoped_subquery_is_not_name_checked():
    plain = {"resources": [{"name": "orders", "fields": [{"name": "id", "type": "integer"}]}]}
    result = check(UNSCOPED, definition=plain)
    assert result.valid, result.errors


def test_unscoped_subquery_fails_closed_for_value_policies():
    assert codes(check(UNSCOPED)) == {"value_unverifiable"}
    dated = (
        "SELECT id FROM orders UNION SELECT id FROM orders "
        "LIMIT (SELECT count(*) FROM orders WHERE paid_at >= '00:00 yesterday')"
    )
    plain = {"resources": DEFINITION["resources"]}
    assert "relative_time_unverifiable" in codes(
        check(dated, policy=GENERATE_POLICY, definition=plain)
    )


# date-time parameters


@pytest.mark.parametrize(
    "value",
    [
        "2024-W01-1T10:00+09:00",
        "2024-01-01T10+09:00",
        "20240101T100000+0900",
        "2024-01-01T10:00:00",
        "2024-01-01 10:00:00+09:00",
        "2024-01-01T10:00:00.1234567+09:00",
    ],
)
def test_date_time_rejects_forms_outside_the_documented_grammar(value):
    assert not value_matches_type(value, "date-time")


@pytest.mark.parametrize(
    "value",
    [
        "2024-01-01T10:00:00+09:00",
        "2024-01-01T10:00+09:00",
        "2024-01-01T10:00:00Z",
        "2024-01-01T10:00:00.123456-05:30",
        "2024-01-01t10:00:00z",
        "1890-01-01T10:00:00+08:27:52",
    ],
)
def test_date_time_accepts_absolute_iso_forms(value):
    assert value_matches_type(value, "date-time")


@pytest.mark.parametrize(
    "where",
    ["state <> :s", "NOT (state = :s)", "NOT (state = :s AND id > 0)", "NOT NOT (state <> :s)"],
)
def test_negated_comparisons_keep_their_meaning_for_registered_constants(where):
    definition = ContextDefinition.model_validate(
        {
            "resources": [
                {
                    "name": "m",
                    "fields": [{"name": "id", "type": "bigint"}, {"name": "state", "type": "text"}],
                }
            ],
            "rules": [
                {
                    "id": "available",
                    "text": "Available machines are not retired.",
                    "condition": {"field": "m.state", "op": "!=", "value": "RETIRED"},
                }
            ],
        }
    )
    result = validate_sql_operation(
        f"SELECT id FROM m WHERE {where}",
        "postgresql",
        definition,
        {"s": "string"},
        {"s": "RETIRED"},
        generate_policy("available machines"),
    )
    assert [e.code for e in result.errors] == ["constant_as_parameter"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM ghosts JOIN phantoms USING (id)",
        "SELECT id FROM (ghosts JOIN phantoms USING (id)) JOIN orders o ON true",
    ],
)
def test_using_join_of_unknown_tables_reports_them_instead_of_crashing(sql):
    definition = ContextDefinition.model_validate(
        {"resources": [{"name": "orders", "fields": [{"name": "id", "type": "bigint"}]}]}
    )
    result = validate_sql_operation(sql, "postgresql", definition, {})
    assert "unknown_resource" in [e.code for e in result.errors]
