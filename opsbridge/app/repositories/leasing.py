"""Generic lease-based work claiming, shared by every table the background worker processes.

A row is claimable when its status is active, it is due (`next_attempt_at <= now`) and no
other worker holds an unexpired lease. Claiming is a single conditional UPDATE, so exactly one
concurrent caller wins on both SQLite and PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.time import utcnow


def claim_row(session: Session, model: Any, row_id: int, *, status_column: Any, active: Sequence[Any],
              worker_id: str, lease_seconds: int) -> bool:
    now = utcnow()
    result = session.execute(
        update(model)
        .where(
            model.id == row_id,
            status_column.in_(active),
            model.next_attempt_at <= now,
            or_(model.locked_until.is_(None), model.locked_until < now),
        )
        .values(locked_until=now + timedelta(seconds=lease_seconds), locked_by=worker_id)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def release_row(session: Session, model: Any, row_id: int, *, worker_id: str) -> None:
    session.execute(
        update(model)
        .where(model.id == row_id, model.locked_by == worker_id)
        .values(locked_until=None, locked_by=None)
        .execution_options(synchronize_session=False)
    )


def due_row_ids(session: Session, model: Any, *, status_column: Any, active: Sequence[Any],
                limit: int = 50, now: datetime | None = None) -> list[int]:
    now = now or utcnow()
    return list(
        session.scalars(
            select(model.id)
            .where(
                status_column.in_(active),
                model.next_attempt_at <= now,
                or_(model.locked_until.is_(None), model.locked_until < now),
            )
            .order_by(model.next_attempt_at)
            .limit(limit)
        ).all()
    )
