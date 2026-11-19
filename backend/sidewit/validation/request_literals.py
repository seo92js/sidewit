"""Generate-only check: values written in the user's request must be named parameters.

Conservative text matching, not semantic validation. A literal is reported only when its
exact text appears in the request, is not part of a date/duration expression there ("1월",
"30 days" are periods, resolved into date parameters), and is not a context constant. A
constant is known structurally (a Rule condition or metric fixed filter on that field) or,
for prose-only rules, by being mentioned in a Rule or description. It never rewrites SQL
and never runs for manually authored Tools.
"""

import functools
import re
from collections.abc import Collection, Iterable

from sqlglot import exp

from sidewit.domain.context import ContextDefinition
from sidewit.domain.issue import Issue

# A number directly followed by one of these in the request is a date or duration.
_DATE_UNIT = re.compile(
    r"\s*(년|개월|월|주일|주|일|시간|시|분|초"
    r"|(years?|months?|weeks?|days?|hours?|minutes?|seconds?)\b)",
    re.IGNORECASE,
)


def request_literal_issues(
    data: Iterable[tuple[exp.Expression, str]],
    definition: ContextDefinition,
    question: str,
    constant_literals: Collection[int],
) -> list[Issue]:
    """`data`: literals used as values, each with the ref it is compared against.
    `constant_literals`: ids of literals matched to a registered constant on their field."""
    constants = [rule.text for rule in definition.rules]
    for resource in definition.resources:
        if resource.description:
            constants.append(resource.description)
        constants.extend(f.description for f in resource.fields if f.description)

    issues: dict[str, Issue] = {}
    for node, ref in data:
        literal = node.this if isinstance(node, exp.Neg) else node
        if not isinstance(literal, exp.Literal):
            continue
        value = ("-" if literal is not node else "") + literal.this
        if not literal.this or id(literal) in constant_literals:
            continue
        if not _requested(value, question, numeric=literal.is_number):
            continue
        if any(_mentioned(value, text) for text in constants):
            continue
        issues.setdefault(
            ref,
            Issue(
                code="request_value_literal",
                message="A value explicitly written in the user request must use a named "
                "parameter instead of a SQL literal. Keep context/Business Rule constants "
                "literal.",
                ref=ref,
            ),
        )
    return list(issues.values())


def _requested(value: str, question: str, *, numeric: bool) -> bool:
    for match in _pattern(value).finditer(question):
        if numeric and _DATE_UNIT.match(question, match.end()):
            continue
        return True
    return False


def _mentioned(value: str, text: str) -> bool:
    return _pattern(value, ignore_case=True).search(text) is not None


@functools.lru_cache(maxsize=1024)
def _pattern(value: str, ignore_case: bool = False) -> re.Pattern[str]:
    # ASCII boundaries: 30 does not match inside 300 or 30.5, US not inside USER. Korean or
    # Japanese suffixes ("서울지역") need no whitespace.
    return re.compile(
        r"(?<![A-Za-z0-9_.+-])" + re.escape(value) + r"(?![A-Za-z0-9_]|\.[0-9])",
        re.IGNORECASE if ignore_case else 0,
    )
