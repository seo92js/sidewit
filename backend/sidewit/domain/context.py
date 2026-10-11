from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sidewit.domain.issue import Issue
from sidewit.domain.metric import (
    Comparison,
    FixedCondition,
    MetricDefinition,
    field_type,
    fixed_value_problem,
    metric_issues,
)
from sidewit.domain.params import value_matches_type

NAME_PATTERN = r"^[^.\s]+$"
REF_PATTERN = r"^[^.\s]+\.[^.\s]+$"

ScalarValue = str | int | float | bool


class ContextField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    type: str = Field(min_length=1)
    description: str | None = None
    primary_key: bool = False
    nullable: bool = True
    # The complete set of values this field holds (an enum, a CHECK list, or a code column
    # declared by the developer). Empty: unrestricted. Values compared with the field must be
    # one of these, spelled exactly.
    allowed_values: list[ScalarValue] = []


class Resource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    description: str | None = None
    fields: list[ContextField] = []


class Relation(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from", pattern=REF_PATTERN)
    to: str = Field(pattern=REF_PATTERN)


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    # Prose for the model.
    text: str = Field(min_length=1)
    # Optional machine-readable form of the rule's constant, enforced by the server
    # (constant_as_parameter). Prose-only rules guide generation but are not enforced.
    condition: FixedCondition | None = None


class ContextDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resources: list[Resource] = []
    relations: list[Relation] = []
    rules: list[Rule] = []
    metrics: list[MetricDefinition] = []

    def find_resource(self, name: str) -> Resource | None:
        key = name.lower()
        return next((r for r in self.resources if r.name.lower() == key), None)

    def has_field(self, ref: str) -> bool:
        resource_name, _, field_name = ref.partition(".")
        resource = self.find_resource(resource_name)
        if resource is None:
            return False
        key = field_name.lower()
        return any(f.name.lower() == key for f in resource.fields)

    def canonical_ref(self, ref: str) -> str | None:
        """`resource.field` spelled as stored, or None when it does not exist. Context
        references compare case-insensitively (definition_issues forbids names that differ
        only by case); SQL identifiers are matched against the stored spelling."""
        resource_name, _, field_name = ref.partition(".")
        resource = self.find_resource(resource_name)
        if resource is None:
            return None
        key = field_name.lower()
        field = next((f for f in resource.fields if f.name.lower() == key), None)
        return f"{resource.name}.{field.name}" if field else None

    def to_json(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)


def definition_issues(definition: ContextDefinition) -> list[Issue]:
    """Integrity checks that Pydantic field validation cannot express. Names compare
    case-insensitively."""
    issues: list[Issue] = []

    seen_resources: set[str] = set()
    for resource in definition.resources:
        key = resource.name.lower()
        if key in seen_resources:
            issues.append(
                Issue(
                    code="duplicate_resource",
                    message=f"Resource '{resource.name}' is defined more than once.",
                    ref=resource.name,
                )
            )
        seen_resources.add(key)

        seen_fields: set[str] = set()
        for field in resource.fields:
            field_key = field.name.lower()
            if field_key in seen_fields:
                ref = f"{resource.name}.{field.name}"
                issues.append(
                    Issue(
                        code="duplicate_field",
                        message=f"Field '{ref}' is defined more than once.",
                        ref=ref,
                    )
                )
            seen_fields.add(field_key)

    for relation in definition.relations:
        for ref in (relation.from_, relation.to):
            if not definition.has_field(ref):
                issues.append(
                    Issue(
                        code="unknown_relation_endpoint",
                        message=f"Relation endpoint '{ref}' does not exist in the context.",
                        ref=ref,
                    )
                )

    seen_rules: set[str] = set()
    for rule in definition.rules:
        issues += _rule_condition_issues(definition, rule)
        if rule.id in seen_rules:
            issues.append(
                Issue(
                    code="duplicate_rule_id",
                    message=f"Rule id '{rule.id}' is used more than once.",
                    ref=rule.id,
                )
            )
        seen_rules.add(rule.id)

    for resource in definition.resources:
        for field in resource.fields:
            issues += allowed_value_issues(resource, field)

    return issues + metric_issues(definition)


def allowed_value_issues(resource: Resource, field: ContextField) -> list[Issue]:
    ref = f"{resource.name}.{field.name}"
    values = field.allowed_values
    issues: list[Issue] = []
    if len({repr(v) for v in values}) != len(values):
        issues.append(
            Issue(code="invalid_allowed_values", message=f"'{ref}' repeats a value.", ref=ref)
        )
    kind = field_type(field)
    if kind is not None and any(not value_matches_type(v, kind) for v in values):
        issues.append(
            Issue(
                code="invalid_allowed_values",
                message=f"Allowed values of '{ref}' must be {kind} values.",
                ref=ref,
            )
        )
    return issues


def generation_issues(definition: ContextDefinition, allow_generated_sql: bool) -> list[Issue]:
    """A Context must have at least one way to answer: registered metrics, model-written SQL,
    or both (metrics first)."""
    if not allow_generated_sql and not definition.metrics:
        return [
            Issue(
                code="no_generation_path",
                message="allow_generated_sql=false requires at least one registered metric.",
                ref="allow_generated_sql",
            )
        ]
    return []


def _rule_condition_issues(definition: ContextDefinition, rule: Rule) -> list[Issue]:
    if rule.condition is None:
        return []
    ref = f"rules.{rule.id}.condition"
    if not definition.has_field(rule.condition.field):
        return [
            Issue(
                code="invalid_rule_condition",
                message=f"Field '{rule.condition.field}' does not exist in the context.",
                ref=ref,
            )
        ]
    if (problem := fixed_value_problem(definition, rule.condition)) is not None:
        return [Issue(code="invalid_rule_condition", message=problem, ref=ref)]
    return []


@dataclass(frozen=True)
class RegisteredConstant:
    field: str  # resource.field as stored
    op: Comparison
    value: Any
    source: str  # "rule <id>" or "metric <id>"


def registered_constants(definition: ContextDefinition) -> list[RegisteredConstant]:
    """Business constants the Context declares as data: Rule conditions and metric fixed
    filters. Generated SQL must keep them as literals, never as caller-supplied parameters."""
    conditions = [(r.condition, f"rule {r.id}") for r in definition.rules if r.condition]
    conditions += [(f, f"metric {m.id}") for m in definition.metrics for f in m.fixed_filters]
    return [
        RegisteredConstant(field, condition.op, condition.value, source)
        for condition, source in conditions
        if (field := definition.canonical_ref(condition.field)) is not None
    ]


def check_context_size(definition: ContextDefinition) -> None:
    """Bound write/generation inputs without making legacy definitions unreadable."""
    if len(definition.resources) > 100 or sum(len(r.fields) for r in definition.resources) > 1000:
        raise ValueError("Context supports at most 100 resources and 1000 fields.")
    if len(definition.model_dump_json().encode("utf-8")) > 128 * 1024:
        raise ValueError("Context definition exceeds 128 KiB of JSON.")
