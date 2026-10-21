import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

ParamType = Literal["string", "integer", "number", "boolean", "date", "date-time"]

PARAM_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1
_MAX_EXPONENT = 400  # float covers about 1e-324 .. 1.8e308


def valid_json_unicode(value: Any) -> bool:
    """Check decoded JSON strings, including keys in untyped schemas/arguments, without
    recursive traversal. Lone surrogates cannot be written to UTF-8 files or responses.
    """
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return False
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return True


def exact_number(text: str) -> int | float | None:
    """The number a decimal literal denotes, or None when it is not finite or a float would
    round it. The one lossless text-to-number rule for model output and DDL: an integral
    value in PostgreSQL's bigint range is an int ("9007199254740993"), anything else a float
    only if exact ("0.1", "1e30"). "1.0000000000000001", "9007199254740993.5" and "1e999"
    are None rather than silently rounded."""
    if not _DECIMAL.fullmatch(text):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:  # e.g. an exponent beyond Decimal's range
        return None
    # Far beyond float range or precision; also keeps integral checks cheap.
    if not number.is_finite() or not -_MAX_EXPONENT <= number.adjusted() <= _MAX_EXPONENT:
        return None
    if number == number.to_integral_value() and _INT64_MIN <= number <= _INT64_MAX:
        return int(number)
    value = float(number)
    if not math.isfinite(value) or Decimal(repr(value)) != number:
        return None
    return value


def value_matches_type(value: Any, param_type: ParamType) -> bool:
    match param_type:
        case "string":
            return isinstance(value, str) and valid_json_unicode(value)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return (
                isinstance(value, int | float)
                and not isinstance(value, bool)
                and (not isinstance(value, float) or math.isfinite(value))
            )
        case "boolean":
            return isinstance(value, bool)
        case "date":
            if not isinstance(value, str) or not _DATE.match(value):
                return False
            try:
                date.fromisoformat(value)
            except ValueError:
                return False
            return True
        case "date-time":
            if not isinstance(value, str) or "T" not in value:
                return False
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return False
            return parsed.tzinfo is not None


def matches_allowed_value(value: Any, allowed: Any) -> bool:
    """Exact text/boolean equality, numeric equality without a float round-trip.

    SQL numeric literals arrive as strings; typed runtime values and JSON integers retain
    their precision. Booleans must never compare equal to integer 0/1.
    """
    if isinstance(allowed, bool) or isinstance(value, bool):
        return type(value) is type(allowed) and value == allowed
    if isinstance(allowed, int | float):
        if not isinstance(value, str | int | float):
            return False
        try:
            left, right = Decimal(str(value)), Decimal(str(allowed))
            return left.is_finite() and right.is_finite() and left == right
        except InvalidOperation:
            return False
    return value == allowed
