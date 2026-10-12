from datetime import UTC, datetime

from sidewit.domain.context import ContextDefinition
from sidewit.domain.issue import Issue
from sidewit.generation.prompt import render_repair_message, render_system_prompt

DEFINITION = ContextDefinition.model_validate(
    {
        "resources": [
            {
                "name": "orders",
                "description": "주문",
                "fields": [
                    {"name": "id", "type": "bigint", "primary_key": True, "nullable": False},
                    {
                        "name": "status",
                        "type": "text",
                        "nullable": False,
                        "allowed_values": ["COMPLETED", "CANCELLED"],
                    },
                    {"name": "paid_at", "type": "timestamptz", "description": "결제 완료 시각"},
                ],
            },
            {"name": "customers", "fields": [{"name": "id", "type": "bigint"}]},
        ],
        "relations": [{"from": "orders.customer_id", "to": "customers.id"}],
        "rules": [{"id": "rule_1", "text": "매출은 COMPLETED 주문만 포함한다."}],
    }
)


def render(reference_time=datetime(2026, 10, 2, 0, 30, tzinfo=UTC), timezone="Asia/Seoul"):
    return render_system_prompt(DEFINITION, "postgresql", timezone, reference_time)


def test_reference_time_is_shown_in_context_timezone():
    prompt = render()
    assert "SQL dialect: postgresql" in prompt
    assert "Reference time: 2026-10-02T09:30:00+09:00 (Friday), timezone Asia/Seoul" in prompt


def test_reference_time_crossing_date_boundary():
    prompt = render(datetime(2026, 10, 1, 16, 0, tzinfo=UTC))
    assert "2026-10-02T01:00:00+09:00 (Friday)" in prompt


def test_context_is_rendered_with_descriptions_samples_relations_and_rules():
    prompt = render()
    assert "- orders: 주문" in prompt
    assert "  - id (bigint, primary key)" in prompt
    assert "  - status (text, not null) Allowed values (exact): 'COMPLETED', 'CANCELLED'." in prompt
    assert "  - paid_at (timestamptz): 결제 완료 시각" in prompt
    assert "- customers\n" in prompt
    assert "- orders.customer_id → customers.id" in prompt
    assert "- [rule_1] 매출은 COMPLETED 주문만 포함한다." in prompt


def test_empty_sections_are_explicit():
    prompt = render_system_prompt(ContextDefinition(), "postgresql", "UTC", datetime.now(UTC))
    assert prompt.count("(none)") == 3


def test_multiple_undescribed_dates_are_highlighted_without_hiding_rules():
    definition = ContextDefinition.model_validate(
        {
            "resources": [
                {
                    "name": "visits",
                    "fields": [
                        {"name": "opened_at", "type": "TIMESTAMP(6)"},
                        {"name": "closed_at", "type": "timestamptz"},
                        {"name": "other", "type": " "},
                    ],
                }
            ],
            "rules": [{"id": "period", "text": "The period uses closed_at."}],
        }
    )
    prompt = render_system_prompt(definition, "postgresql", "UTC", datetime.now(UTC))
    assert "Only for requests needing date-dependent filtering or grouping." in prompt
    assert "Period choice is unresolved by field descriptions: opened_at, closed_at." in prompt
    assert "If no relevant rule or explicit user choice settles it" in prompt
    assert "[period] The period uses closed_at." in prompt


def test_described_dates_do_not_emit_an_unresolved_period_hint():
    assert "Period choice is unresolved" not in render()


def test_prompt_states_the_generation_policies():
    prompt = render()
    for required in (
        "`unanswerable`",
        "`ambiguous`",
        "`clarifications`",
        "Choose fields by their descriptions and the business rules",
        "The reference time never decides which field to use.",
        "Parameters represent values derived from the user's request only.",
        "Constants required by the context or the business rules are not parameters.",
        "named placeholders `:name`",
        "Never use `?`, `$1` or `%s`",
        "Never use `NOW()`, `CURRENT_DATE`, `CURRENT_TIME`, `CURRENT_TIMESTAMP`, `LOCALTIME`, "
        "`LOCALTIMESTAMP`",
        "`value` is always written as a string",
    ):
        assert required in prompt, required
    assert "$" not in prompt.replace("`$1`", ""), "no unsubstituted template variables"


def test_repair_message_lists_validation_errors():
    message = render_repair_message(
        [
            Issue(
                code="unknown_field",
                message="Field 'amt' does not exist on 'orders'.",
                ref="orders.amt",
            ),
            Issue(code="multiple_statements", message="Exactly one statement is allowed."),
        ]
    )
    assert "- [unknown_field] Field 'amt' does not exist on 'orders'. (ref: orders.amt)" in message
    assert "- [multiple_statements] Exactly one statement is allowed.\n" in message
    assert "`unanswerable`" in message


def test_prompt_renders_exact_sql_identifiers_and_escapes_quotes():
    definition = ContextDefinition.model_validate(
        {
            "resources": [
                {"name": "Store", "fields": [{"name": "regionSido", "type": "text"}]},
                {"name": "order", "fields": [{"name": 'a"b', "type": "text"}]},
            ]
        }
    )
    prompt = render_system_prompt(definition, "postgresql", "UTC", datetime.now(UTC))
    assert '- Resource SQL: "Store"' in prompt
    assert '- Field SQL: "Store"."regionSido"' in prompt
    assert '- Field SQL: "order"."a""b"' in prompt
    assert "PostgreSQL folds unquoted identifiers to lowercase" in prompt


def test_repair_identifier_hints_only_for_name_resolution_errors():
    definition = ContextDefinition.model_validate(
        {
            "resources": [
                {"name": "Store", "fields": [{"name": "regionSido", "type": "text"}]},
            ]
        }
    )
    for code in ("unknown_resource", "unknown_field"):
        message = render_repair_message([Issue(code=code, message="Unknown name")], definition)
        assert '"Store"."regionSido"' in message
        assert "Do not guess a replacement for a genuinely unknown name" in message
    message = render_repair_message(
        [Issue(code="multiple_statements", message="Only one statement")], definition
    )
    assert "Exact SQL identifiers" not in message
