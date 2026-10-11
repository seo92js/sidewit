import re
from decimal import ROUND_HALF_UP, Decimal, localcontext
from functools import lru_cache
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect as SqlglotDialect
from sqlglot.errors import SqlglotError

from sidewit.domain.params import exact_number

Dialect = Literal["postgresql"]

# Sidewit dialect name -> sqlglot dialect name. v0.1 officially supports PostgreSQL only.
_SQLGLOT_DIALECTS: dict[str, str] = {"postgresql": "postgres"}

# Keywords that cannot be used as unquoted table/column names. sqlglot's parser is lenient
# (it accepts `SELECT order FROM t`), so the database's own list decides. PostgreSQL:
# "reserved" and "reserved (can be function or type)" in the SQL Key Words appendix.
_RESERVED: dict[str, frozenset[str]] = {
    "postgresql": frozenset(
        """
        all analyse analyze and any array as asc asymmetric authorization binary both case
        cast check collate collation column concurrently constraint create cross
        current_catalog current_date current_role current_schema current_time
        current_timestamp current_user default deferrable desc distinct do else end except
        false fetch for foreign freeze from full grant group having ilike in initially inner
        intersect into is isnull join lateral leading left like limit localtime
        localtimestamp natural not notnull null offset on only or order outer overlaps
        placing primary references returning right select session_user similar some
        symmetric system_user table tablesample then to trailing true union unique user
        using variadic verbose when where window with
        """.split()
    )
}


def to_sqlglot(dialect: Dialect) -> str:
    return _SQLGLOT_DIALECTS[dialect]


@lru_cache
def _sqlglot_dialect(dialect: Dialect) -> SqlglotDialect:
    return SqlglotDialect.get_or_raise(to_sqlglot(dialect))


def normalized_name(identifier: exp.Identifier, dialect: Dialect) -> str:
    """The name the database stores: PostgreSQL folds unquoted identifiers to lower case and
    keeps quoted ones as written."""
    return _sqlglot_dialect(dialect).normalize_identifier(identifier.copy()).this


def identifier(name: str, dialect: Dialect) -> exp.Identifier:
    """An identifier that resolves to exactly `name` (a stored, already normalized name).
    Quoted only when required: unsafe characters, a name the dialect would case-fold, or a
    keyword that does not parse back as the same table and column name."""
    return exp.to_identifier(name, quoted=_needs_quotes(name, dialect))


@lru_cache(maxsize=4096)
def _needs_quotes(name: str, dialect: Dialect) -> bool:
    unquoted = exp.to_identifier(name)
    if unquoted.quoted or normalized_name(unquoted, dialect) != name:
        return True
    if name.lower() in _RESERVED[dialect]:
        return True
    read = to_sqlglot(dialect)
    try:
        column = sqlglot.parse_one(f"SELECT {name} FROM t", read=read).expressions[0]
        table = sqlglot.parse_one(f"SELECT 1 FROM {name}", read=read).find(exp.Table)
    except (SqlglotError, IndexError):
        return True
    return not (
        isinstance(column, exp.Column)
        and column.name == name
        and isinstance(table, exp.Table)
        and table.name == name
    )


def render_sql(expression: exp.Expression, dialect: Dialect) -> str:
    """SQL text with Sidewit's canonical `:name` placeholders, whatever style the dialect's
    generator would use (psycopg's %(name)s for PostgreSQL)."""
    named = expression.transform(
        lambda node: exp.var(f":{node.name}") if isinstance(node, exp.Placeholder) else node
    )
    return named.sql(dialect=to_sqlglot(dialect))


def unbounded_text(to: exp.DataType) -> bool:
    """`::text` or `::varchar` without a length: casting a string to it keeps the string.
    A length (`::varchar(1)`) or `char` (which is `char(1)`) can truncate."""
    return to.is_type(exp.DataType.Type.TEXT) or (
        to.is_type(exp.DataType.Type.VARCHAR, exp.DataType.Type.NVARCHAR) and not to.expressions
    )


Scalar = str | int | float | bool


_INTEGER_BITS = {
    exp.DataType.Type.SMALLINT: 16,
    exp.DataType.Type.INT: 32,
    exp.DataType.Type.BIGINT: 64,
}


def cast_result_type(source: str, to: exp.DataType) -> str | None:
    """Verified source/target pairs, including modifier validity. None means unsupported.

    These are scalar value categories, not a replacement for PostgreSQL type resolution.
    CHAR, floating point, user-defined types and numeric-to-text formatting are excluded.
    """
    params = _type_params(to)
    if params is None:
        return None
    if to.is_type(exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR):
        valid = not params or (
            to.is_type(exp.DataType.Type.VARCHAR)
            and len(params) == 1
            and 1 <= params[0] <= 10485760
        )
        return "string" if source == "string" and valid else None
    if to.this in _INTEGER_BITS:
        return "integer" if source in {"string", "integer", "number"} and not params else None
    if to.is_type(exp.DataType.Type.DECIMAL):
        valid = not params or (
            len(params) <= 2
            and 1 <= params[0] <= 1000
            and (len(params) == 1 or -1000 <= params[1] <= 1000)
        )
        return "number" if source in {"string", "integer", "number"} and valid else None
    if to.is_type(exp.DataType.Type.BOOLEAN):
        return "boolean" if source in {"string", "boolean"} and not params else None
    return None


def cast_value(value: Scalar, to: exp.DataType) -> Scalar | None:
    """Evaluate only cast pairs declared by cast_result_type; None is never success.

    Python scalar types preserve string versus numeric input between casts. SQL numeric
    literals must enter as numbers. Integral narrowing validates the target's bounds.
    """
    source = (
        "boolean"
        if isinstance(value, bool)
        else "integer"
        if isinstance(value, int)
        else "number"
        if isinstance(value, float)
        else "string"
    )
    result = cast_result_type(source, to)
    if result is None:
        return None
    params = _type_params(to) or []
    if result == "string":
        return value[: params[0]] if params else value
    if result == "boolean":
        if isinstance(value, bool):
            return value
        word = value.strip().lower()
        if word in {"1", "on"}:
            return True
        if word in {"0", "off", "of"}:
            return False
        matches = {
            flag
            for full, flag in {"true": True, "yes": True, "false": False, "no": False}.items()
            if word and full.startswith(word)
        }
        return matches.pop() if len(matches) == 1 else None
    if isinstance(value, str):
        word = value.strip()
        if result == "integer" and not re.fullmatch(r"[+-]?[0-9]+", word):
            return None  # text-to-int does not accept decimal points or exponents
        number = exact_number(word)
        if number is None:
            return None
    else:
        number = value
    decimal = Decimal(repr(number) if isinstance(number, float) else number)
    if result == "integer":
        integer = int(_round(decimal, 0))
        bits = _INTEGER_BITS[to.this]
        return integer if -(2 ** (bits - 1)) <= integer < 2 ** (bits - 1) else None
    if not params:
        return number
    precision, scale = params[0], params[1] if len(params) > 1 else 0
    rounded = _round(decimal, scale)
    if rounded and rounded.copy_abs().adjusted() >= precision - scale:
        return None
    return exact_number(str(rounded))


def _round(number: Decimal, scale: int) -> Decimal:
    # PostgreSQL numeric precision is independent of Python's default 28-digit context.
    # Reserve one extra digit for carry (9.99 -> 10.0); do not mutate global precision.
    with localcontext() as context:
        context.prec = max(28, len(number.as_tuple().digits), number.adjusted() + scale + 2)
        return number.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)


def _type_params(to: exp.DataType) -> list[int] | None:
    """`varchar(20)` -> [20], `numeric(4, 2)` -> [4, 2]."""
    params = []
    for param in to.expressions:
        value = param.this if isinstance(param, exp.DataTypeParam) else param
        if not (isinstance(value, exp.Literal) and value.is_int):
            return None
        params.append(int(value.this))
    return params


def user_type_identity(type_: str | exp.DataType) -> tuple[str, str] | None:
    """(schema, name) a user-defined type name refers to, as PostgreSQL stores it: `mood`,
    `public.mood` and `"Public"."Mood"` are distinct only by their normalized parts; an
    unqualified name is taken as `public`. A Context field type (text) and a cast target in
    SQL (sqlglot DataType) map to the same identity. None for built-in or other types."""
    if isinstance(type_, str):
        try:
            type_ = exp.DataType.build(type_, dialect="postgres", udt=True)
        except (SqlglotError, ValueError):
            return None
    if type_.this != exp.DataType.Type.USERDEFINED:
        return None
    kind = type_.args.get("kind")
    # DataType.build stores unknown names as text; parsed CAST targets use AST nodes.
    if isinstance(kind, str):
        try:
            column = sqlglot.parse_one(kind, read="postgres", into=exp.Column)
        except SqlglotError:
            return None
        parts = list(column.parts)
    else:
        parts = []
        while isinstance(kind, exp.Dot):
            parts.insert(0, kind.expression)
            kind = kind.this
        parts.insert(0, kind)
    if not 1 <= len(parts) <= 2 or not all(isinstance(p, exp.Identifier) for p in parts):
        return None
    names = [normalized_name(p, "postgresql") for p in parts]
    return (names[0], names[1]) if len(names) == 2 else ("public", names[0])
