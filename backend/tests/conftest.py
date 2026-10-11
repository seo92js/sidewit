import asyncio
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from sidewit.api.state import get_history, get_store
from sidewit.config import get_settings
from sidewit.eval.cases import load_suite
from sidewit.generation.history import GenerationHistory
from sidewit.main import app
from sidewit.store import Store

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def golden_suite():
    directory = Path(os.environ.get("SIDEWIT_EVALS_DIR", BACKEND_DIR.parent / "evals"))
    return load_suite(directory / "commerce")


@pytest.fixture(scope="session")
def database_url() -> str:
    """Recreates PostgreSQL for differential tests only. SIDEWIT_DATABASE_URL must name
    a database ending in `_test` (see scripts/check-backend.sh)."""
    dsn = os.environ.get("SIDEWIT_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL differential tests require SIDEWIT_DATABASE_URL")
    url = make_url(dsn)
    if not (url.database or "").endswith("_test"):
        pytest.fail(f"Refusing to run DB tests against non-test database '{url.database}'.")

    async def recreate() -> None:
        admin = create_async_engine(
            url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
        )
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
        await admin.dispose()

    asyncio.run(recreate())
    return url.render_as_string(hide_password=False)


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "data")
    yield instance
    instance.close()


@pytest.fixture
def history():
    return GenerationHistory(200)


@pytest.fixture
async def client(store, history):
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_history] = lambda: history
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("SIDEWIT_API_KEY", "secret-key")
    get_settings.cache_clear()
    yield "secret-key"
    monkeypatch.delenv("SIDEWIT_API_KEY")
    get_settings.cache_clear()
