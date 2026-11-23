"""Optional execution-result oracle for isolated, read-only PostgreSQL eval databases.

This module is imported only by the evaluation CLI's --execution-url path. Sidewit's API
does not import it or acquire a database connection.
"""

from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import asyncpg
from sqlalchemy import BigInteger, Boolean, Date, DateTime, Numeric, Text, bindparam, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from sidewit.domain.operation import SqlOperation
from sidewit.domain.params import ParamType
from sidewit.eval.cases import GoldenCase
from sidewit.eval.runner import EvalSetupError, ExecutionComparison

_MAX_ROWS = 1000
_PARAM_TYPES = {
    "string": Text(),
    "integer": BigInteger(),
    "number": Numeric(),
    "boolean": Boolean(),
    "date": Date(),
    "date-time": DateTime(timezone=True),
}


class _TooManyRows(Exception):
    pass


class PostgresExecutionEvaluator:
    def __init__(self, dsn: str):
        try:
            url = make_url(dsn)
        except (ArgumentError, ValueError) as e:
            raise EvalSetupError("Execution URL is malformed.") from e
        if url.drivername != "postgresql+asyncpg" or not (url.database or "").endswith("_eval"):
            raise EvalSetupError(
                "Execution URL must use postgresql+asyncpg and a database ending in _eval."
            )
        self._url = url
        self._engine: AsyncEngine | None = None
        self._expected: dict[str, tuple[list[tuple[Any, ...]], bool]] = {}

    async def __aenter__(self):
        self._engine = create_async_engine(self._url, poolclass=NullPool)
        try:
            async with self._engine.connect() as conn:
                superuser = await conn.scalar(text("SELECT current_setting('is_superuser')"))
                if superuser != "off":
                    raise EvalSetupError("Execution database role must not be a superuser.")
        except EvalSetupError:
            await self._engine.dispose()
            raise
        except SQLAlchemyError as e:
            await self._engine.dispose()
            raise EvalSetupError(f"Execution database is unavailable ({type(e).__name__}).") from e
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    async def prepare(self, cases: list[GoldenCase]) -> None:
        for case in cases:
            if case.expect.status != "ok":
                continue
            if case.expect.steps is not None:
                raise EvalSetupError(
                    "Execution evaluation currently supports single-step cases only."
                )
            if case.reference is None:
                raise EvalSetupError(
                    f"Case {case.id} needs reference SQL for execution evaluation."
                )
            try:
                rows = await self._rows(
                    case.reference.statement,
                    case.reference.parameters,
                    case.reference.parameter_types,
                )
            except (SQLAlchemyError, asyncpg.PostgresError, _TooManyRows, ValueError) as e:
                raise EvalSetupError(
                    f"Reference SQL for {case.id} failed ({type(e).__name__})."
                ) from e
            self._expected[case.id] = (rows, case.reference.ordered)

    async def compare(self, case_id: str, operation: SqlOperation) -> ExecutionComparison:
        expected, ordered = self._expected[case_id]
        try:
            actual = await self._rows(
                operation.statement, operation.parameters, operation.parameter_types
            )
        except (SQLAlchemyError, asyncpg.PostgresError, _TooManyRows, ValueError) as e:
            return ExecutionComparison(
                matched=False,
                expected_row_count=len(expected),
                error=f"Generated SQL failed ({type(e).__name__}).",
            )
        left = [_normalize_row(row) for row in expected]
        right = [_normalize_row(row) for row in actual]
        matched = left == right if ordered else Counter(left) == Counter(right)
        return ExecutionComparison(
            matched=matched,
            expected_row_count=len(expected),
            actual_row_count=len(actual),
        )

    async def _rows(
        self, statement: str, parameters: dict[str, Any], parameter_types: dict[str, ParamType]
    ) -> list[tuple[Any, ...]]:
        assert self._engine is not None
        if parameters.keys() != parameter_types.keys():
            raise ValueError("Parameter values and types differ.")
        bound = text(statement).bindparams(
            *(bindparam(name, type_=_PARAM_TYPES[kind]) for name, kind in parameter_types.items())
        )
        values = {
            name: _convert(value, parameter_types[name]) for name, value in parameters.items()
        }
        async with self._engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                await conn.execute(text("SET LOCAL statement_timeout = '5s'"))
                return await _limited_rows(conn, bound, values)


async def _limited_rows(conn: AsyncConnection, statement: Any, values: dict) -> list[tuple]:
    rows = []
    async with conn.stream(statement, values) as result:
        async for row in result:
            rows.append(tuple(row))
            if len(rows) > _MAX_ROWS:
                raise _TooManyRows
    return rows


def _convert(value: Any, kind: ParamType) -> Any:
    if kind == "date":
        return date.fromisoformat(value)
    if kind == "date-time":
        # Sidewit accepts lowercase t/z (RFC 3339); fromisoformat rejects a lowercase z.
        return datetime.fromisoformat(value.upper())
    if kind == "number":
        return Decimal(str(value))
    return value


def _normalize_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(_normalize(value) for value in row)


def _normalize(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value))
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value
    if isinstance(value, list | tuple):
        return tuple(_normalize(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, _normalize(item)) for key, item in value.items()))
    return value
