"""Structural validation of SQL operations (SPEC §7).

Deterministic and static: no database, no LLM. It guarantees structure only — syntax,
statement policy, resource/field existence and the parameter contract. It does NOT check
whether Business Rules or metric definitions were applied. Invalid SQL is reported, never
rewritten.
"""

import re
from collections.abc import Collection, Iterator
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope

from sidewit.domain.context import ContextDefinition, registered_constants
from sidewit.domain.dialect import (
    Dialect,
    cast_result_type,
    cast_value,
    to_sqlglot,
    unbounded_text,
    user_type_identity,
)
from sidewit.domain.issue import Issue
from sidewit.domain.metric import field_type
from sidewit.domain.params import (
    PARAM_NAME_PATTERN,
    ParamType,
    exact_number,
    matches_allowed_value,
    valid_json_unicode,
    value_matches_type,
)
from sidewit.sql import COMPLEXITY_MESSAGE, SqlComplexityError, parse_sql
from sidewit.validation.functions import function_issues
from sidewit.validation.request_literals import request_literal_issues
from sidewit.validation.sql_analysis import (
    FieldRef,
    SqlAnalysis,
    SqlAnalysisError,
)
from sidewit.validation.sql_time import (
    TemporalValues,
    special_time_literal,
    temporal_inputs,
    temporal_type,
)
from sidewit.validation.sql_values import ValueAnalysis, ValueOrigin

# A `:name` inside a string literal, e.g. INTERVAL ':days days'. The lookbehind skips
# `::casts` and words such as 'env:prod'.
_LITERAL_PLACEHOLDER = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")
_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,
)
_MISSING = object()

# Expressions whose value depends on the database's current time (PostgreSQL).
_CURRENT_TIME_NODES = (
    exp.CurrentTimestamp,  # NOW(), CURRENT_TIMESTAMP
    exp.CurrentDate,
    exp.CurrentTime,
    exp.Localtime,
    exp.Localtimestamp,
)
_CURRENT_TIME_FUNCTIONS = {
    "now",
    "clock_timestamp",
    "statement_timestamp",
    "transaction_timestamp",
    "timeofday",
}

# Comparison node -> operator with the field on the left; flipped when the field is right.
_COMPARISONS: dict[type[exp.Expression], str] = {
    exp.EQ: "=",
    exp.NullSafeEQ: "=",
    exp.NEQ: "!=",
    exp.NullSafeNEQ: "!=",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.LT: "<",
    exp.LTE: "<=",
}
_FLIPPED = {"=": "=", "!=": "!=", ">": "<", ">=": "<=", "<": ">", "<=": ">="}
# A field compared with one of its values by equality; ranges say nothing about the set.
_EQUALITY = {"=", "!="}


@dataclass(frozen=True)
class ValidationPolicy:
    """Per-path rules on top of the structural checks (SPEC §7)."""

    # False for Generate: relative time must be resolved against reference_time into
    # parameter values, so results never depend on the database clock (SPEC §4).
    allow_relative_time: bool = True
    # Generate: the user's request. Literals it explicitly mentions must be parameters.
    request_text: str | None = None
    # False for Generate: a registered constant (Rule condition, metric fixed filter) must
    # stay a SQL literal; binding it to a parameter would make it caller-controlled.
    allow_constant_parameters: bool = True


DEFAULT_POLICY = ValidationPolicy()
# Model-written SQL without the request text (e.g. re-checking a stored operation).
GENERATE_POLICY = ValidationPolicy(allow_relative_time=False, allow_constant_parameters=False)
# SQL the server compiled from a metric contract. Its parameters are the contract's declared
# inputs, which may legitimately share a field with another metric's fixed filter.
CONTRACT_POLICY = ValidationPolicy(allow_relative_time=False)


def generate_policy(question: str) -> ValidationPolicy:
    return replace(GENERATE_POLICY, request_text=question)


class ValidationResult(BaseModel):
    valid: bool
    errors: list[Issue]
    warnings: list[Issue]
    checks: list[str]


@dataclass(frozen=True)
class OperationAnalysis:
    result: ValidationResult
    # Placeholder -> allowed values of the enum-like field(s) it is compared with (the
    # intersection when it is compared with several). Used to constrain Tool inputs.
    parameter_allowed: dict[str, list[Any]]


def validate_sql_operation(
    statement: str,
    dialect: Dialect,
    context: ContextDefinition,
    declared_params: dict[str, ParamType],
    values: dict[str, Any] | None = None,
    policy: ValidationPolicy = DEFAULT_POLICY,
) -> ValidationResult:
    return analyze_sql_operation(
        statement, dialect, context, declared_params, values, policy
    ).result


def analyze_sql_operation(
    statement: str,
    dialect: Dialect,
    context: ContextDefinition,
    declared_params: dict[str, ParamType],
    values: dict[str, Any] | None = None,
    policy: ValidationPolicy = DEFAULT_POLICY,
) -> OperationAnalysis:
    """validate_sql_operation plus what the same pass learned about the parameters."""
    if not valid_json_unicode(declared_params) or not valid_json_unicode(values):
        # Reject before diagnostics can copy an unencodable key into an Issue's message/ref.
        return OperationAnalysis(
            ValidationResult(
                valid=False,
                errors=[
                    Issue(
                        code="parameter_type_mismatch",
                        message="Parameter names and values must contain valid Unicode.",
                    )
                ],
                warnings=[],
                checks=["parameters"],
            ),
            {},
        )
    try:
        validator = _Validator(statement, dialect, context, declared_params, values, policy)
        result = validator.run()
        return OperationAnalysis(result, validator.parameter_allowed)
    except (RecursionError, SqlComplexityError):
        # Includes type parsing and lineage analysis, not just the initial SQL parse.
        return OperationAnalysis(
            ValidationResult(
                valid=False,
                errors=[Issue(code="sql_analysis_error", message=COMPLEXITY_MESSAGE)],
                warnings=[],
                checks=["syntax"],
            ),
            {},
        )


def allowed_values_by_parameter(
    statement: str, dialect: Dialect, context: ContextDefinition
) -> dict[str, list[Any]]:
    """Placeholders compared with an enum-like field, mapped to its allowed values (used to
    constrain Tool inputs). Empty when the statement does not parse."""
    return analyze_sql_operation(statement, dialect, context, {}).parameter_allowed


def parameter_value_issues(
    statement: str,
    dialect: Dialect,
    context: ContextDefinition,
    declared: dict[str, ParamType],
    values: dict[str, Any],
) -> list[Issue]:
    """Check actual inputs with the same rules as /validate.

    The caller has already validated the saved statement and its Context version, so only
    input-dependent failures from this pass apply to invocation.
    """
    result = validate_sql_operation(statement, dialect, context, declared, values)
    return [
        issue
        for issue in result.errors
        if issue.code
        in {
            "value_not_allowed",
            "value_unverifiable",
            "parameter_type_mismatch",
            "sql_analysis_error",
        }
    ]


class _Validator:
    def __init__(
        self,
        statement: str,
        dialect: Dialect,
        context: ContextDefinition,
        declared_params: dict[str, ParamType],
        values: dict[str, Any] | None,
        policy: ValidationPolicy,
    ) -> None:
        self.statement = statement
        self.policy = policy
        self.context = context
        self.sidewit_dialect = dialect
        self.dialect = to_sqlglot(dialect)
        self.declared = declared_params
        self.values = values
        # Stored names; the AST is normalized the same way, so names compare exactly.
        self.fields: dict[str, list[str]] = {
            r.name: [f.name for f in r.fields] for r in context.resources
        }
        self.string_fields: set[FieldRef] = {
            (r.name, f.name)
            for r in context.resources
            for f in r.fields
            if field_type(f) == "string"
        }
        self.boolean_fields: set[FieldRef] = {
            (r.name, f.name)
            for r in context.resources
            for f in r.fields
            if field_type(f) == "boolean"
        }
        # (resource, field) -> declared allowed values (enum-like fields only).
        # Fields of a user-defined (enum) type: a cast to that same type keeps the value.
        self.user_types: dict[FieldRef, tuple[str, str]] = {
            (r.name, f.name): identity
            for r in context.resources
            for f in r.fields
            if (identity := user_type_identity(f.type)) is not None
        }
        self.allowed: dict[FieldRef, list[Any]] = {
            (r.name, f.name): f.allowed_values
            for r in context.resources
            for f in r.fields
            if f.allowed_values
        }
        self.enum_labels: dict[tuple[str, str], set[str]] = {}
        for ref, identity in self.user_types.items():
            labels = self.allowed.get(ref, [])
            if labels and all(isinstance(v, str) for v in labels):
                self.enum_labels.setdefault(identity, set()).update(labels)
        self.constants = registered_constants(context)
        # Literal nodes equal to a registered constant on their field (not request values).
        self.constant_literals: set[int] = set()
        # placeholder -> allowed values of the enum-like field it is compared with.
        self.parameter_allowed: dict[str, list[Any]] = {}
        self.relations: set[tuple[str | None, str | None]] = set()
        for relation in context.relations:
            a, b = context.canonical_ref(relation.from_), context.canonical_ref(relation.to)
            self.relations |= {(a, b), (b, a)}
        self.errors: list[Issue] = []
        self.warnings: list[Issue] = []
        self.checks: list[str] = []

    def run(self) -> ValidationResult:
        ast = self._parse()
        if ast is not None and self._check_statement_policy(ast):
            self._check(ast)
        return ValidationResult(
            valid=not self.errors,
            errors=_dedupe(self.errors),
            warnings=_dedupe(self.warnings),
            checks=self.checks,
        )

    def _check(self, ast: exp.Expression) -> None:
        """Checks that need name resolution are skipped when the analysis fails; the others
        (parameters, placeholders, relative time) still run."""
        self.checks.append("functions")
        self.errors += function_issues(ast, self.context.sql_extensions)
        analyzed = True
        try:
            self.analysis = SqlAnalysis(ast, self.fields, self.string_fields, self.user_types)
            scopes = self.analysis.scopes
            self._check_resources(ast)
            self._check_fields(scopes)
            self._check_relations(scopes)
        except SqlAnalysisError as e:
            self._error("sql_analysis_error", str(e))
            analyzed = False
        self._check_parameters(ast)
        if analyzed:
            try:
                comparisons = list(self._field_comparisons(scopes))
            except SqlAnalysisError as e:
                self._error("sql_analysis_error", str(e))
                analyzed = False
            else:
                self._check_allowed_values(comparisons)
                self._check_constants(comparisons)
        if not analyzed and (
            self.allowed or (self.constants and not self.policy.allow_constant_parameters)
        ):
            self._error(
                "value_unverifiable", "Required Context value policies could not be analyzed."
            )
        self._check_placeholder_safety(ast)
        if not self.policy.allow_relative_time:
            self._check_relative_time(ast, analyzed)
        # Text matching is only meaningful on structurally valid SQL.
        if analyzed and self.policy.request_text is not None and not self.errors:
            self.checks.append("request_literals")
            self.errors += request_literal_issues(
                ast,
                self.sidewit_dialect,
                self.context,
                self.policy.request_text,
                self.constant_literals,
            )

    def _error(self, code: str, message: str, ref: str | None = None) -> None:
        self.errors.append(Issue(code=code, message=message, ref=ref))

    # syntax ---------------------------------------------------------------------------

    def _parse(self) -> exp.Expression | None:
        self.checks.append("syntax")
        if len(self.statement) > 64000:
            self._error("sql_parse_error", "SQL exceeds 64000 characters.")
            return None
        try:
            statements = [s for s in parse_sql(self.statement, self.dialect) if s]
        except SqlglotError as e:
            self._error("sql_parse_error", f"SQL could not be parsed: {e}")
            return None
        if not statements:
            self._error("sql_parse_error", "SQL statement is empty.")
            return None
        if len(statements) > 1:
            self._error(
                "multiple_statements",
                f"Exactly one statement is allowed; found {len(statements)}.",
            )
            return None
        # Dollar strings have literal contents. Escape/Unicode strings require a complete
        # PostgreSQL decoder; fail closed instead of letting them bypass value policies.
        for node in list(statements[0].walk()):
            if isinstance(node, exp.RawString):
                node.replace(exp.Literal.string(node.this))
            elif isinstance(node, (exp.ByteString, exp.UnicodeString)):
                self._error(
                    "unsupported_string_literal",
                    "Escape and Unicode SQL strings are unsupported; use a bound parameter "
                    "or an ordinary quoted string.",
                )
        # Fold unquoted identifiers as PostgreSQL does; every later check (and error refs)
        # sees the names the database would resolve.
        return normalize_identifiers(statements[0], dialect=self.dialect)

    # statement_policy -----------------------------------------------------------------

    def _check_statement_policy(self, ast: exp.Expression) -> bool:
        """Returns False when the top-level statement is not a query at all, in which case
        the remaining checks are meaningless."""
        self.checks.append("statement_policy")
        root = ast.unnest() if isinstance(ast, exp.Subquery) else ast
        if not isinstance(root, (exp.Select, exp.SetOperation)):
            self._error(
                "statement_not_allowed",
                f"Only SELECT queries are allowed; got {root.key.upper()}.",
            )
            return False

        for node in ast.find_all(*_FORBIDDEN_NODES):
            self._error(
                "statement_not_allowed",
                f"{node.key.upper()} is not allowed inside a query.",
            )
        for select in ast.find_all(exp.Select):
            if select.args.get("into"):
                self._error("statement_not_allowed", "SELECT INTO is not allowed.")
            if select.args.get("locks"):
                self._error(
                    "statement_not_allowed", "Locking clauses (FOR UPDATE/SHARE) are not allowed."
                )
        return True

    # resources ------------------------------------------------------------------------

    def _check_resources(self, ast: exp.Expression) -> None:
        self.checks.append("resources")
        self.errors += resource_issues(ast, self.fields, self.sidewit_dialect)

    # fields ---------------------------------------------------------------------------

    def _check_fields(self, scopes: list[Scope]) -> None:
        self.checks.append("fields")
        for scope in scopes:
            for problem in self.analysis.using_problems(scope):
                if problem.ambiguous:
                    code, issue = "ambiguous_field", "appears more than once"
                else:
                    code, issue = "unknown_field", "does not exist"
                self._error(
                    code,
                    f"USING column '{problem.name}' {issue} on the {problem.side} side of the "
                    "join.",
                    problem.name,
                )
            for column in _scope_columns(scope):
                if column.is_star:
                    if (
                        column.table
                        and self.analysis.find_source(scope, column.table, column) is None
                    ):
                        self._error(
                            "unknown_resource",
                            f"'{column.table}.*' names no table or alias available here.",
                            column.table,
                        )
                elif column.table:
                    self._check_qualified_column(scope, column)
                else:
                    self._check_unqualified_column(scope, column)

    def _check_qualified_column(self, scope: Scope, column: exp.Column) -> None:
        source = self.analysis.find_source(scope, column.table, column)
        name = column.name
        ref = f"{column.table}.{name}"
        if source is None:
            self._error(
                "unknown_resource",
                f"'{column.table}' is not a table or alias available at this point of the query.",
                column.table,
            )
            return
        names = self.analysis.source_names(source)
        if names is None:
            return  # opaque source: its columns cannot be checked
        if name not in names:
            if isinstance(source, exp.Table) and names == self.fields.get(source.name):
                # A Context table under its own column names: report the resource field.
                message, ref = (
                    f"Field '{name}' does not exist on '{source.name}'.",
                    (f"{source.name}.{name}"),
                )
            else:
                message = f"'{column.table}' has no column '{name}'."
            self._error("unknown_field", message, ref)
        elif names.count(name) > 1:
            self._error("ambiguous_field", f"'{column.table}' has more than one '{name}'.", ref)

    def _check_unqualified_column(self, scope: Scope, column: exp.Column) -> None:
        """Resolves a bare name as PostgreSQL does: against the FROM columns (JOIN USING
        columns count once) visible at its position, then of each enclosing query it may
        see."""
        name = column.name
        for current, env in self.analysis.reachable(scope, column):
            columns, opaque = self.analysis.candidates(current, env, name)
            matches = [c.aliases[0] for c in columns]
            if len(matches) > 1:
                where = (
                    f"'{matches[0]}' has more than one '{name}'."
                    if len(set(matches)) == 1
                    else f"Field '{name}' exists in {', '.join(matches)}; qualify it."
                )
                self._error("ambiguous_field", where, name)
                return
            if matches or opaque:
                return
            if current is scope and _is_alias_reference(scope, column):
                return
        self._error(
            "unknown_field",
            f"Field '{column.name}' does not exist on any resource in this query.",
            column.name,
        )

    # relations (warning) --------------------------------------------------------------

    def _check_relations(self, scopes: list[Scope]) -> None:
        self.checks.append("relations")
        for scope in scopes:
            if not isinstance(scope.expression, exp.Select):
                continue
            for join in scope.expression.args.get("joins") or []:
                condition = join.args.get("on")
                if condition is None:
                    continue
                for eq in condition.find_all(exp.EQ):
                    left = self.analysis.field(scope, eq.this)
                    right = self.analysis.field(scope, eq.expression)
                    if left is None or right is None or left[0] == right[0]:
                        continue
                    pair = (f"{left[0]}.{left[1]}", f"{right[0]}.{right[1]}")
                    if pair not in self.relations:
                        self.warnings.append(
                            Issue(
                                code="undeclared_relation",
                                message=f"Join '{pair[0]} = {pair[1]}' is not a declared "
                                "relation in the context.",
                                ref=f"{pair[0]} = {pair[1]}",
                            )
                        )

    # comparisons ----------------------------------------------------------------------

    def _field_comparisons(
        self, scopes: list[Scope]
    ) -> Iterator[tuple[FieldRef, str, ValueOrigin]]:
        """(field, operator, value side) for every comparison of a context field with a
        value, anywhere in a SELECT: WHERE, HAVING, JOIN ON, but also FILTER, CASE WHEN and
        projections. `field IN (...)`, `field = ANY(ARRAY[...])` yield each element. Each
        comparison is visited in the SELECT that contains it."""
        values_analysis = ValueAnalysis(self.analysis)
        for scope in scopes:
            select = scope.expression
            if not isinstance(select, exp.Select):
                continue
            for node in select.find_all(exp.In, *_COMPARISONS):
                if node.find_ancestor(exp.Select) is not select:
                    continue
                if isinstance(node, exp.In):
                    if node.args.get("unnest"):
                        continue
                    field = self.analysis.field(scope, node.this)
                    op = "!=" if isinstance(node.parent, exp.Not) else "="
                    query = node.args.get("query")
                    values = [query] if query is not None else node.expressions
                else:
                    op = _COMPARISONS[type(node)]
                    field = self.analysis.field(scope, node.this)
                    values = [node.expression]
                    if field is None:
                        field = self.analysis.field(scope, node.expression)
                        op, values = _FLIPPED[op], [node.this]
                if field is None:
                    continue
                for value in values:
                    for element in values_analysis.values(scope, value):
                        yield field, op, element

    # allowed values -------------------------------------------------------------------

    def _check_allowed_values(self, comparisons: list) -> None:
        """Literals, and parameter values when given, compared with an enum-like field must be
        one of its allowed values, spelled exactly."""
        if not self.allowed:
            return
        self.checks.append("allowed_values")
        for field, op, other in comparisons:
            if field in self.allowed and op in _EQUALITY:
                self._check_allowed_value(field, other)

    # registered constants --------------------------------------------------------------

    def _check_constants(self, comparisons: list) -> None:
        """A parameter bound to a registered constant's field and value makes a business
        definition caller-controlled. Checked by field and value, not by rule text."""
        if not self.constants:
            return
        self.checks.append("constants")
        for field, op, other in comparisons:
            ref = ".".join(field)
            matches = [c for c in self.constants if c.field == ref and c.op == op]
            if not matches:
                continue
            value, placeholder, _ = self._resolve(field, other)
            if value is _MISSING:
                continue
            if placeholder is None:
                if any(matches_allowed_value(value, c.value) for c in matches):
                    # request_literal_issues looks at Literal nodes, also inside -n or casts.
                    literal = (
                        other.expression.find(exp.Literal) if other.expression is not None else None
                    )
                    if literal is not None:
                        self.constant_literals.add(id(literal))
                continue
            if self.policy.allow_constant_parameters:
                continue
            constant = next((c for c in matches if matches_allowed_value(value, c.value)), None)
            if constant is not None:
                self._error(
                    "constant_as_parameter",
                    f"Parameter :{placeholder.name} binds {ref} {op} {constant.value!r}, a "
                    f"registered constant ({constant.source}). Write it as a SQL literal; it is "
                    "not a caller input.",
                    placeholder.name,
                )

    def _check_allowed_value(self, field: FieldRef, node: ValueOrigin) -> None:
        if node.kind == "database":
            return
        if node.kind == "unknown":
            self._unverifiable(field)
            return
        value, placeholder, preserving = self._resolve(field, node)
        if placeholder is not None and preserving:
            # The parameter's value reaches the field unchanged: it takes the field's values.
            allowed = self.allowed[field]
            previous = self.parameter_allowed.get(placeholder.name)
            self.parameter_allowed[placeholder.name] = (
                list(allowed)
                if previous is None
                else [v for v in previous if any(matches_allowed_value(v, a) for a in allowed)]
            )
        if value is _MISSING:
            if (
                placeholder is None
                or self.values is not None
                or not self._supported_casts(field, node)
            ):
                self._unverifiable(field)
            return
        allowed = self.allowed[field]
        if any(matches_allowed_value(value, a) for a in allowed):
            return
        ref = ".".join(field)
        listed = ", ".join(repr(a) for a in allowed[:30]) + (", ..." if len(allowed) > 30 else "")
        self._error(
            "value_not_allowed",
            f"{value!r} is not an allowed value of {ref}. Use one of these exactly: {listed}.",
            ref,
        )

    def _unverifiable(self, field: FieldRef) -> None:
        self._error(
            "value_unverifiable",
            f"Cannot determine the value compared with {'.'.join(field)}; "
            "use a direct parameter or a supported literal cast.",
            ".".join(field),
        )

    def _own_enum_cast(self, field: FieldRef, target: exp.DataType) -> bool:
        own = self.user_types.get(field)
        return own in self.enum_labels and user_type_identity(target) == own

    def _supported_casts(self, field: FieldRef, origin: ValueOrigin) -> bool:
        node = origin.expression
        if isinstance(node, exp.Placeholder):
            source = self.declared.get(node.name, "unknown")
        elif isinstance(node, exp.Boolean):
            source = "boolean"
        elif isinstance(node, exp.Literal) and node.is_string:
            source = "string"
        else:
            source = "number"
        for target in origin.casts:
            if self._own_enum_cast(field, target):
                source = "enum" if source in {"string", "enum"} else None
            elif source == "enum":
                # Enums convert to text/varchar, not directly to numeric/boolean.
                source = (
                    cast_result_type("string", target)
                    if target.is_type(exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR)
                    else None
                )
            else:
                source = cast_result_type(source, target)
            if source is None:
                return False
        return True

    def _resolve(
        self, field: FieldRef, origin: ValueOrigin
    ) -> tuple[Any, exp.Placeholder | None, bool]:
        if origin.kind != "expression" or origin.expression is None:
            return _MISSING, None, False
        node = origin.expression
        casts = origin.casts
        placeholder = node if isinstance(node, exp.Placeholder) else None
        string_input = placeholder is not None and (
            self.declared.get(placeholder.name) == "string"
            or (
                placeholder.name not in self.declared
                and any(self._own_enum_cast(field, to) for to in casts)
            )
        )
        preserving = all(
            (unbounded_text(to) or self._own_enum_cast(field, to)) and string_input for to in casts
        )
        if not self._supported_casts(field, origin):
            return _MISSING, placeholder, preserving
        if placeholder is not None:
            value = (self.values or {}).get(placeholder.name, _MISSING)
        elif casts:
            value = _literal_value(node)
            if isinstance(node, exp.Literal) and node.is_number or isinstance(node, exp.Neg):
                parsed = exact_number(value) if isinstance(value, str) else None
                value = _MISSING if parsed is None else parsed
        else:
            value = self._literal_for(field, node)
        for to in casts:
            if value is _MISSING:
                break
            if self._own_enum_cast(field, to):
                # Validate each intermediate enum label before any later truncation.
                labels = self.enum_labels[self.user_types[field]]
                cast = value if isinstance(value, str) and value in labels else None
            else:
                cast = cast_value(value, to)
            value = _MISSING if cast is None else cast
        return value, placeholder, preserving

    def _literal_for(self, field: FieldRef, node: exp.Expression) -> Any:
        """The value a SQL literal gives `field`. PostgreSQL reads a quoted literal compared
        with a boolean column as a boolean ('true', 't', 'yes', 'on', '1', ...)."""
        value = _literal_value(node)
        if field in self.boolean_fields and isinstance(value, str):
            return _postgres_bool(value)
        return value

    # parameters -----------------------------------------------------------------------

    def _check_parameters(self, ast: exp.Expression) -> None:
        self.checks.append("parameters")
        used: set[str] = set()
        for node in ast.find_all(exp.Placeholder, exp.Parameter):
            name = node.this if isinstance(node, exp.Placeholder) else None
            if not isinstance(name, str) or not name:
                self._error(
                    "unsupported_placeholder_style",
                    f"Placeholder '{node.sql(dialect=self.dialect)}' is not supported; "
                    "use named ':name' placeholders.",
                    node.sql(dialect=self.dialect),
                )
            elif not PARAM_NAME_PATTERN.match(name):
                self._error(
                    "unsupported_placeholder_style",
                    f"Placeholder ':{name}' must match {PARAM_NAME_PATTERN.pattern}.",
                    name,
                )
            else:
                used.add(name)

        for name in sorted(used - self.declared.keys()):
            self._error(
                "undeclared_parameter",
                f"Placeholder ':{name}' has no declared parameter.",
                name,
            )
        for name in sorted(self.declared.keys() - used):
            self._error(
                "unused_parameter",
                f"Parameter '{name}' is declared but not used in the statement.",
                name,
            )

        if self.values is None:
            return
        for name in sorted(self.values.keys() - self.declared.keys()):
            self._error("undeclared_parameter", f"Value given for undeclared '{name}'.", name)
        for name, param_type in sorted(self.declared.items()):
            value = self.values.get(name, _MISSING)
            if value is _MISSING:
                self._error("parameter_type_mismatch", f"No value for parameter '{name}'.", name)
            elif not value_matches_type(value, param_type):
                self._error(
                    "parameter_type_mismatch",
                    f"Value {value!r} for '{name}' is not a valid {param_type}.",
                    name,
                )

    # placeholder_safety ---------------------------------------------------------------

    def _check_placeholder_safety(self, ast: exp.Expression) -> None:
        self.checks.append("placeholder_safety")
        for literal in ast.find_all(exp.Literal):
            if not literal.is_string:
                continue
            for match in _LITERAL_PLACEHOLDER.finditer(literal.this):
                self._error(
                    "placeholder_in_literal",
                    f"Placeholder ':{match.group(1)}' inside string literal '{literal.this}' "
                    "will not be bound.",
                    match.group(1),
                )

    # relative_time (policy) -----------------------------------------------------------

    def _check_relative_time(self, ast: exp.Expression, analyzed: bool) -> None:
        self.checks.append("relative_time")
        clocks = [node for node in ast.walk() if is_current_time(node)]
        for node in clocks:
            expression = node.sql(dialect=self.dialect)
            self._error(
                "relative_time_function",
                f"'{expression}' depends on the database clock; resolve the period from "
                "reference_time into a parameter instead.",
                expression,
            )
        if clocks:
            return  # Already rejected; do not add unverifiable-source errors for known clocks.
        temporal_fields = {
            (r.name, f.name)
            for r in self.context.resources
            for f in r.fields
            if field_type(f) in {"date", "date-time"}
        }
        analysis = self.analysis if analyzed else None
        if analysis:
            values = TemporalValues(analysis, temporal_fields)
            for scope in analysis.scopes:
                for node in scope.expression.walk():
                    if node.find_ancestor(exp.Select) is not scope.expression:
                        continue
                    for source in temporal_inputs(node):
                        self._check_temporal_values(node, values.values(scope, source))
                    # Check the comparison operands, not descendant columns: a temporal
                    # column inside TO_CHAR or a text cast does not make its result temporal.
                    if isinstance(node, exp.Between):
                        pairs = [(node.this, node.args[key]) for key in ("low", "high")]
                    elif isinstance(node, exp.In):
                        query = node.args.get("query")
                        pairs = [
                            (node.this, value)
                            for value in ([query] if query is not None else node.expressions)
                        ]
                    elif type(node) in _COMPARISONS:
                        pairs = [(node.this, node.expression), (node.expression, node.this)]
                    else:
                        continue
                    for field_expr, value_expr in pairs:
                        field_expr = field_expr.unnest()
                        value_expr = value_expr.unnest()
                        temporal = analysis.field(scope, field_expr) in temporal_fields or any(
                            v.temporal for v in values.values(scope, field_expr)
                        )
                        if temporal:
                            self._check_temporal_values(node, values.values(scope, value_expr))

    def _check_temporal_values(self, node: exp.Expression, origins: list[ValueOrigin]) -> None:
        for origin in origins:
            if origin.kind == "database":
                continue
            value = _MISSING
            if isinstance(origin.expression, exp.Literal) and origin.expression.is_string:
                value = origin.expression.this
            elif isinstance(origin.expression, exp.Placeholder):
                if self.values is None:
                    continue  # Static checks cannot decide an input value not supplied yet.
                value = self.values.get(origin.expression.name, _MISSING)
            if origin.kind == "unknown":
                self._error(
                    "relative_time_unverifiable",
                    "Date input cannot be verified as independent of the database clock; "
                    "use an absolute date/date-time parameter instead.",
                    node.sql(dialect=self.dialect),
                )
                continue
            if (
                isinstance(node, exp.Cast)
                and node.to.is_type(exp.DataType.Type.ARRAY)
                and isinstance(value, str)
                and origin.expression.find_ancestor(exp.Array) is None
            ):
                self._error(
                    "relative_time_unverifiable",
                    "Date arrays must use an explicit ARRAY constructor, not encoded text.",
                    node.sql(dialect=self.dialect),
                )
                continue
            for target in origin.casts:
                if temporal_type(target):
                    break  # Check the value before PostgreSQL interprets it as a date.
                if isinstance(value, str):
                    value = cast_value(value, target)
                    if value is None:
                        self._error(
                            "relative_time_unverifiable",
                            "Date input contains an unsupported cast; use an absolute "
                            "date/date-time parameter instead.",
                            node.sql(dialect=self.dialect),
                        )
                        break
            if isinstance(value, str) and special_time_literal(value):
                expression = node.sql(dialect=self.dialect)
                self._error(
                    "relative_time_function",
                    f"'{expression}' depends on the database clock; resolve the period from "
                    "reference_time into a parameter instead.",
                    expression,
                )


def is_current_time(node: exp.Expression) -> bool:
    if isinstance(node, _CURRENT_TIME_NODES):
        return True
    if isinstance(node, exp.Anonymous):
        name = node.name.lower()
        # age(x) with a single argument is measured from current_date.
        return name in _CURRENT_TIME_FUNCTIONS or (name == "age" and len(node.expressions) == 1)
    if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal) and node.this.is_string:
        return node.to.is_type(*exp.DataType.TEMPORAL_TYPES) and (
            special_time_literal(node.this.this)
        )
    return False


def _literal_value(node: exp.Expression) -> Any:
    """The scalar a SQL literal writes (numbers as their text), or _MISSING."""
    if isinstance(node, exp.Literal | exp.Boolean):
        return node.this
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and node.this.is_number:
        return "-" + node.this.this
    return _MISSING


_BOOLEAN_WORDS = {"true": True, "yes": True, "false": False, "no": False}


def _postgres_bool(text: str) -> bool | str:
    """PostgreSQL's boolean input: a prefix of true/false/yes/no, on/off, 1/0 (trimmed,
    case-insensitive). Text it rejects is returned as is (and is not an allowed value)."""
    word = text.strip().lower()
    if word in {"1", "on"}:
        return True
    if word in {"0", "off", "of"}:
        return False
    matches = {v for full, v in _BOOLEAN_WORDS.items() if word and full.startswith(word)}
    return matches.pop() if len(matches) == 1 else text


def resource_issues(
    ast: exp.Expression, resources: Collection[str], dialect: Dialect
) -> list[Issue]:
    """Tables the statement reads that are not Context resources. This is Context policy,
    separate from whether PostgreSQL accepts the SQL: `pg_roles` or `pg_settings` resolve in
    any database but are not part of the Context. `ast` has normalized identifiers. Every
    table reference counts (not just scope sources, which miss e.g. the first table of an
    aliased parenthesized join); CTE names are not tables."""
    # Resolve each reference in its own lexical scope. A nested WITH must never exempt
    # an unrelated table of the same name elsewhere in the statement.
    cte_references = {
        id(table)
        for scope in traverse_scope(ast)
        for table in scope.tables
        if isinstance(scope.sources.get(table.alias_or_name), Scope)
    }
    issues = []
    for table in ast.find_all(exp.Table):
        if not _is_named_table(table):
            continue
        if table.args.get("db") is not None or table.args.get("catalog") is not None:
            name = table.sql(dialect=to_sqlglot(dialect))
            issues.append(
                Issue(
                    code="unknown_resource",
                    message=f"Schema-qualified table '{name}' is not supported; use the "
                    "resource name only.",
                    ref=name,
                )
            )
        elif table.name not in resources and id(table) not in cte_references:
            issues.append(
                Issue(
                    code="unknown_resource",
                    message=f"Resource '{table.name}' does not exist in the context.",
                    ref=table.name,
                )
            )
    return issues


def _is_named_table(table: exp.Table) -> bool:
    """False for table functions such as generate_series(...)."""
    return isinstance(table.this, exp.Identifier)


def _scope_columns(scope: Scope) -> list[exp.Column]:
    """Columns written directly in this scope's SELECT, each checked exactly once.

    `Scope.columns` is not used for SELECTs: it also lists unqualified columns of nested
    subqueries, and it drops names it assumes are output aliases (e.g. in HAVING/ORDER BY),
    which would hide unknown fields."""
    select = scope.expression
    if not isinstance(select, exp.Select):
        return list(scope.columns)
    return [c for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select]


def _is_alias_reference(scope: Scope, column: exp.Column) -> bool:
    """PostgreSQL resolves output-column aliases only in the GROUP BY and ORDER BY of the
    same SELECT, not in WHERE, HAVING or window clauses."""
    select = scope.expression
    if not isinstance(select, exp.Select):
        return False
    clause = column.find_ancestor(exp.Group, exp.Order, exp.Window, exp.Select)
    if not isinstance(clause, exp.Group | exp.Order) or clause.parent is not select:
        return False
    aliases = {e.alias for e in select.expressions if e.alias}
    return column.name in aliases


def _dedupe(issues: list[Issue]) -> list[Issue]:
    seen: set[tuple[str, str | None, str]] = set()
    unique: list[Issue] = []
    for issue in issues:
        key = (issue.code, issue.ref, issue.message)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique
