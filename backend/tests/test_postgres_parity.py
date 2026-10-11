"""Differential test: the structural validator must accept exactly the SQL PostgreSQL itself
accepts. Each statement is planned with EXPLAIN (never executed) against tables that mirror
the Context, in a throwaway schema of the test database.

Only structural verdicts are compared. Context value rules (value_not_allowed) are stricter
than PostgreSQL on purpose, so they are left out. When adding a validator rule about names,
columns or query shape, add the statements that motivated it here.
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from sidewit.domain.context import ContextDefinition
from sidewit.validation.sql_validator import validate_sql_operation

SCHEMA = "sidewit_parity"
DDL = f"""
CREATE SCHEMA {SCHEMA};
CREATE TABLE {SCHEMA}.orders (
    id bigint, status text, flag boolean, amount numeric, tags text[], customer_id bigint
);
CREATE TABLE {SCHEMA}.customers (id bigint, name text);
"""
CONTEXT = ContextDefinition.model_validate(
    {
        "resources": [
            {
                "name": "orders",
                "fields": [
                    {"name": "id", "type": "bigint"},
                    {"name": "status", "type": "text", "allowed_values": ["ACTIVE"]},
                    {"name": "flag", "type": "boolean", "allowed_values": [True]},
                    {"name": "amount", "type": "numeric"},
                    {"name": "tags", "type": "text[]"},
                    {"name": "customer_id", "type": "bigint"},
                ],
            },
            {
                "name": "customers",
                "fields": [{"name": "id", "type": "bigint"}, {"name": "name", "type": "text"}],
            },
        ]
    }
)
# Context value rules, deliberately stricter than PostgreSQL.
VALUE_RULES = {"value_not_allowed"}

STATEMENTS = [
    # JOIN USING / NATURAL
    "SELECT * FROM orders o JOIN customers c USING (id) "
    "UNION ALL SELECT o.id, status, flag, amount, tags, customer_id, name FROM orders o, customers",
    "SELECT * FROM orders o JOIN customers c USING (id) "
    "UNION ALL SELECT o.id, status, flag, amount, tags, customer_id, name, name "
    "FROM orders o, customers",
    "SELECT id FROM orders JOIN customers USING (id)",
    "SELECT id, name FROM orders NATURAL JOIN customers",
    "SELECT o.id, c.id FROM orders o JOIN customers c USING (id)",
    "SELECT c.name FROM orders o JOIN customers c USING (id) WHERE id > 0",
    "SELECT q.id FROM (SELECT * FROM orders JOIN customers USING (id)) q",
    "SELECT q.name FROM (SELECT * FROM orders o JOIN customers c USING (id)) q",
    "SELECT 1 FROM orders o JOIN customers c USING (missing)",
    "SELECT 1 FROM orders o JOIN customers c USING (status)",
    "SELECT 1 FROM orders a JOIN orders b ON true JOIN customers c USING (id)",
    # visibility: correlation and LATERAL
    "SELECT 1 FROM orders o WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = o.customer_id)",
    "SELECT c.id, (SELECT SUM(o.amount) FROM orders o WHERE o.customer_id = c.id) FROM customers c",
    "SELECT 1 FROM orders o JOIN LATERAL (SELECT o.id) q ON true",
    "SELECT x FROM orders o, LATERAL (SELECT o.amount AS x) l",
    "SELECT 1 FROM orders o WHERE EXISTS (SELECT 1 FROM (SELECT o.id) q)",
    "SELECT 1 FROM orders o JOIN (SELECT o.id) q ON true",
    "SELECT 1 FROM orders o JOIN (SELECT id) q ON true",
    "WITH q AS (SELECT o.id FROM customers) SELECT 1 FROM orders o, q",
    # stars, aliases, ambiguity
    "SELECT o.* FROM orders o",
    "SELECT missing.* FROM orders",
    "SELECT id FROM orders, customers",
    "SELECT status FROM (SELECT status, status FROM orders) q",
    "SELECT q.missing FROM (SELECT * FROM orders) q",
    'SELECT id FROM "ORDERS"',
    "SELECT ID FROM ORDERS",
    "SELECT status AS s FROM orders ORDER BY s",
    "SELECT status AS s, COUNT(*) FROM orders GROUP BY s",
    "SELECT status AS s FROM orders WHERE s = 'ACTIVE'",
    # table functions, recursion, set operations
    "SELECT id FROM orders CROSS JOIN LATERAL unnest(tags) AS t",
    "SELECT o.id, t FROM orders o CROSS JOIN LATERAL unnest(o.tags) AS t",
    "SELECT t.x FROM unnest(ARRAY[1, 2]) AS t(x)",
    "SELECT t.y FROM unnest(ARRAY[1, 2]) AS t(x)",
    "SELECT d FROM generate_series(1, 3) AS d",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r",
    "WITH c(x) AS (SELECT id FROM orders) SELECT x FROM c",
    "WITH c(x) AS (SELECT id FROM orders) SELECT id FROM c",
    "SELECT 1 UNION ALL SELECT 1, 2",
    "SELECT id FROM (SELECT 1 AS id UNION ALL SELECT 1, 2) q",
    "SELECT column1 FROM (VALUES (1), (2)) v",
    # casts and literals (structure only; value rules are excluded)
    "SELECT s FROM (SELECT status::text AS s FROM orders) q WHERE s = 'X'",
    "SELECT id FROM orders WHERE status::varchar(1) = 'A'",
    "SELECT id FROM orders WHERE flag::text = 'true'",
    "SELECT id FROM orders WHERE flag = 'true'",
    "SELECT COUNT(*) FILTER (WHERE status = 'ACTIVE') FROM orders",
    "SELECT id FROM orders WHERE status != ALL(ARRAY['ACTIVE'])",
    "SELECT id FROM orders WHERE status = SOME(ARRAY['ACTIVE'])",
    # position: LATERAL items, table functions and ON see only what precedes them
    "SELECT q.id FROM orders o, LATERAL (SELECT c.id) q, customers c",
    "SELECT q.id FROM customers c, orders o, LATERAL (SELECT c.id) q",
    "SELECT o.id FROM orders o JOIN customers c ON x.id = o.id JOIN customers x ON true",
    "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id",
    "SELECT t FROM orders o, unnest(o.tags) AS t",
    "SELECT t FROM unnest(o.tags) AS t, orders o",
    "SELECT 1 FROM orders o WHERE o.id IN (SELECT c.id FROM customers c WHERE c.id = o.id)",
    # join trees: JOIN binds tighter than a comma; RIGHT/FULL JOIN LATERAL sees no left side
    "SELECT o.id FROM orders o, customers c JOIN customers x ON o.id = x.id",
    "SELECT o.id FROM orders o JOIN customers c ON true JOIN customers x ON o.id = x.id",
    "SELECT * FROM orders o RIGHT JOIN LATERAL (SELECT o.id) q ON true",
    "SELECT * FROM orders o FULL JOIN LATERAL (SELECT o.id) q ON true",
    "SELECT * FROM orders o LEFT JOIN LATERAL (SELECT o.id) q ON true",
    "SELECT 1 FROM orders o, customers c JOIN LATERAL (SELECT o.id) q ON true",
    "SELECT 1 FROM orders o, customers c CROSS JOIN LATERAL (SELECT c.id) q",
    "SELECT 1 FROM orders o, customers c RIGHT JOIN LATERAL (SELECT o.id) q ON true",
    "SELECT 1 FROM orders o, customers c RIGHT JOIN LATERAL (SELECT c.id) q ON true",
    "SELECT 1 FROM orders o, customers c, LATERAL (SELECT o.id, c.id) q",
    "SELECT id FROM orders a, customers JOIN orders b USING (id)",
    "SELECT name FROM orders a, customers JOIN orders b USING (id)",
    "SELECT o.id FROM orders o JOIN customers c ON status = 'ACTIVE' JOIN orders x USING (status)",
    "SELECT o.id FROM orders o JOIN orders x USING (status) JOIN customers c ON status = 'ACTIVE'",
    # parenthesized join trees
    "SELECT o.id FROM (orders o JOIN customers c ON o.id = c.id)",
    "SELECT id FROM (orders o JOIN customers c USING (id))",
    "SELECT 1 FROM customers x, (orders o JOIN customers c ON o.id = c.id) "
    "JOIN customers t ON t.id = o.id",
    "SELECT 1 FROM customers x JOIN (orders o JOIN customers c ON x.id = c.id) ON true",
    "SELECT 1 FROM customers x JOIN (orders o JOIN customers c ON o.id = c.id) ON x.id = o.id",
    "SELECT o.id FROM (orders o JOIN customers c ON true) JOIN customers x USING (id)",
    "SELECT q.id FROM (orders o JOIN LATERAL (SELECT o.id) q ON true)",
    # table column alias lists rename columns
    "SELECT o.x FROM orders AS o(x, s)",
    "SELECT o.id FROM orders AS o(x, s)",
    "SELECT o.amount FROM orders AS o(x, s)",
    "SELECT x FROM orders AS o(x)",
    "SELECT 1 FROM customers AS c(a, b, extra)",
    "SELECT * FROM orders AS o(x) UNION ALL SELECT id, status, flag, amount, tags, customer_id "
    "FROM orders",
    # basic output shape
    "SELECT * FROM (VALUES (1), (1, 2)) q",
    "SELECT * FROM (VALUES (1, 2), (3, 4)) q",
    "SELECT *",
    "SELECT * FROM (SELECT 1) q(a, b)",
]


@pytest.fixture(scope="module")
def postgres_verdicts(database_url) -> dict[str, str | None]:
    """statement -> None when PostgreSQL plans it, else its error message."""

    async def plan_all() -> dict[str, str | None]:
        engine = create_async_engine(database_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        verdicts: dict[str, str | None] = {}
        try:
            async with engine.connect() as conn:
                await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
                for statement in filter(None, (s.strip() for s in DDL.split(";"))):
                    await conn.execute(text(statement))
                await conn.execute(text(f"SET search_path TO {SCHEMA}"))
                for sql in STATEMENTS:
                    try:
                        await conn.exec_driver_sql("EXPLAIN " + sql)
                        verdicts[sql] = None
                    except Exception as e:  # noqa: BLE001 - any planner error is the verdict
                        verdicts[sql] = str(getattr(e, "orig", e)).splitlines()[0]
                await conn.execute(text(f"DROP SCHEMA {SCHEMA} CASCADE"))
        finally:
            await engine.dispose()
        return verdicts

    return asyncio.run(plan_all())


@pytest.mark.parametrize("sql", STATEMENTS)
def test_validator_matches_postgres(sql, postgres_verdicts):
    result = validate_sql_operation(sql, "postgresql", CONTEXT, {})
    structural = [f"{e.code}: {e.message}" for e in result.errors if e.code not in VALUE_RULES]
    postgres = postgres_verdicts[sql]
    assert (postgres is None) == (not structural), (
        f"PostgreSQL: {postgres or 'accepts'}; validator: {structural or 'accepts'}"
    )
