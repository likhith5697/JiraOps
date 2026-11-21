"""Engine / session factory. SQLite is tuned for concurrent readers + one writer (WAL)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base


def build_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if database_url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool  # share one in-memory DB across threads (tests)
        engine = create_engine(database_url, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_engine(database_url, pool_pre_ping=True)


class Database:
    def __init__(self, database_url: str) -> None:
        self.engine = build_engine(database_url)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        # Import models so they register with Base.metadata.
        from app.database.migrations import apply_additive_migrations
        from app.repositories.knowledge_repository import ensure_search_index
        from app.models import (  # noqa: F401
            graph_subscription,
            knowledge,
            llm_usage,
            support_request,
            teams_command,
            teams_notification,
        )

        Base.metadata.create_all(self.engine)
        apply_additive_migrations(self.engine)
        ensure_search_index(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commit on success, roll back on error."""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
