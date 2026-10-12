"""
FS-CAE Database Module

SQLAlchemy 2.0 engine and session management.
Structured for drop-in PostgreSQL adoption later.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


def _set_sqlite_pragmas(dbapi_conn, connection_record):
    """Enable WAL mode and foreign key enforcement for SQLite."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


def build_engine(database_url: str | None = None, echo: bool = False):
    """
    Create a SQLAlchemy engine.
    Uses SQLite connect_args for thread safety when SQLite is the backend.
    """
    url = database_url or get_settings().DATABASE_URL
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(url, echo=echo, connect_args=connect_args)

    # Register SQLite pragmas
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _set_sqlite_pragmas)

    return engine


def build_session_factory(engine) -> sessionmaker[Session]:
    """Create a session factory bound to the given engine."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI database session dependency."""
    engine = build_engine()
    SessionFactory = build_session_factory(engine)
    db = SessionFactory()
    try:
        yield db
    finally:
        db.close()
