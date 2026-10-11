import pytest
from pydantic import ValidationError

from sidewit.domain.context import ContextDefinition, definition_issues


def make(**overrides) -> ContextDefinition:
    data = {
        "resources": [
            {"name": "customers", "fields": [{"name": "id", "type": "bigint"}]},
            {
                "name": "orders",
                "fields": [
                    {"name": "id", "type": "bigint"},
                    {"name": "customer_id", "type": "bigint"},
                ],
            },
        ],
        "relations": [{"from": "orders.customer_id", "to": "customers.id"}],
        "rules": [{"id": "rule_1", "text": "Cancelled orders are excluded."}],
    }
    data.update(overrides)
    return ContextDefinition.model_validate(data)


def codes(definition: ContextDefinition) -> list[str]:
    return [i.code for i in definition_issues(definition)]


def test_valid_definition_has_no_issues():
    assert codes(make()) == []


def test_relation_endpoints_are_case_insensitive():
    definition = make(relations=[{"from": "ORDERS.Customer_Id", "to": "Customers.ID"}])
    assert codes(definition) == []


def test_duplicate_resource_case_insensitive():
    definition = make(
        resources=[{"name": "orders", "fields": []}, {"name": "Orders", "fields": []}],
        relations=[],
    )
    assert codes(definition) == ["duplicate_resource"]


def test_duplicate_field():
    definition = make(
        resources=[
            {
                "name": "orders",
                "fields": [{"name": "id", "type": "int"}, {"name": "ID", "type": "int"}],
            }
        ],
        relations=[],
    )
    assert codes(definition) == ["duplicate_field"]


def test_unknown_relation_endpoints_reported_per_side():
    definition = make(relations=[{"from": "orders.missing", "to": "nowhere.id"}])
    issues = definition_issues(definition)
    assert [(i.code, i.ref) for i in issues] == [
        ("unknown_relation_endpoint", "orders.missing"),
        ("unknown_relation_endpoint", "nowhere.id"),
    ]


def test_duplicate_rule_id():
    definition = make(rules=[{"id": "r", "text": "a"}, {"id": "r", "text": "b"}])
    assert codes(definition) == ["duplicate_rule_id"]


@pytest.mark.parametrize(
    "bad",
    [
        {"resources": [{"name": "public.orders", "fields": []}]},
        {"relations": [{"from": "orders", "to": "customers.id"}]},
        {"resources": [{"name": "orders", "unexpected": 1}]},
        {"relations": [{"from": "orders.customer_id", "to": "customers.id", "kind": "x"}]},
    ],
)
def test_shape_errors_are_rejected_by_model(bad):
    with pytest.raises(ValidationError):
        make(**bad)


def test_relation_serializes_with_from_key():
    assert make().to_json()["relations"][0] == {
        "from": "orders.customer_id",
        "to": "customers.id",
    }
