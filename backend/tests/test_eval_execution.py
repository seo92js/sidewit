import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from sidewit.domain.operation import SqlOperation
from sidewit.eval.cases import GoldenCase, load_suite
from sidewit.eval.execution import PostgresExecutionEvaluator
from sidewit.eval.runner import EvalSetupError


@pytest.fixture(scope="module")
def execution_url(database_url):
    admin_url = make_url(database_url)
    eval_db = "sidewit_test_eval"
    role = "sidewit_eval_test"

    async def setup():
        admin = create_async_engine(
            admin_url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
        )
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{eval_db}" WITH (FORCE)'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
            await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'fixture-password'"))
            await conn.execute(text(f'CREATE DATABASE "{eval_db}"'))
        await admin.dispose()

        database = create_async_engine(admin_url.set(database=eval_db), poolclass=NullPool)
        async with database.begin() as conn:
            await conn.execute(text("CREATE TABLE items (value INTEGER, happened_at TIMESTAMPTZ)"))
            await conn.execute(
                text(
                    "INSERT INTO items VALUES (1, '2026-10-01T00:00:00+09:00'), "
                    "(1, '2026-10-02T00:00:00+09:00'), "
                    "(2, '2026-10-03T00:00:00+09:00')"
                )
            )
            await conn.execute(text(f"GRANT CONNECT ON DATABASE {eval_db} TO {role}"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await conn.execute(text(f"GRANT SELECT ON items TO {role}"))
        await database.dispose()

    async def teardown():
        admin = create_async_engine(
            admin_url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
        )
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{eval_db}" WITH (FORCE)'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await admin.dispose()

    asyncio.run(setup())
    eval_url = admin_url.set(database=eval_db, username=role, password="fixture-password")
    yield eval_url.render_as_string(hide_password=False)
    asyncio.run(teardown())


def _case(case_id, statement, *, ordered=False):
    return GoldenCase.model_validate(
        {
            "id": case_id,
            "question": "fixture",
            "category": "execution",
            "expect": {"status": "ok"},
            "reference": {"statement": statement, "ordered": ordered},
        }
    )


def _operation(statement, parameters=None, parameter_types=None):
    return SqlOperation(
        dialect="postgresql",
        statement=statement,
        parameters=parameters or {},
        parameter_types=parameter_types or {},
    )


async def test_execution_comparison_preserves_duplicates_and_optional_order(execution_url):
    async with PostgresExecutionEvaluator(execution_url) as evaluator:
        await evaluator.prepare(
            [
                _case("unordered", "SELECT value FROM items"),
                _case("ordered", "SELECT value FROM items ORDER BY value", ordered=True),
            ]
        )
        reversed_rows = _operation("SELECT value FROM items ORDER BY value DESC")
        assert (await evaluator.compare("unordered", reversed_rows)).matched
        assert not (
            await evaluator.compare("unordered", _operation("SELECT DISTINCT value FROM items"))
        ).matched
        assert not (await evaluator.compare("ordered", reversed_rows)).matched


async def test_execution_uses_typed_parameters_and_blocks_writes(execution_url):
    case = GoldenCase.model_validate(
        {
            "id": "since",
            "question": "fixture",
            "category": "execution",
            "expect": {"status": "ok"},
            "reference": {
                "statement": "SELECT COUNT(*) FROM items WHERE happened_at >= :since",
                "parameters": {"since": "2026-10-02T00:00:00+09:00"},
                "parameter_types": {"since": "date-time"},
            },
        }
    )
    async with PostgresExecutionEvaluator(execution_url) as evaluator:
        await evaluator.prepare([case])
        same = await evaluator.compare(
            "since",
            _operation(
                "SELECT COUNT(*) FROM items WHERE happened_at >= :when",
                {"when": "2026-10-02T00:00:00+09:00"},
                {"when": "date-time"},
            ),
        )
        assert same.matched and same.expected_row_count == same.actual_row_count == 1
        write = await evaluator.compare("since", _operation("DELETE FROM items RETURNING value"))
        assert not write.matched and write.error is not None
        assert write.error.startswith("Generated SQL failed (")
        assert "DELETE" not in write.error


async def test_execution_limits_rows(execution_url):
    async with PostgresExecutionEvaluator(execution_url) as evaluator:
        await evaluator.prepare([_case("small", "SELECT value FROM items")])
        result = await evaluator.compare("small", _operation("SELECT generate_series(1, 1001)"))
        assert result.error == "Generated SQL failed (_TooManyRows)."


async def test_execution_rejects_superuser_and_non_eval_database(database_url, execution_url):
    # execution_url creates sidewit_test_eval; connect to it as the superuser here.
    with pytest.raises(EvalSetupError, match="ending in _eval"):
        PostgresExecutionEvaluator(database_url)
    superuser_url = (
        make_url(database_url)
        .set(database="sidewit_test_eval")
        .render_as_string(hide_password=False)
    )
    with pytest.raises(EvalSetupError, match="must not be a superuser"):
        async with PostgresExecutionEvaluator(superuser_url):
            pass


def test_execution_suite_has_new_cases_and_references():
    from pathlib import Path

    suite = load_suite(Path(__file__).resolve().parents[2] / "evals/execution-workshops")
    assert len(suite.cases) == 24
    assert all(case.expect.status == "ok" and case.reference for case in suite.cases)
