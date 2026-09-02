"""Database engine and session management.

SQLite by default so the project runs with zero external services; point
DATABASE_URL at Postgres for production (docker-compose does this for you).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from datetime import timezone

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 30}
        kwargs = {"connect_args": connect_args}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, future=True, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        return engine

    # Postgres / others: recycle connections so a restarted DB does not wedge
    # the pool, and pre-ping to drop dead connections.
    return create_engine(url, future=True, pool_pre_ping=True, pool_recycle=1800)


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background work: commit on success, roll back on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create tables. Idempotent - safe to call on every boot."""
    from app import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)


class UTCDateTime(TypeDecorator):
    """A DateTime that is always timezone-aware UTC in Python.

    SQLite has no native timezone support and hands back naive datetimes, which
    makes `db_value > datetime.now(timezone.utc)` raise TypeError. Normalising
    on the way in and out means application code can assume aware UTC
    everywhere, on SQLite and Postgres alike.
    """

    impl = DateTime
    cache_ok = True

    def __init__(self):
        super().__init__(timezone=True)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
