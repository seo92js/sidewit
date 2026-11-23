"""Closed SQL function policy, in PostgreSQL function names.

A call is identified by the name PostgreSQL resolves: unquoted names fold to lower case,
quoted names are exact (`"LOWER"(x)` is not lower). The node types SQLGlot produces are
derived from the allowed names, not maintained by hand.

This limits the operations Sidewit returns; it is not a database privilege boundary.
An application's search_path, overloads, casts and operators remain its responsibility.
"""

import functools

import sqlglot
from sqlglot import exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from sidewit.domain.dialect import reserved_keyword
from sidewit.domain.issue import Issue

POSTGRES_FUNCTIONS = frozenset(
    """
abs acos asin atan atan2 ceil ceiling exp floor ln log log10 power pow random round sign
sqrt trunc
avg count max min sum array_agg json_agg jsonb_agg string_agg bool_and bool_or every
stddev stddev_pop stddev_samp variance var_pop var_samp percentile_cont percentile_disc
row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value
nth_value
coalesce nullif greatest least
lower upper initcap left right length char_length character_length bit_length
octet_length concat concat_ws replace reverse split_part starts_with strpos position
substring substr trim btrim ltrim rtrim translate md5 encode decode regexp_replace
regexp_like
array_position array_length array_to_string cardinality unnest generate_series
date date_trunc date_part extract to_char to_timestamp make_date make_timestamptz age now
clock_timestamp statement_timestamp transaction_timestamp timeofday
json_array json_build_array json_build_object json_array_elements json_array_length json_each
json_typeof json_extract_path json_extract_path_text jsonb_build_array jsonb_build_object
jsonb_array_elements jsonb_array_length jsonb_each jsonb_typeof to_json to_jsonb
""".split()
)

POSTGIS = frozenset(
    """
st_area st_astext st_asewkt st_asbinary st_asgeojson st_centroid st_contains
st_coveredby st_covers st_crosses st_distance st_distancesphere st_distancespheroid
st_dwithin st_envelope st_equals st_geometrytype st_geomfromtext st_geomfromewkt
st_geomfromgeojson st_intersects st_isempty st_isvalid st_length st_makeenvelope
st_makeline st_makepoint st_point st_setsrid st_srid st_transform st_touches
st_within st_x st_y st_z
""".split()
)

# PostgreSQL syntax and operators that SQLGlot represents as function nodes.
SYNTAX: frozenset[type[exp.Func]] = frozenset(
    {
        exp.And,
        exp.Or,
        exp.Case,
        exp.If,  # a CASE branch
        exp.Cast,  # CAST(x AS t), x::t
        exp.Collate,
        exp.Array,  # ARRAY[...], ARRAY(SELECT ...)
        exp.Exists,
        exp.CurrentDate,
        exp.CurrentTime,
        exp.CurrentTimestamp,
        exp.Localtime,
        exp.Localtimestamp,
        exp.JSONExtract,  # ->
        exp.JSONExtractScalar,  # ->>
        exp.JSONBExtract,  # #>
        exp.JSONBExtractScalar,  # #>>
        exp.JSONBContainsTopKey,  # ?
        exp.ArrayContainsAll,  # @> (arrays and jsonb)
        exp.RegexpLike,  # ~, !~
        exp.Pow,  # ^
    }
)

# Quantifiers SQLGlot leaves as anonymous calls: x = ALL(...), x = SOME(...).
_QUANTIFIERS = frozenset({"all", "some"})
# PostgreSQL "column name" keywords: their parentheses are grammar (EXISTS (, ROW (, EXTRACT (),
# never a call by name. Reserved keywords are in domain.dialect; FILTER ( is unreserved.
_COL_NAME_KEYWORDS = frozenset(
    """
between bigint bit boolean char character coalesce dec decimal exists extract float greatest
grouping inout int integer interval json json_array json_arrayagg json_exists json_object
json_objectagg json_query json_scalar json_serialize json_table json_value least merge_action
national nchar none normalize nullif numeric out overlay position precision real row setof
smallint substring time timestamp treat trim values varchar xmlattributes xmlconcat
xmlelement xmlexists xmlforest xmlnamespaces xmlparse xmlpi xmlroot xmlserialize xmltable
filter
""".split()
)
# Names SQLGlot parses as function calls.
_CALLABLE = frozenset(
    k.lower() for k in (*Postgres.Parser.FUNCTIONS, *Postgres.Parser.FUNCTION_PARSERS)
)


def function_issues(ast: exp.Expression, statement: str, extensions: list[str]) -> list[Issue]:
    """`ast` has normalized identifiers; `statement` is the text it was parsed from."""
    names = POSTGRES_FUNCTIONS | (POSTGIS if "postgis" in extensions else frozenset())
    nodes = _node_types(names)
    issues: dict[str, Issue] = {}

    def reject(name: str) -> None:
        issues.setdefault(
            name,
            Issue(
                code="function_not_allowed",
                message=f"Function '{name}' is not supported by the SQL function policy.",
                ref=name,
            ),
        )

    for node in ast.find_all(exp.Func):
        # Schema-qualified calls can resolve to entirely different implementations.
        table = node.find_ancestor(exp.Table)
        if isinstance(node.parent, exp.Dot) or (
            table is not None and (table.args.get("db") or table.args.get("catalog"))
        ):
            reject(_node_name(node))
        elif isinstance(node, exp.Anonymous):
            name = _node_name(node)
            if name not in names and name not in _QUANTIFIERS:
                reject(name)
        elif type(node) not in nodes:
            reject(_node_name(node))

    # Typed nodes lose the written name (truncate -> Trunc, "LOWER" -> Lower): check every
    # call by the name PostgreSQL resolves, skipping names the tree uses otherwise (aliases).
    named = {i.meta["start"] for i in ast.find_all(exp.Identifier) if "start" in i.meta}
    tokens = sqlglot.tokenize(statement, read="postgres")
    for token, following in zip(tokens, tokens[1:], strict=False):
        if following.token_type != TokenType.L_PAREN or token.start in named:
            continue
        if token.token_type in Postgres.Parser.TYPE_TOKENS:
            continue  # varchar(10), or a function-style cast such as date(x): a cast
        if token.token_type == TokenType.IDENTIFIER:
            if token.text not in names:
                reject(token.text)
            continue
        name = token.text.lower()
        grammar = reserved_keyword(name, "postgresql") or name in _COL_NAME_KEYWORDS
        if name in _CALLABLE and not grammar and name not in names:
            reject(name)
    return list(issues.values())


def _node_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return node.this.this if isinstance(node.this, exp.Identifier) else node.name.lower()
    return node.sql_name().lower()


# SQL-standard call syntax that PostgreSQL names after another function (TRIM -> btrim) or that
# SQLGlot merges with a call of a different name (POSITION -> strpos).
_SPECIAL_FORMS = (
    "extract(year FROM x)",
    "position('a' IN x)",
    "substring(x FROM 1 FOR 2)",
    "trim(BOTH FROM x)",
    "trim(LEADING 'a' FROM x)",
    "overlay(x PLACING 'a' FROM 1)",
)


@functools.cache
def call_names() -> dict[type[exp.Func], str]:
    """Node type -> the function name it was written with, when that is certain: exactly one
    allowed name parses to it, and no operator or special syntax does (`x % y` and `mod(x, y)`
    are both Mod; `ceil` and `ceiling` are both Ceil)."""
    names: dict[type[exp.Func], set[str]] = {}
    for name in POSTGRES_FUNCTIONS | POSTGIS:
        for args in ("", "x", "x, y", "x, y, z"):
            node = _projection(f"{name}({args})")
            if isinstance(node, exp.Func) and not isinstance(node, exp.Anonymous):
                names.setdefault(type(node), set()).add(name)
    special = {type(_projection(form)) for form in _SPECIAL_FORMS}
    return {
        kind: next(iter(written))
        for kind, written in names.items()
        if len(written) == 1
        and kind not in SYNTAX
        and kind not in special
        and not issubclass(kind, exp.Binary | exp.Unary | exp.Predicate)
    }


def _projection(sql: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(f"SELECT {sql}", read="postgres").expressions[0]
    except SqlglotError:
        return None


@functools.cache
def _node_types(names: frozenset[str]) -> frozenset[type[exp.Func]]:
    """Node types SQLGlot produces for calls of `names` (some differ by arity or position)."""
    types: set[type[exp.Func]] = set(SYNTAX)
    for name in names:
        for args in ("x", "x, y", "x, y, z"):
            for sql in (f"SELECT {name}({args})", f"SELECT * FROM {name}({args}) AS t"):
                try:
                    tree = sqlglot.parse_one(sql, read="postgres")
                except SqlglotError:
                    continue
                types.update(
                    type(f) for f in tree.find_all(exp.Func) if not isinstance(f, exp.Anonymous)
                )
    return frozenset(types)
