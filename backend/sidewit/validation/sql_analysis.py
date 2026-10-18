"""PostgreSQL name resolution and value lineage for the structural validator.

The caller passes an AST whose identifiers are already normalized (unquoted names folded to
lower case), so names compare exactly, as PostgreSQL does. The module owns the three name
rules every check relies on:

- visibility: which FROM items a name at a given position may reference (reachable). A
  correlated subquery sees the enclosing query; a derived table or CTE does not see its
  siblings; a LATERAL item or table function sees only the items before it; a JOIN's ON
  sees only the items up to that join.
- joined columns: the columns a FROM clause exposes after JOIN USING / NATURAL JOIN merge
  common columns into one (joined_columns), which unqualified names and `*` resolve against.
- value lineage: the Context field whose stored value an expression carries unchanged
  (field). Output existence and lineage are separate: SUM(x) is an output column, but it
  does not carry x's allowed values.

Shapes whose columns cannot be known (table functions without a column list,
record-returning functions) are opaque: neither rejected nor traced.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope

from sidewit.domain.dialect import unbounded_text, user_type_identity
from sidewit.sql import AnalysisBudget

FieldRef = tuple[str, str]
Source = exp.Expression | Scope


class SqlAnalysisError(Exception):
    """The query is structurally inconsistent (e.g. duplicate aliases in one FROM)."""


@dataclass(frozen=True)
class OutputColumn:
    name: str
    scope: Scope
    expression: exp.Expression | None  # None: no traceable expression (VALUES, column list)
    # For an aliased join `(a JOIN b) AS j`: the field the column carries, already resolved.
    origin: FieldRef | None = None


@dataclass(frozen=True)
class JoinedColumn:
    """A column of a FROM clause. A JOIN USING / NATURAL column merges several origins."""

    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class UsingProblem:
    name: str
    side: str  # "left" or "right"
    ambiguous: bool  # True: the side has the column more than once; False: not at all


def unwrap_preserving(node: exp.Expression) -> tuple[exp.Expression, bool]:
    """Strips wrappers that keep the stored value: parentheses, aliases, COLLATE, and casts
    to unbounded text (`::text`, `::varchar`). Returns the inner node and whether a text
    cast was crossed: that keeps the value only for a string field (`flag::text` turns
    boolean true into 'true'), which `field` checks. A bounded cast (`::varchar(1)`) can
    truncate and any other cast can change the value, so those stop the unwrapping."""
    text_cast = False
    while True:
        if isinstance(node, exp.Alias | exp.Paren | exp.Collate):
            node = node.this
        elif isinstance(node, exp.Cast) and unbounded_text(node.to):
            node, text_cast = node.this, True
        else:
            return node, text_cast


def selected(scope: Scope) -> dict[str, Source]:
    """Sources in this scope's FROM/JOIN. `Scope.sources` also contains every CTE visible to
    the scope, which must not take part in column resolution."""
    try:
        return {name: source for name, (_, source) in scope.selected_sources.items()}
    except OptimizeError as e:
        raise SqlAnalysisError(str(e)) from e


@dataclass(frozen=True)
class Env:
    """What a name sees at one position of a FROM clause: the visible items (by alias),
    their columns as joined at that point, and whether one of them is opaque."""

    aliases: frozenset[str]
    columns: list[JoinedColumn]
    opaque: bool


@dataclass(frozen=True)
class _JoinInfo:
    group: int  # comma group of the join tree
    side: str
    left: Env  # the join's left side (what a LATERAL on its right may see)
    on: Env  # left and right joined (what its ON sees)


@dataclass
class _FromState:
    """A FROM clause evaluated as PostgreSQL binds it: comma-separated groups, each a join
    tree (parenthesized joins nest), JOIN binding tighter than commas."""

    groups: list[Env] = field(default_factory=list)
    joins: dict[int, _JoinInfo] = field(default_factory=dict)  # id(exp.Join) -> info
    comma_group: dict[int, int] = field(default_factory=dict)  # id(comma exp.Join) -> group
    problems: list[UsingProblem] = field(default_factory=list)

    def before(self, group: int) -> Env:
        """Every item of the comma groups before `group`."""
        return _concat(self.groups[:group])

    def all(self) -> Env:
        return _concat(self.groups)


def _concat(envs: list[Env]) -> Env:
    return Env(
        frozenset().union(*(e.aliases for e in envs)),
        [c for e in envs for c in e.columns],
        any(e.opaque for e in envs),
    )


def _join_tree(item: exp.Expression) -> tuple[exp.Expression, list[exp.Join]] | None:
    """`(a JOIN b ON ...)`: sqlglot nests the joins under the first table. With an alias,
    `(a JOIN b) AS j` is one item instead (its own scope; a and b are hidden outside)."""
    if isinstance(item, exp.Subquery) and not isinstance(item.this, exp.Query) and not item.alias:
        inner = item.this
        joins = inner.args.get("joins") or []
        if joins:
            return inner, joins
    return None


def _is_comma(join: exp.Join) -> bool:
    return not (join.args.get("on") or join.args.get("using") or join.kind or join.method)


class SqlAnalysis:
    def __init__(
        self,
        ast: exp.Expression,
        tables: dict[str, list[str]],
        string_fields: set[FieldRef],
        user_types: dict[FieldRef, tuple[str, str]] | None = None,
    ):
        """`tables`: Context resource name -> field names, as stored. `string_fields`:
        fields whose values are strings, so a text cast keeps them."""
        self.tables = tables
        self.string_fields = string_fields
        self.user_types = user_types or {}
        self.budget = AnalysisBudget()
        self._fields: dict[tuple[int, int, bool], FieldRef | None] = {}
        self._field_cycles = 0
        try:
            self.scopes = traverse_scope(ast)
            for scope in self.scopes:
                selected(scope)  # duplicate aliases fail before any policy checks
        except OptimizeError as e:
            raise SqlAnalysisError(str(e)) from e
        self._outputs: dict[int, list[OutputColumn] | None] = {}
        self._joined: dict[int, _FromState] = {}
        for values in ast.find_all(exp.Values):
            if len({len(row.expressions) for row in values.expressions}) > 1:
                raise SqlAnalysisError("VALUES lists must all be the same length.")
        for scope in self.scopes:
            select = scope.expression
            if (
                isinstance(select, exp.Select)
                and select.args.get("from_") is None
                and any(isinstance(p, exp.Star) for p in select.expressions)
            ):
                raise SqlAnalysisError("SELECT * with no tables specified is not valid.")
            # Every set operation, including the top-level one, must have arms of equal
            # width; computing its outputs checks that.
            if isinstance(select, exp.SetOperation):
                self.outputs(scope)

    # columns ----------------------------------------------------------------------------

    def source_names(self, source: Source) -> list[str] | None:
        """Column names a FROM source exposes, or None when they cannot be known."""
        if isinstance(source, Scope):
            outputs = self.outputs(source)
            return None if outputs is None else [c.name for c in outputs]
        if isinstance(source, exp.Table):
            alias = source.args.get("alias")
            renamed = (
                [c.name for c in alias.columns]
                if isinstance(alias, exp.TableAlias) and alias.columns
                else []
            )
            if isinstance(source.this, exp.Identifier):
                # Unknown Context tables are reported as unknown_resource by the caller.
                stored = self.tables.get(source.name)
                if stored is None:
                    return None
                if len(renamed) > len(stored):
                    raise SqlAnalysisError(
                        f"Table '{source.name}' has {len(stored)} columns but "
                        f"{len(renamed)} column aliases."
                    )
                # `orders AS o(x, s)` renames the first columns; the rest keep their names.
                return renamed + stored[len(renamed) :]
            if renamed:
                return renamed
            # PostgreSQL's scalar generate_series exposes its function/table alias as a column.
            if isinstance(source.this, exp.GenerateSeries | exp.ExplodingGenerateSeries):
                return [source.alias_or_name or "generate_series"]
        return None

    # names and positions -----------------------------------------------------------------

    def reachable(self, scope: Scope, node: exp.Expression) -> Iterator[tuple[Scope, Env | None]]:
        """The scopes whose FROM items `node` (an expression in `scope`) may reference,
        nearest first, each with what it sees from that position (None: everything). A
        subquery in WHERE/SELECT/... (correlated), a LATERAL item and a table function see
        the enclosing query; a derived table or CTE does not see the FROM items of the query
        it sits in, but still sees the queries around that one."""
        yield scope, self._env_at(scope, node)
        child = scope
        while child.parent is not None:
            parent = child.parent
            if child.scope_type not in (ScopeType.DERIVED_TABLE, ScopeType.CTE):
                yield parent, self._env_at(parent, child.expression)
            child = parent

    def find_source(self, scope: Scope, alias: str, node: exp.Expression) -> Source | None:
        """The source `alias` names where `node` (in `scope`) can see it."""
        for current, env in self.reachable(scope, node):
            source = selected(current).get(alias)
            if source is not None and (env is None or alias in env.aliases):
                return source
        return None

    def joined_columns(self, scope: Scope) -> tuple[list[JoinedColumn], bool]:
        """Columns of this scope's whole FROM clause in PostgreSQL order (JOIN USING /
        NATURAL columns merged into one), and whether an opaque item may add more."""
        env = self._from(scope).all()
        return env.columns, env.opaque

    def candidates(
        self, scope: Scope, env: Env | None, name: str
    ) -> tuple[list[JoinedColumn], bool]:
        """FROM columns named `name` seen at a position, and whether an opaque visible item
        might have the column too."""
        env = env or self._from(scope).all()
        return [c for c in env.columns if c.name == name], env.opaque

    def using_problems(self, scope: Scope) -> list[UsingProblem]:
        """JOIN USING columns missing from the left or the right side."""
        return self._from(scope).problems

    def _env_at(self, scope: Scope, node: exp.Expression) -> Env | None:
        """What `node` sees by where it is in `scope`'s FROM clause:
        - in a JOIN's ON: that join's two sides, joined (a USING merge further right does
          not exist yet);
        - inside a FROM item (LATERAL subquery, function arguments): the items before it,
          i.e. earlier comma groups and, unless it is the right side of a RIGHT/FULL JOIN,
          the left side of its join;
        - anywhere else (SELECT list, WHERE, ...): None, the whole FROM clause."""
        select = scope.expression
        if not isinstance(select, exp.Select) or select.args.get("from_") is None:
            return None
        state = self._from(scope)
        child, parent = node, node.parent
        while parent is not None and child is not select:
            if isinstance(parent, exp.Join):
                info = state.joins.get(id(parent))
                if child is parent.this:
                    if info is None:  # an item after a comma
                        return state.before(state.comma_group[id(parent)])
                    if info.side in ("RIGHT", "FULL"):
                        return state.before(info.group)
                    return _concat([state.before(info.group), info.left])
                if info is not None:
                    return info.on
            elif isinstance(parent, exp.From):
                return Env(frozenset(), [], False)  # the first FROM item sees nothing
            child, parent = parent, parent.parent
        return None

    def _from(self, scope: Scope) -> _FromState:
        key = id(scope)
        if key not in self._joined:
            self._joined[key] = self._evaluate_from(scope)
        return self._joined[key]

    def _evaluate_from(self, scope: Scope) -> _FromState:
        state = _FromState()
        select = scope.expression
        if not isinstance(select, exp.Select) or select.args.get("from_") is None:
            return state
        sources = selected(scope)
        first: exp.Expression = select.args["from_"].this
        group_joins: list[exp.Join] = []
        for join in [*(select.args.get("joins") or []), None]:
            if join is None or _is_comma(join):
                state.groups.append(
                    self._tree_env(first, group_joins, sources, len(state.groups), state)
                )
                if join is not None:
                    state.comma_group[id(join)] = len(state.groups)
                    first, group_joins = join.this, []
            else:
                group_joins.append(join)
        return state

    def _tree_env(
        self,
        first: exp.Expression,
        joins: list[exp.Join],
        sources: dict[str, Source],
        group: int,
        state: _FromState,
    ) -> Env:
        """A join tree's columns, recording what each JOIN's ON and right side see."""
        left = self._item_env(first, sources, group, state)
        for join in joins:
            right = self._item_env(join.this, sources, group, state)
            joined = self._join_columns(left, right, join, state.problems)
            on = Env(left.aliases | right.aliases, joined, left.opaque or right.opaque)
            state.joins[id(join)] = _JoinInfo(group, join.side, left, on)
            left = on
        return left

    def _item_env(
        self, expression: exp.Expression, sources: dict[str, Source], group: int, state: _FromState
    ) -> Env:
        # The first table of a parenthesized join carries its joins but is a plain item.
        if (tree := _join_tree(expression)) is not None:
            return self._tree_env(tree[0], tree[1], sources, group, state)
        alias = expression.alias_or_name
        source = sources.get(alias)
        names = self.source_names(source) if source is not None else None
        columns = [JoinedColumn(n, (alias,)) for n in names or []]
        return Env(frozenset({alias}), columns, names is None)

    def _aliased_join_outputs(self, scope: Scope, first: exp.Table) -> list[OutputColumn]:
        """`(a JOIN b ...) AS j`: the join's columns, each tied to the field it carries.
        sqlglot leaves the first table out of the scope's sources, so it is added here."""
        sources = {**selected(scope), first.alias_or_name: first}
        env = self._tree_env(first, first.args["joins"], sources, 0, _FromState())
        columns = []
        for column in env.columns:
            origins = {
                self._source_field(sources[alias], column.name, frozenset(), False)
                for alias in column.aliases
            }
            origin = origins.pop() if len(origins) == 1 else None
            columns.append(OutputColumn(column.name, scope, None, origin))
        return columns

    @staticmethod
    def _join_columns(
        left: Env, right: Env, join: exp.Join, problems: list[UsingProblem]
    ) -> list[JoinedColumn]:
        """`left JOIN right` columns: USING / NATURAL columns merged first, then the rest."""
        if join.method == "NATURAL":
            right_names = {c.name for c in right.columns}
            using = [c.name for c in left.columns if c.name in right_names]
        else:
            using = [i.name for i in join.args.get("using") or []]
        merged = []
        for name in using:
            left_matches = [c for c in left.columns if c.name == name]
            right_matches = [c for c in right.columns if c.name == name]
            # PostgreSQL reports the left side first, and only one problem per column.
            if not left.opaque and len(left_matches) != 1:
                problems.append(UsingProblem(name, "left", bool(left_matches)))
            elif not right.opaque and len(right_matches) != 1:
                problems.append(UsingProblem(name, "right", bool(right_matches)))
            origins = tuple(a for c in left_matches + right_matches for a in c.aliases)
            merged.append(JoinedColumn(name, origins))
        return (
            merged
            + [c for c in left.columns if c.name not in using]
            + [c for c in right.columns if c.name not in using]
        )

    def outputs(self, scope: Scope) -> list[OutputColumn] | None:
        """Output columns of a derived table, CTE or set operation; None when unknown."""
        key = id(scope)
        if key not in self._outputs:
            self._outputs[key] = self._compute_outputs(scope)
        return self._outputs[key]

    def _compute_outputs(self, scope: Scope) -> list[OutputColumn] | None:
        expression = scope.expression
        columns: list[OutputColumn] | None
        if isinstance(expression, exp.SetOperation):
            arms = [self.outputs(s) for s in scope.set_operation_scopes]
            columns = arms[0] if arms else None
            if columns is not None and any(
                arm is not None and len(arm) != len(columns) for arm in arms[1:]
            ):
                raise SqlAnalysisError("Set operation arms must have the same number of columns.")
        elif isinstance(expression, exp.Values):
            width = len(expression.expressions[0].expressions) if expression.expressions else 0
            columns = [OutputColumn(f"column{i + 1}", scope, None) for i in range(width)]
        elif isinstance(expression, exp.Select):
            columns = self._select_outputs(scope, expression)
        elif isinstance(expression, exp.Lateral) and scope.subquery_scopes:
            columns = self.outputs(scope.subquery_scopes[0])
        elif isinstance(expression, exp.Table) and expression.args.get("joins"):
            columns = self._aliased_join_outputs(scope, expression)
        else:
            columns = None  # table function (unnest, jsonb_each, ...): known only by name list
        aliases = _column_aliases(scope)
        if aliases:
            if columns is None:
                return [OutputColumn(name, scope, None) for name in aliases]
            if len(aliases) > len(columns):
                raise SqlAnalysisError("Output alias list has more names than the query columns.")
            names = aliases + [c.name for c in columns[len(aliases) :]]
            columns = [
                OutputColumn(n, c.scope, c.expression, c.origin)
                for n, c in zip(names, columns, strict=True)
            ]
        return columns

    def _select_outputs(self, scope: Scope, select: exp.Select) -> list[OutputColumn] | None:
        columns: list[OutputColumn] = []
        for projection in select.expressions:
            if not projection.is_star:
                columns.append(OutputColumn(projection.alias_or_name, scope, projection))
                continue
            if isinstance(projection, exp.Column) and projection.table:
                # alias.*: every column of that one item, merged USING columns included.
                source = selected(scope).get(projection.table)
                names = self.source_names(source) if source is not None else None
                if names is None:
                    return None
                columns.extend(
                    OutputColumn(n, scope, exp.column(n, table=projection.table, quoted=True))
                    for n in names
                )
                continue
            joined, opaque = self.joined_columns(scope)
            if opaque:
                return None  # SELECT * over an opaque source
            columns.extend(OutputColumn(c.name, scope, _column_ref(c)) for c in joined)
        return columns

    # lineage ----------------------------------------------------------------------------

    def field(
        self,
        scope: Scope,
        node: exp.Expression,
        seen: frozenset[tuple[int, str, str]] = frozenset(),
        text_cast: bool = False,
    ) -> FieldRef | None:
        """The Context field whose value `node` carries unchanged, or None (computed value,
        opaque or ambiguous source, recursive reference). Only value-preserving wrappers are
        followed (unwrap_preserving)."""
        self.budget.consume()
        node, crossed = unwrap_preserving(node)
        text_cast = text_cast or crossed
        if isinstance(node, exp.Cast) and (identity := user_type_identity(node.to)) is not None:
            origin = self.field(scope, node.this, seen, text_cast)
            return (
                origin if origin is not None and self.user_types.get(origin) == identity else None
            )
        if not isinstance(node, exp.Column) or node.is_star:
            return None
        marker = (id(scope), node.table, node.name)
        if marker in seen:
            self._field_cycles += 1
            return None  # recursive CTE: no single origin
        # Visibility can differ between ON/LATERAL/projection positions in one scope.
        key = (id(scope), id(node), text_cast)
        if key in self._fields:
            return self._fields[key]
        cycles = self._field_cycles
        result = self._resolve_field(scope, node, seen | {marker}, text_cast)
        # Cycle-cut results depend on the traversal path. Reuse only acyclic proofs.
        if cycles == self._field_cycles:
            self._fields[key] = result
        return result

    def _resolve_field(
        self,
        scope: Scope,
        node: exp.Column,
        seen: frozenset[tuple[int, str, str]],
        text_cast: bool,
    ) -> FieldRef | None:
        if node.table:
            source = self.find_source(scope, node.table, node)
            return (
                None if source is None else self._source_field(source, node.name, seen, text_cast)
            )
        for current, env in self.reachable(scope, node):
            matches, opaque = self.candidates(current, env, node.name)
            if len(matches) > 1 or (not matches and opaque):
                return None  # ambiguous (reported by the validator) or possibly opaque
            if matches:
                sources = selected(current)
                origins = {
                    self._source_field(sources[alias], node.name, seen, text_cast)
                    for alias in matches[0].aliases
                }
                # A merged USING column keeps a domain only when every side is that field.
                return origins.pop() if len(origins) == 1 else None
        return None

    def _source_field(
        self,
        source: Source,
        name: str,
        seen: frozenset[tuple[int, str, str]],
        text_cast: bool,
    ) -> FieldRef | None:
        if isinstance(source, exp.Table):
            names = self.source_names(source) if isinstance(source.this, exp.Identifier) else None
            if names is None or names.count(name) != 1:
                return None
            # The stored field behind a possibly renamed column (`orders AS o(x)`).
            ref = (source.name, self.tables[source.name][names.index(name)])
            return None if text_cast and ref not in self.string_fields else ref
        if not isinstance(source, Scope):
            return None
        outputs = self.outputs(source)
        matches = [i for i, c in enumerate(outputs or []) if c.name == name]
        if len(matches) != 1:
            return None  # unknown, opaque or ambiguous output (reported by the validator)
        index = matches[0]
        arms = (
            source.set_operation_scopes
            if isinstance(source.expression, exp.SetOperation)
            else [source]
        )
        # A set-operation value keeps its domain only when every arm reads the same field.
        origins = {self._output_field(self.outputs(arm), index, seen, text_cast) for arm in arms}
        return origins.pop() if len(origins) == 1 else None

    def _output_field(
        self,
        outputs: list[OutputColumn] | None,
        index: int,
        seen: frozenset[tuple[int, str, str]],
        text_cast: bool,
    ) -> FieldRef | None:
        if outputs is None or index >= len(outputs):
            return None
        output = outputs[index]
        if output.origin is not None:
            return None if text_cast and output.origin not in self.string_fields else output.origin
        if output.expression is None:
            return None
        return self.field(output.scope, output.expression, seen, text_cast)


def _column_aliases(scope: Scope) -> list[str]:
    """Column alias list (`WITH r(n) AS ...`, `AS q(a, b)`) naming this query's outputs. An
    arm of a set operation takes the list of the whole operation; sqlglot resolves a
    recursive CTE's self-reference to an arm, which must still see the CTE's names."""
    expression = scope.expression
    while isinstance(expression.parent, exp.SetOperation):
        expression = expression.parent
    holder = expression.parent
    alias = holder.args.get("alias") if isinstance(holder, exp.CTE | exp.Subquery) else None
    if isinstance(alias, exp.TableAlias) and alias.columns:
        return [c.name for c in alias.columns]
    return list(scope.outer_columns)


def _column_ref(column: JoinedColumn) -> exp.Column:
    """How a `*` output refers back to its FROM column: qualified for one origin, by bare
    name for a merged USING column (which resolves through joined_columns)."""
    if len(column.aliases) == 1:
        return exp.column(column.name, table=column.aliases[0], quoted=True)
    return exp.column(column.name, quoted=True)
