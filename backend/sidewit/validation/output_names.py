"""Names PostgreSQL gives unaliased output columns (FigureColname in parse_target.c).

Only names that are certain are returned; anything else is None, so a reference to it is
rejected rather than guessed.
"""

from sqlglot import exp

from sidewit.validation.functions import call_names

# Strength as in PostgreSQL: 2 for a name from the expression itself, 1 for a fallback such
# as "case", which an enclosing cast replaces with the type name.
_Named = tuple[str, int]

_KEYWORDS: dict[type[exp.Expression], str] = {
    exp.CurrentDate: "current_date",
    exp.CurrentTime: "current_time",
    exp.Localtime: "localtime",
    exp.Localtimestamp: "localtimestamp",
    exp.Exists: "exists",
    exp.Array: "array",
    exp.Nullif: "nullif",
    exp.Coalesce: "coalesce",
    exp.Greatest: "greatest",
    exp.Least: "least",
}


def output_name(projection: exp.Expression) -> str | None:
    if isinstance(projection, exp.Alias):
        return projection.alias
    named = _figure(projection)
    return named[0] if named else None


def _figure(node: exp.Expression) -> _Named | None:
    if isinstance(node, exp.Paren | exp.Bracket | exp.Window | exp.Filter | exp.Collate):
        # Subscripts, OVER and FILTER keep the name of the expression they apply to.
        return _figure(node.this)
    if isinstance(node, exp.Column):
        return (node.name, 2) if not node.is_star else None
    if isinstance(node, exp.Cast):
        inner = _figure(node.this)
        return inner if inner and inner[1] == 2 else None  # else the type's internal name
    if isinstance(node, exp.Case):
        default = node.args.get("default")
        if default is None or isinstance(default, exp.Literal | exp.Null | exp.Boolean):
            return ("case", 1)
        inner = _figure(default)
        return inner if inner and inner[1] == 2 else None
    if isinstance(node, exp.Subquery):
        query = node.this
        first = query.expressions[0] if isinstance(query, exp.Select) else None
        name = output_name(first) if first is not None else None
        return (name, 2) if name else None
    if type(node) in _KEYWORDS:
        return (_KEYWORDS[type(node)], 2)
    if isinstance(node, exp.Anonymous):
        return (node.name.lower(), 2)
    if isinstance(node, exp.Func) and type(node) in call_names():
        return (call_names()[type(node)], 2)
    return None
