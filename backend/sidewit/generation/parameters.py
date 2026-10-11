"""Parse string-valued provider inputs. Numbers convert only when exact (exact_number); any
other text is kept as is, so validation rejects it and the model repairs it."""

from sidewit.domain.params import exact_number


def convert_parameter(value: str, kind: str) -> str | int | float | bool:
    if kind in {"integer", "number"}:
        number = exact_number(value)
        if number is None or (kind == "integer" and not isinstance(number, int)):
            return value
        return number
    if kind == "boolean" and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value
