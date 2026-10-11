"""Supported cast pairs agree with PostgreSQL, including rejection boundaries."""

from decimal import Decimal

import pytest
import sqlglot
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from sidewit.domain.dialect import cast_value
from sidewit.validation.input_schema import input_schema_for
from tests.test_validation_boundaries import create_context


@pytest.mark.parametrize(
    ("value", "target", "sql", "valid"),
    [
        ("1.0", "integer", "'1.0'::integer", False),
        ("1e2", "integer", "'1e2'::integer", False),
        (" +12 ", "integer", "' +12 '::integer", True),
        (32767, "smallint", "32767::smallint", True),
        (32768, "smallint", "32768::smallint", False),
        (-32768, "smallint", "(-32768)::smallint", True),
        (2147483648, "integer", "2147483648::integer", False),
        (1.5, "integer", "1.5::integer", True),
        (-1.5, "integer", "(-1.5)::integer", True),
        ("AX", "varchar(1)", "'AX'::varchar(1)", True),
        (1.235, "numeric(4,2)", "1.235::numeric(4,2)", True),
        (0.00999, "numeric(3,5)", "0.00999::numeric(3,5)", True),
        (0.01, "numeric(3,5)", "0.01::numeric(3,5)", False),
        (1, "numeric(40,30)", "1::numeric(40,30)", True),
        ("yes", "boolean", "'yes'::boolean", True),
    ],
)
async def test_cast_pair_matches_postgres(database_url, value, target, sql, valid):
    to = sqlglot.parse_one("CAST(1 AS " + target + ")", read="postgres").to
    actual = cast_value(value, to)
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            if not valid:
                with pytest.raises(DBAPIError):
                    await conn.exec_driver_sql("SELECT " + sql)
                assert actual is None
            else:
                expected = (await conn.exec_driver_sql("SELECT " + sql)).scalar_one()
                assert actual is not None
                assert (
                    Decimal(str(actual)) if isinstance(expected, Decimal) else actual
                ) == expected
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("target", "value", "allowed"),
    [
        ("integer", "1.0", 1),
        ("smallint", 40000, 40000),
    ],
)
async def test_invalid_runtime_cast_is_rejected(client, target, value, allowed):
    ctx = await create_context(
        client,
        {
            "resources": [
                {
                    "name": "t",
                    "fields": [{"name": "code", "type": "numeric", "allowed_values": [allowed]}],
                }
            ]
        },
    )
    typ = "string" if isinstance(value, str) else "integer"
    sql = f"SELECT code FROM t WHERE code=CAST(:s AS {target})"
    r = await client.post(
        "/api/v1/tools",
        json={
            "context_id": ctx["id"],
            "name": "cast_probe",
            "description": "probe",
            "operation": {"type": "sql", "statement": sql},
            "input_schema": input_schema_for({"s": typ}),
        },
    )
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/tools/cast_probe/invoke", json={"arguments": {"s": value}})
    assert r.status_code == 422, r.text


@pytest.mark.parametrize(
    ("sql", "allowed", "valid"),
    [
        ("1.5::integer", [2], True),
        ("'1.5'::integer", [2], False),
        ("'A '::char(3)::text", ["A"], False),
        ("'12'::integer::text", ["12"], False),
        ("'AX'::varchar(2)::varchar(1)", ["A"], True),
    ],
)
async def test_chain_preserves_type_or_rejects_explicitly(client, sql, allowed, valid):
    ctx = await create_context(
        client,
        {
            "resources": [
                {
                    "name": "t",
                    "fields": [
                        {
                            "name": "code",
                            "type": "text" if isinstance(allowed[0], str) else "integer",
                            "allowed_values": allowed,
                        }
                    ],
                }
            ]
        },
    )
    r = await client.post(
        "/api/v1/validate",
        json={
            "context_id": ctx["id"],
            "operation": {"statement": "SELECT code FROM t WHERE code=" + sql},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is valid, r.text
