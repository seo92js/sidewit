"""Bound untrusted SQL before recursive parsing and before recursive AST analysis."""

from sqlglot import Dialect, exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

MAX_NESTING = 32
MAX_TOKENS = 20000
MAX_AST_DEPTH = 64
MAX_AST_NODES = 10000
MAX_ANALYSIS_WORK = 50000
COMPLEXITY_MESSAGE = "SQL exceeds the supported nesting or complexity limit."


class SqlComplexityError(SqlglotError):
    pass


class AnalysisBudget:
    """Bound lineage work as well as SQL size; a small CTE DAG can expand exponentially."""

    def __init__(self) -> None:
        self.remaining = MAX_ANALYSIS_WORK

    def consume(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise SqlComplexityError(COMPLEXITY_MESSAGE)


def parse_sql(statement: str, dialect: str) -> list[exp.Expression | None]:
    """Tokenize once, ignoring parentheses in strings/comments; never change recursionlimit.
    SQLGlot may also recurse on shapes without parentheses, so catch its recursion failures
    and bound the resulting trees before handing them to the rest of the application.
    """
    try:
        grammar = Dialect.get_or_raise(dialect)
        tokens = grammar.tokenize(statement)
        if len(tokens) > MAX_TOKENS:
            raise SqlComplexityError(COMPLEXITY_MESSAGE)
        depth = 0
        for token in tokens:
            if token.token_type in {TokenType.L_PAREN, TokenType.L_BRACKET}:
                depth += 1
                if depth > MAX_NESTING:
                    raise SqlComplexityError(COMPLEXITY_MESSAGE)
            elif token.token_type in {TokenType.R_PAREN, TokenType.R_BRACKET}:
                depth = max(0, depth - 1)
        trees = grammar.parser().parse(tokens, statement)
        pending = [(tree, 1) for tree in trees if tree is not None]
        count = 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if depth > MAX_AST_DEPTH or count > MAX_AST_NODES:
                raise SqlComplexityError(COMPLEXITY_MESSAGE)
            pending.extend((child, depth + 1) for child in node.iter_expressions())
        return trees
    except RecursionError:
        raise SqlComplexityError(COMPLEXITY_MESSAGE) from None
