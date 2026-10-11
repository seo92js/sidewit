"""Request-origin checks without semantic inference or SQL rewriting."""

import re

import pytest

from sidewit.domain.context import ContextDefinition
from sidewit.validation.sql_validator import generate_policy, validate_sql_operation

DEFINITION = ContextDefinition.model_validate(
    {
        "resources": [
            {
                "name": "members",
                "fields": [
                    {"name": "id", "type": "bigint"},
                    {"name": "zone", "type": "text"},
                    {"name": "state", "type": "text"},
                    {"name": "fee", "type": "numeric"},
                    {"name": "registered_at", "type": "timestamp"},
                ],
            }
        ],
        "rules": [{"id": "active", "text": "Active members have state = 'ACTIVE'."}],
    }
)


def request_issues(sql, question, definition=None):
    """Generate-policy errors; placeholders are declared so only request literals remain."""
    declared = {name: "string" for name in re.findall(r"(?<![:\w]):([a-z_][a-z0-9_]*)", sql)}
    result = validate_sql_operation(
        sql, "postgresql", definition or DEFINITION, declared, None, generate_policy(question)
    )
    assert "request_literals" in result.checks
    return result.errors


@pytest.mark.parametrize(
    ("sql", "question", "ref"),
    [
        ("SELECT id FROM members WHERE zone = 'Oslo'", "Oslo지역 회원", "zone"),
        ("SELECT id FROM members WHERE zone = '서울'", "서울지역 회원", "zone"),
        ("SELECT id FROM members WHERE zone = '北海道'", "北海道지역 회원", "zone"),
        ("SELECT id FROM members WHERE zone IN ('Oslo', 'Rome')", "Oslo와 Rome", "zone"),
        ("SELECT id FROM members WHERE fee >= 1250", "이용료 1250원 이상", "fee"),
        ("SELECT id FROM members WHERE fee BETWEEN 10 AND 20", "10~20 사이", "fee"),
        ("SELECT id FROM members WHERE fee < -10", "이용료 -10 미만", "fee"),
        ("SELECT id FROM members LIMIT 3", "상위 3개", "limit"),
        ("SELECT id FROM members OFFSET 5", "5개 건너뛰기", "offset"),
        (
            "SELECT m.id FROM members m WHERE m.zone = CAST('Oslo' AS TEXT)",
            "Oslo의 회원",
            "m.zone",
        ),
    ],
)
def test_explicit_request_literals_are_rejected(sql, question, ref):
    assert validate_sql_operation(sql, "postgresql", DEFINITION, {}).valid
    errors = request_issues(sql, question)
    assert len(errors) == 1
    assert errors[0].code == "request_value_literal" and errors[0].ref == ref


@pytest.mark.parametrize(
    ("sql", "question"),
    [
        ("SELECT id FROM members WHERE zone = :zone", "Oslo 지역 회원"),
        ("SELECT id FROM members WHERE state = 'ACTIVE'", "ACTIVE 회원"),
        ("SELECT 'Oslo' AS label, id FROM members", "Oslo라는 label을 붙여줘"),
        (
            "SELECT id FROM members WHERE TO_CHAR(registered_at, 'YYYY-MM-DD') = :date",
            "YYYY-MM-DD 형식으로 날짜 비교",
        ),
        ("SELECT id FROM members WHERE zone = 'US'", "USER 목록"),
        ("SELECT id FROM members WHERE zone = 'US'", "show us all members"),
        ("SELECT id FROM members WHERE fee > 30", "300원 조건"),
        ("SELECT id FROM members WHERE fee > 30", "30.5원 조건"),
        ("SELECT id FROM members WHERE zone = ''", "회원 목록"),
    ],
)
def test_constants_formats_and_nonmatches_are_not_rejected(sql, question):
    assert request_issues(sql, question) == []


def test_manual_tool_validation_still_accepts_fixed_filters():
    sql = "SELECT id FROM members WHERE zone = 'Oslo'"
    result = validate_sql_operation(sql, "postgresql", DEFINITION, {})
    assert result.valid and "request_literals" not in result.checks


def test_rule_mentions_conservatively_protect_constants_without_proving_semantics():
    definition = ContextDefinition.model_validate(
        DEFINITION.model_dump()
        | {"rules": [{"id": "north", "text": "This metric is defined for Oslo."}]}
    )
    assert (
        request_issues("SELECT id FROM members WHERE zone = 'Oslo'", "Oslo 회원", definition) == []
    )


def test_field_descriptions_can_define_fixed_constants_without_a_rule():
    data = DEFINITION.model_dump()
    data["rules"] = []
    data["resources"][0]["fields"][2]["description"] = "Active members use state ACTIVE."
    definition = ContextDefinition.model_validate(data)
    sql = "SELECT id FROM members WHERE state = 'ACTIVE'"
    assert request_issues(sql, "ACTIVE 회원", definition) == []


@pytest.mark.parametrize(
    ("sql", "question"),
    [
        # "1월" is a period, unrelated to "one customer".
        (
            "SELECT id FROM members WHERE registered_at >= :start_at ORDER BY fee DESC LIMIT 1",
            "1월에 가입한 회원 중 이용료가 가장 높은 회원",
        ),
        ("SELECT id FROM members WHERE fee > 30 LIMIT 30", "최근 30일 가입 회원"),
        ("SELECT id FROM members LIMIT 7", "last 7 days of members"),
        ("SELECT id FROM members LIMIT 2", "2 weeks of signups"),
    ],
)
def test_numbers_in_date_or_duration_expressions_are_not_request_values(sql, question):
    assert request_issues(sql, question) == []


def test_number_is_still_a_request_value_when_also_used_outside_a_period():
    errors = request_issues("SELECT id FROM members LIMIT 3", "3월 가입자 중 상위 3명")
    assert [(e.code, e.ref) for e in errors] == [("request_value_literal", "limit")]


def test_ref_names_the_compared_expression_without_a_column():
    sql = "SELECT zone FROM members GROUP BY zone HAVING COUNT(*) >= 3"
    errors = request_issues(sql, "회원이 3명 이상인 지역")
    assert [e.ref for e in errors] == ["COUNT(*)"]


def test_check_runs_only_on_structurally_valid_sql():
    result = validate_sql_operation(
        "SELECT nope FROM members WHERE zone = 'Oslo'",
        "postgresql",
        DEFINITION,
        {},
        None,
        generate_policy("Oslo 회원"),
    )
    assert [e.code for e in result.errors] == ["unknown_field"]
    assert "request_literals" not in result.checks


def test_allowed_values_are_not_context_constants():
    """A field's allowed values are what users filter by, so they stay request values."""
    data = DEFINITION.model_dump()
    data["resources"][0]["fields"][1]["allowed_values"] = ["Oslo", "서울"]
    definition = ContextDefinition.model_validate(data)
    errors = request_issues("SELECT id FROM members WHERE zone = 'Oslo'", "Oslo 회원", definition)
    assert [e.code for e in errors] == ["request_value_literal"]
