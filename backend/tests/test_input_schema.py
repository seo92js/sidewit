import copy

import pytest

from sidewit.validation.input_schema import (
    argument_issues,
    input_schema_for,
    input_schema_issues,
    parameter_types,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "region": {"type": "string", "description": "지역", "enum": ["서울", "부산"]},
        "start_date": {"type": "string", "format": "date"},
        "since": {"type": "string", "format": "date-time"},
        "days": {"type": "integer"},
        "min_amount": {"type": "number"},
        "completed_only": {"type": "boolean"},
    },
    "required": ["region", "start_date", "since", "days", "min_amount", "completed_only"],
    "additionalProperties": False,
}


def mutate(fn):
    schema = copy.deepcopy(SCHEMA)
    fn(schema)
    return schema


def refs(schema):
    issues = input_schema_issues(schema)
    assert all(i.code == "invalid_input_schema" for i in issues)
    return [i.ref for i in issues]


def test_valid_schema_has_no_issues():
    assert input_schema_issues(SCHEMA) == []


def test_empty_inputs_are_allowed():
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    assert input_schema_issues(schema) == []


@pytest.mark.parametrize(
    ("change", "expected_ref"),
    [
        (lambda s: s.update(type="array"), "type"),
        (lambda s: s.pop("additionalProperties"), "additionalProperties"),
        (lambda s: s.update(additionalProperties=True), "additionalProperties"),
        (lambda s: s.update(**{"$schema": "x"}), "$schema"),
        (lambda s: s.update(properties=[]), "properties"),
        (lambda s: s["required"].remove("days"), "required"),
        (lambda s: s["required"].append("ghost"), "required"),
        (lambda s: s["required"].append("days"), "required"),
        (lambda s: s.update(required="days"), "required"),
        (lambda s: s["properties"]["days"].update(default=30), "properties.days.default"),
        (lambda s: s["properties"]["days"].update(minimum=1), "properties.days.minimum"),
        (lambda s: s["properties"]["days"].update(type="array"), "properties.days.type"),
        (lambda s: s["properties"]["days"].update(type="null"), "properties.days.type"),
        (lambda s: s["properties"]["days"].update(format="date"), "properties.days.format"),
        (
            lambda s: s["properties"]["start_date"].update(format="email"),
            "properties.start_date.format",
        ),
        (lambda s: s["properties"]["days"].update(enum=[1, 2]), "properties.days.enum"),
        (lambda s: s["properties"]["region"].update(enum=[]), "properties.region.enum"),
        (lambda s: s["properties"]["region"].update(enum=["a", "a"]), "properties.region.enum"),
        (
            lambda s: s["properties"]["region"].update(description=1),
            "properties.region.description",
        ),
    ],
)
def test_invalid_input_schema(change, expected_ref):
    assert expected_ref in refs(mutate(change))


@pytest.mark.parametrize("name", ["Region", "start-date", "1days", "_x", "a" * 65])
def test_input_names_must_be_canonical_parameter_names(name):
    schema = {
        "type": "object",
        "properties": {name: {"type": "string"}},
        "required": [name],
        "additionalProperties": False,
    }
    assert f"properties.{name}" in refs(schema)


def test_array_inputs_are_not_supported():
    schema = {
        "type": "object",
        "properties": {"ids": {"type": "array", "items": {"type": "integer"}}},
        "required": ["ids"],
        "additionalProperties": False,
    }
    assert set(refs(schema)) == {"properties.ids.items", "properties.ids.type"}


def test_non_object_schema():
    assert input_schema_issues(["not", "a", "schema"])[0].code == "invalid_input_schema"


def test_parameter_types_and_round_trip():
    types = parameter_types(SCHEMA)
    assert types == {
        "region": "string",
        "start_date": "date",
        "since": "date-time",
        "days": "integer",
        "min_amount": "number",
        "completed_only": "boolean",
    }
    generated = input_schema_for(types)
    assert input_schema_issues(generated) == []
    assert parameter_types(generated) == types
    assert generated["required"] == list(types)


GOOD_ARGS = {
    "region": "서울",
    "start_date": "2026-09-01",
    "since": "2026-09-01T00:00:00+09:00",
    "days": 30,
    "min_amount": 9.5,
    "completed_only": True,
}


def codes(arguments):
    return [(i.code, i.ref) for i in argument_issues(SCHEMA, arguments)]


def test_valid_arguments():
    assert codes(GOOD_ARGS) == []


def test_missing_and_unexpected_arguments():
    arguments = {k: v for k, v in GOOD_ARGS.items() if k != "days"} | {"extra": 1}
    assert codes(arguments) == [("missing_argument", "days"), ("unexpected_argument", "extra")]


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("region", 1),
        ("start_date", "2026/09/01"),
        ("start_date", "2026-09-01T00:00:00+09:00"),
        ("since", "2026-09-01T00:00:00"),
        ("days", "30"),
        ("days", True),
        ("days", 1.5),
        ("min_amount", "9.5"),
        ("completed_only", "true"),
        ("region", None),
    ],
)
def test_argument_type_mismatch(name, bad):
    assert codes(GOOD_ARGS | {name: bad}) == [("argument_type_mismatch", name)]


def test_argument_not_in_enum():
    assert codes(GOOD_ARGS | {"region": "대구"}) == [("argument_not_in_enum", "region")]
