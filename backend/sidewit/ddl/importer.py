"""Parse CREATE TABLE DDL into a ContextDefinition preview (SPEC §5.1 import-ddl)."""

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from sidewit.domain.context import (
    ContextDefinition,
    ContextField,
    Relation,
    Resource,
    ScalarValue,
    allowed_value_issues,
)
from sidewit.domain.dialect import (
    Dialect,
    cast_value,
    normalized_name,
    to_sqlglot,
    unbounded_text,
    user_type_identity,
)
from sidewit.domain.issue import Issue
from sidewit.domain.metric import metric_issues
from sidewit.domain.params import exact_number, matches_allowed_value
from sidewit.sql import COMPLEXITY_MESSAGE, parse_sql

ImportMode = Literal["merge", "replace"]


class DdlParseError(Exception):
    pass


class PendingForeignKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str = Field(min_length=1, max_length=256)
    column: str = Field(min_length=1, max_length=256)
    ref_table: str = Field(min_length=1, max_length=256)
    ref_column: str | None = Field(default=None, min_length=1, max_length=256)


@dataclass
class _Parsed:
    dialect: Dialect
    qualified_tables: dict[str, str] = field(default_factory=dict)
    # Enum type name (as stored) -> its labels, from CREATE TYPE ... AS ENUM.
    enums: dict[tuple[str, ...], list[str]] = field(default_factory=dict)
    resources: list[Resource] = field(default_factory=list)
    foreign_keys: list[PendingForeignKey] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)


@dataclass
class ImportResult:
    definition: ContextDefinition
    warning_details: list[Issue]
    pending_foreign_keys: list[PendingForeignKey] = field(default_factory=list)


def import_ddl(
    existing: ContextDefinition,
    ddl: str,
    dialect: Dialect,
    mode: ImportMode,
    pending_foreign_keys: list[PendingForeignKey] | None = None,
) -> ImportResult:
    try:
        return _import_ddl(existing, ddl, dialect, mode, pending_foreign_keys)
    except RecursionError:
        raise DdlParseError(COMPLEXITY_MESSAGE) from None


def _import_ddl(
    existing: ContextDefinition,
    ddl: str,
    dialect: Dialect,
    mode: ImportMode,
    pending_foreign_keys: list[PendingForeignKey] | None = None,
) -> ImportResult:
    try:
        parsed = _parse(ddl, dialect)
    except ValidationError as e:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in e.errors()
        )
        raise DdlParseError(
            f"DDL contains a name or value unsupported by Context: {details}"
        ) from e
    warnings = parsed.warnings

    if mode == "replace":
        resources = parsed.resources
        relations: list[Relation] = []
    else:
        resources = _merge_resources(existing.resources, parsed.resources, warnings)
        relations = list(existing.relations)

    draft = ContextDefinition(
        resources=resources,
        relations=relations,
        rules=existing.rules,
        metrics=existing.metrics,
        sql_extensions=existing.sql_extensions,
    )
    pending: list[PendingForeignKey] = []
    for fk in [*(pending_foreign_keys or []), *parsed.foreign_keys]:
        relation = _resolve_foreign_key(draft, fk, warnings)
        if relation is not None and not _has_relation(draft.relations, relation):
            draft.relations.append(relation)
        if relation is None and fk not in pending:
            pending.append(fk)

    # The preview must be savable: a list the Context would reject is dropped, with a reason.
    for resource in draft.resources:
        for field_ in resource.fields:
            if field_.allowed_values and (issues := allowed_value_issues(resource, field_)):
                warnings.append(
                    Issue(
                        code="allowed_values_rejected",
                        message=f"allowed_values of '{resource.name}.{field_.name}' "
                        "were not imported: "
                        f"{issues[0].message}",
                    )
                )
                field_.allowed_values = []
    if draft.metrics:
        warnings.extend(
            Issue(code="metric_invalid", message=f"Metric {issue.ref}: {issue.message}")
            for issue in metric_issues(draft)
        )
    return ImportResult(definition=draft, warning_details=warnings, pending_foreign_keys=pending)


_PSQL_META_COMMAND = re.compile(r"^\\[a-z].*$", re.MULTILINE)


def _parse(ddl: str, dialect: Dialect) -> _Parsed:
    sqlglot_dialect = to_sqlglot(dialect)
    # pg_dump output carries psql meta-commands (`\restrict <key>` since PostgreSQL
    # 16.10/17.6); they are not SQL.
    ddl = _PSQL_META_COMMAND.sub("", ddl)
    try:
        statements = parse_sql(ddl, sqlglot_dialect)
    except SqlglotError as e:
        raise DdlParseError(str(e)) from e

    parsed = _Parsed(dialect=dialect)
    statements = [s for s in statements if s is not None]
    # Enum types first, so columns can use types declared later in the script.
    for statement in statements:
        if _is_enum_type(statement):
            labels = [e.this for e in statement.expression.expressions]
            parsed.enums[_enum_key(statement.this, parsed)] = labels
    for statement in statements:
        if _is_enum_type(statement):
            continue
        if (
            isinstance(statement, exp.Create)
            and statement.kind == "TABLE"
            and isinstance(statement.this, exp.Schema)
        ):
            _parse_create_table(statement.this, parsed)
        elif isinstance(statement, exp.Alter) and statement.kind == "TABLE":
            _parse_alter_table(statement, parsed)
        else:
            parsed.warnings.append(
                Issue(
                    code="ignored_statement",
                    message=f"Ignored statement: {_snippet(statement, sqlglot_dialect)}",
                )
            )
    return parsed


def _parse_create_table(schema: exp.Schema, parsed: _Parsed) -> None:
    table = schema.this
    name = _table_name(table, parsed)
    if any(r.name.lower() == name.lower() for r in parsed.resources):
        parsed.warnings.append(
            Issue(
                code="duplicate_table",
                message=f"Table '{name}' is defined more than once; kept the first.",
            )
        )
        return

    fields: list[ContextField] = []
    primary_keys: set[str] = set()
    table_checks: list[_CheckList] = []

    for item in schema.expressions:
        if isinstance(item, exp.ColumnDef):
            column = _parse_column(item, name, fields, primary_keys, parsed)
            if column is not None:
                fields.append(column)
            continue
        for pk in item.find_all(exp.PrimaryKey):
            primary_keys.update(_column_name(e, parsed).lower() for e in pk.expressions)
        for fk in item.find_all(exp.ForeignKey):
            _collect_foreign_key(name, fk, parsed)
        for check in item.find_all(exp.CheckColumnConstraint):
            if (found := _check_values(check.this, parsed)) is not None:
                table_checks.append(found)

    for f in fields:
        if f.name.lower() in primary_keys:
            f.primary_key = True
            f.nullable = False
    for found in table_checks:
        target = next((f for f in fields if f.name.lower() == found.column.lower()), None)
        if target is None:
            continue
        if found.values is None:
            _unreadable_check(name, target.name, parsed)
        else:
            _constrain(target, found.values, name, parsed)

    parsed.resources.append(Resource(name=name, fields=fields))


def _parse_column(
    column: exp.ColumnDef,
    table: str,
    fields: list[ContextField],
    primary_keys: set[str],
    parsed: _Parsed,
) -> ContextField | None:
    name = normalized_name(column.this, parsed.dialect)
    if any(f.name.lower() == name.lower() for f in fields):
        parsed.warnings.append(
            Issue(
                code="duplicate_column",
                message=f"Column '{table}.{name}' is defined more than once; kept the first.",
            )
        )
        return None

    kind = column.args.get("kind")
    field_type = (
        kind.sql(dialect=to_sqlglot(parsed.dialect)).lower() if kind is not None else "unknown"
    )
    if isinstance(kind, exp.DataType) and kind.this == exp.DataType.Type.USERDEFINED:
        # Preserve quoted identifier case; builtin type spelling remains canonical lowercase.
        field_type = normalize_identifiers(kind.copy(), dialect=to_sqlglot(parsed.dialect)).sql(
            dialect=to_sqlglot(parsed.dialect)
        )
    labels = _enum_labels(kind, parsed)
    lists: list[list[ScalarValue]] = [list(labels)] if labels else []
    nullable = True
    for constraint in column.args.get("constraints") or []:
        ckind = constraint.args.get("kind")
        if isinstance(ckind, exp.PrimaryKeyColumnConstraint):
            primary_keys.add(name.lower())
        elif isinstance(ckind, exp.NotNullColumnConstraint) and not ckind.args.get("allow_null"):
            nullable = False
        elif isinstance(ckind, exp.Reference):
            ref_table, ref_columns = _reference_target(ckind, parsed)
            parsed.foreign_keys.append(
                PendingForeignKey(
                    table=table,
                    column=name,
                    ref_table=ref_table,
                    ref_column=ref_columns[0] if ref_columns else None,
                )
            )
        elif isinstance(ckind, exp.CheckColumnConstraint):
            found = _check_values(ckind.this, parsed)
            if found is not None and found.column.lower() == name.lower():
                if found.values is None:
                    _unreadable_check(table, name, parsed)
                else:
                    lists.append(found.values)

    field = ContextField(name=name, type=field_type, nullable=nullable)
    for values in lists:
        _constrain(field, values, table, parsed)
    return field


def _parse_alter_table(statement: exp.Alter, parsed: _Parsed) -> None:
    table = _table_name(statement.this, parsed)
    found = False
    for action in statement.args.get("actions") or []:
        for fk in action.find_all(exp.ForeignKey):
            found = True
            _collect_foreign_key(table, fk, parsed)
    if not found:
        parsed.warnings.append(
            Issue(
                code="unsupported_alter",
                message=f"Ignored ALTER TABLE on '{table}': only FOREIGN KEY is imported.",
            )
        )


def _collect_foreign_key(table: str, fk: exp.ForeignKey, parsed: _Parsed) -> None:
    columns = [_column_name(e, parsed) for e in fk.expressions]
    reference = fk.args.get("reference")
    if reference is None:
        return
    ref_table, ref_columns = _reference_target(reference, parsed)
    if len(columns) != 1 or len(ref_columns) > 1:
        parsed.warnings.append(
            Issue(
                code="unsupported_composite_foreign_key",
                message=f"Skipped composite foreign key on '{table}' ({', '.join(columns)}); "
                "v0.1 relations are single-field.",
            )
        )
        return
    parsed.foreign_keys.append(
        PendingForeignKey(
            table=table,
            column=columns[0],
            ref_table=ref_table,
            ref_column=ref_columns[0] if ref_columns else None,
        )
    )


def _reference_target(reference: exp.Reference, parsed: _Parsed) -> tuple[str, list[str]]:
    target = reference.this
    if isinstance(target, exp.Schema):
        columns = [_column_name(e, parsed) for e in target.expressions]
        return _table_name(target.this, parsed), columns
    return _table_name(target, parsed), []


def _resolve_foreign_key(
    definition: ContextDefinition, fk: PendingForeignKey, warnings: list[Issue]
) -> Relation | None:
    source = f"{fk.table}.{fk.column}"
    ref_column = fk.ref_column
    if ref_column is None:
        target = definition.find_resource(fk.ref_table)
        pks = [f.name for f in target.fields if f.primary_key] if target else []
        if len(pks) != 1:
            warnings.append(
                Issue(
                    code="pending_foreign_key",
                    message=f"Skipped foreign key '{source}' → '{fk.ref_table}': "
                    "cannot determine a single referenced primary key.",
                )
            )
            return None
        ref_column = pks[0]

    target_ref = f"{fk.ref_table}.{ref_column}"
    if not definition.has_field(source) or not definition.has_field(target_ref):
        warnings.append(
            Issue(
                code="pending_foreign_key",
                message=f"Skipped foreign key '{source}' → '{target_ref}': "
                "endpoint is not in the context.",
            )
        )
        return None
    return Relation(from_=source, to=target_ref)


def _merge_resources(
    existing: list[Resource], parsed: list[Resource], warnings: list[Issue]
) -> list[Resource]:
    """Same-name resources/fields (case-insensitive) keep their description; the DDL wins for
    the stored name, type, primary_key and nullable, and for allowed_values when it declares
    them (ENUM/CHECK). Existing items absent from the DDL are kept with a warning."""
    parsed_by_name = {r.name.lower(): r for r in parsed}
    merged: list[Resource] = []

    for old in existing:
        new = parsed_by_name.pop(old.name.lower(), None)
        if new is None:
            warnings.append(
                Issue(
                    code="resource_preserved",
                    message=f"Resource '{old.name}' is not in the DDL; kept as is.",
                )
            )
            merged.append(old)
            continue

        new_fields = {f.name.lower(): f for f in new.fields}
        fields: list[ContextField] = []
        for old_field in old.fields:
            new_field = new_fields.pop(old_field.name.lower(), None)
            if new_field is None:
                warnings.append(
                    Issue(
                        code="field_preserved",
                        message=f"Field '{old.name}.{old_field.name}' is not in the DDL; kept.",
                    )
                )
                fields.append(old_field)
            else:
                fields.append(
                    old_field.model_copy(
                        update={
                            "name": new_field.name,
                            "type": new_field.type,
                            "primary_key": new_field.primary_key,
                            "nullable": new_field.nullable,
                            "allowed_values": new_field.allowed_values or old_field.allowed_values,
                        }
                    )
                )
        fields.extend(new_fields.values())
        merged.append(old.model_copy(update={"name": new.name, "fields": fields}))

    merged.extend(parsed_by_name.values())
    return merged


def _has_relation(relations: list[Relation], relation: Relation) -> bool:
    key = (relation.from_.lower(), relation.to.lower())
    return any((r.from_.lower(), r.to.lower()) == key for r in relations)


def _table_name(table: exp.Expression, parsed: _Parsed) -> str:
    name = _name(table.this, parsed) if isinstance(table, exp.Table) else table.name
    if isinstance(table, exp.Table) and table.args.get("db") is not None:
        qualified = f"{table.db}.{name}"
        key = name.lower()
        previous = parsed.qualified_tables.get(key)
        if previous is not None and previous != qualified:
            raise DdlParseError(
                f"Tables '{previous}' and '{qualified}' collapse to the same Context name. "
                "Import them into separate Contexts."
            )
        parsed.qualified_tables[key] = qualified
        message = f"Schema qualifier removed: '{table.db}.{name}' → '{name}'."
        if not any(w.message == message for w in parsed.warnings):
            parsed.warnings.append(Issue(code="schema_qualifier_removed", message=message))
    return name


def _column_name(node: exp.Expression, parsed: _Parsed) -> str:
    if isinstance(node, exp.Ordered):
        node = node.this
    if isinstance(node, exp.Column):
        node = node.this
    return _name(node, parsed)


def _name(node: exp.Expression, parsed: _Parsed) -> str:
    """Names as the database stores them (unquoted identifiers are case-folded), so SQL built
    from the Context resolves to the same objects."""
    return normalized_name(node, parsed.dialect) if isinstance(node, exp.Identifier) else node.name


def _snippet(statement: exp.Expression, sqlglot_dialect: str) -> str:
    sql = " ".join(statement.sql(dialect=sqlglot_dialect).split())
    return sql if len(sql) <= 80 else sql[:77] + "..."


def _is_enum_type(statement: exp.Expression) -> bool:
    return (
        isinstance(statement, exp.Create)
        and statement.kind == "TYPE"
        and isinstance(statement.expression, exp.DataType)
        and statement.expression.this == exp.DataType.Type.ENUM
    )


def _enum_key(node: exp.Expression | None, parsed: _Parsed) -> tuple[str, ...]:
    """Keep schema and quoted names distinct; an unqualified DDL type defaults to public.

    Arbitrary SET search_path evaluation is outside the DDL import contract.
    """
    if node is None:
        return ()
    return user_type_identity(node.sql(dialect=to_sqlglot(parsed.dialect))) or ()


def _enum_labels(kind: exp.Expression | None, parsed: _Parsed) -> list[str]:
    """Labels of a column whose type is an enum declared in the same DDL."""
    if not isinstance(kind, exp.DataType) or kind.this != exp.DataType.Type.USERDEFINED:
        return []
    identity = user_type_identity(kind)
    return parsed.enums.get(identity, []) if identity is not None else []


@dataclass(frozen=True)
class _CheckList:
    column: str
    values: list[ScalarValue] | None  # None: a value list with an element we cannot read


def _check_values(condition: exp.Expression, parsed: _Parsed) -> _CheckList | None:
    """The value list of `col IN (...)`, `col = ANY (ARRAY[...])` or `col = a OR col = b
    ...`; None for any other CHECK (ranges, functions: not a list). pg_dump writes a
    varchar column's list as `(col)::text = ANY ((ARRAY['A'::varchar, ...])::text[])`."""
    while isinstance(condition, exp.Paren):
        condition = condition.this
    column: exp.Column | None = None
    elements: list[exp.Expression] = []
    if isinstance(condition, exp.In) and condition.expressions:
        column, elements = _list_column(condition.this), list(condition.expressions)
    elif isinstance(condition, exp.EQ) and isinstance(condition.expression, exp.Any):
        array = condition.expression.this
        while isinstance(array, exp.Paren | exp.Cast):
            array = array.this
        if isinstance(array, exp.Array):
            column, elements = _list_column(condition.this), list(array.expressions)
    else:
        terms = list(_or_terms(condition))
        columns = [_list_column(t.this) if isinstance(t, exp.EQ) else None for t in terms]
        if terms and all(columns):
            names = {_column_name(c, parsed) for c in columns if c is not None}
            if len(names) == 1:
                column, elements = columns[0], [t.expression for t in terms]
    if column is None or any(e.find(exp.Column) for e in elements):
        return None  # not a list on one column, or compares columns with each other
    name = _column_name(column, parsed)
    values = [_check_literal(e, parsed) for e in elements]
    if any(v is None for v in values):
        return _CheckList(name, None)
    return _CheckList(name, [v for v in values if v is not None])


def _list_column(node: exp.Expression) -> exp.Column | None:
    """The column a value list constrains: `col`, or `(col)::text` as pg_dump writes a
    varchar column. Only an unbounded text cast keeps the column's value; a list on
    `col::varchar(1)` constrains a truncation, not the column itself."""
    while isinstance(node, exp.Paren) or (isinstance(node, exp.Cast) and unbounded_text(node.to)):
        node = node.this
    return node if isinstance(node, exp.Column) else None


def _check_literal(node: exp.Expression, parsed: _Parsed) -> ScalarValue | None:
    """A list element's value, with casts applied as PostgreSQL would: 'A', 'A'::varchar,
    -1, '1'::integer (the number 1), 'true'::boolean. None when it is not a literal, or a
    cast whose result cannot be stated exactly ('1.5'::integer rounds; dates and the like
    are not imported)."""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Cast):
        inner = _check_literal(node.this, parsed)
        identity = user_type_identity(node.to)
        if identity is not None:
            # Only an enum declared by this DDL can prove a label-preserving cast.
            labels = parsed.enums.get(identity, [])
            return inner if isinstance(inner, str) and inner in labels else None
        return None if inner is None else cast_value(inner, node.to)
    if isinstance(node, exp.Literal):
        return _value(node)
    if isinstance(node, exp.Neg):
        if isinstance(node.this, exp.Literal) and node.this.is_number:
            return _number("-" + node.this.this)
        inner = _check_literal(node.this, parsed)  # `::` binds tighter than minus
        return -inner if isinstance(inner, int | float) and not isinstance(inner, bool) else None
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    return None


def _constrain(field: ContextField, values: list[ScalarValue], table: str, parsed: _Parsed) -> None:
    """Every CHECK (and the enum type) holds at once: the allowed values are the
    intersection, in the first list's order."""
    if not field.allowed_values:
        field.allowed_values = list(values)
        return
    common = [v for v in field.allowed_values if any(matches_allowed_value(v, w) for w in values)]
    if not common:
        parsed.warnings.append(
            Issue(
                code="conflicting_check",
                message=f"CHECK constraints on '{table}.{field.name}' allow no common value; "
                "allowed_values were not imported.",
            )
        )
    field.allowed_values = common


def _unreadable_check(table: str, column: str, parsed: _Parsed) -> None:
    parsed.warnings.append(
        Issue(
            code="unsupported_check",
            message=f"CHECK on '{table}.{column}' lists values that are not plain literals; its "
            "allowed_values were not imported. Add them to the Context if they matter.",
        )
    )


def _or_terms(condition: exp.Expression):
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, exp.Or):
        yield from _or_terms(condition.this)
        yield from _or_terms(condition.expression)
    else:
        yield condition


def _value(literal: exp.Literal) -> ScalarValue:
    return literal.this if literal.is_string else _number(literal.this)


def _number(text: str) -> int | float:
    number = exact_number(text)
    if number is None:
        raise DdlParseError(f"Numeric CHECK value {text} must be finite and representable exactly.")
    return number
