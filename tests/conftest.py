"""
Shared pytest fixtures for FS-CAE tests.

Provides an in-memory SQLite database and session for each test.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base

# Import all models so they register with Base.metadata
import app.models  # noqa: F401


def _set_sqlite_pragmas(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


@pytest.fixture(scope="function")
def engine():
    """Create a fresh in-memory SQLite engine per test."""
    eng = create_engine("sqlite:///:memory:", echo=False)
    event.listen(eng, "connect", _set_sqlite_pragmas)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture(scope="function")
def session(engine):
    """Provide a transactional session that rolls back after each test."""
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sess = Session()
    yield sess
    sess.close()
