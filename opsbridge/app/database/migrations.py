"""Additive, idempotent schema migration run at startup.

`create_all` creates missing *tables* but never alters existing ones. This adds any columns and
indexes that exist in the models but not yet in the database, so a database created by an older
OpsBridge version keeps working after an upgrade. It only ever ADDS nullable columns (or columns
with a constant default) and indexes; it never drops, renames or changes types.

For non-additive changes, introduce Alembic (see README: Current limitations).
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect, text

from app.database.base import Base

logger = logging.getLogger("opsbridge.migrations")


def _default_sql(column) -> str:  # noqa: ANN001
    default = column.default
    if default is not None and getattr(default, "is_scalar", False):
        value = default.arg
        if isinstance(value, bool):
            return f" DEFAULT {int(value)}"
        if isinstance(value, (int, float)):
            return f" DEFAULT {value}"
    return ""


def apply_additive_migrations(engine: Engine) -> list[str]:
    applied: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all handles new tables
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                default_sql = _default_sql(column)
                if not column.nullable and not default_sql:
                    raise RuntimeError(
                        f"cannot add NOT NULL column {table.name}.{column.name} without a default; use Alembic"
                    )
                col_type = column.type.compile(dialect=engine.dialect)
                null_sql = "" if column.nullable else " NOT NULL"
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {col_type}{default_sql}{null_sql}'))
                applied.append(f"{table.name}.{column.name}")
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            existing_indexes = {ix["name"] for ix in inspect(conn).get_indexes(table.name)}
            for index in table.indexes:
                if index.name not in existing_indexes:
                    index.create(conn)
                    applied.append(f"index {index.name}")
    if applied:
        logger.info("applied additive schema migration", extra={"changes": applied})
    return applied
