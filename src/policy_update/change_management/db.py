"""Engine/session bootstrap for the Person-2 tables.

No shared DB config existed elsewhere in the repo at the time of writing (checked
`src/infrastructure/`, `payroll/config.py` — empty). This is a self-contained,
swap-in-later setup: point `DATABASE_URL` at Postgres in every real environment;
the SQLite default only exists so `create_file`-level unit tests don't need a server.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .db_models import Base

_DEFAULT_SQLITE_URL = "sqlite:///./policy_update_change_management.db"


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", _DEFAULT_SQLITE_URL)


def make_engine(database_url: str | None = None):
    url = database_url or get_database_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


def make_session_factory(engine=None) -> sessionmaker[Session]:
    engine = engine or make_engine()
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db(engine=None) -> None:
    """Create tables if they don't exist yet. In a real Postgres environment this
    should be replaced by an Alembic migration; kept here for local/dev/test use."""
    engine = engine or make_engine()
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(session_factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    factory = session_factory or make_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
