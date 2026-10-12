import os
from pathlib import Path

import psycopg
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    from tie.db import connect, migrate

    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("drop schema public cascade")
        admin.execute("create schema public")
    conn = connect(url)
    migrate(conn)
    yield conn
    conn.close()
