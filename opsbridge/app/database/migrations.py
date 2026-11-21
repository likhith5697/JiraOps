"""Additive, idempotent schema migration run at startup.

`create_all` creates missing *tables* but never alters existing ones. This adds any columns and
indexes that exist in the models but not yet in the database, so a database created by an older
OpsBridge version keeps working after an upgrade. It ADDS nullable columns (or columns with a
constant default) and indexes, and can RELAX a NOT NULL constraint the models no longer require
(SQLite: safe table rebuild; PostgreSQL: ALTER COLUMN ... DROP NOT NULL). It never drops data,
renames columns or changes types.

For non-additive changes, introduce Alembic (see README: Current limitations).
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect, text
from sqlalchemy.schema import CreateIndex, CreateTable

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


def _relax_not_null(engine: Engine) -> list[str]:
    """Make columns nullable where the model now allows NULL but the database does not."""
    applied: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        db_cols = {c["name"]: c for c in inspector.get_columns(table.name)}
        relax = [c.name for c in table.columns
                 if c.name in db_cols and c.nullable and not c.primary_key and not db_cols[c.name]["nullable"]]
        if not relax:
            continue
        if engine.dialect.name == "sqlite":
            _rebuild_sqlite_table(engine, table, set(db_cols))
        else:
            with engine.begin() as conn:
                for name in relax:
                    conn.execute(text(f'ALTER TABLE {table.name} ALTER COLUMN "{name}" DROP NOT NULL'))
        applied += [f"{table.name}.{name} nullable" for name in relax]
    return applied


def _rebuild_sqlite_table(engine: Engine, table, existing_columns: set[str]) -> None:  # noqa: ANN001
    """SQLite cannot ALTER a column's constraints: copy into a table built from the current model."""
    old = f"{table.name}__old"
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")          # must be set outside a transaction
        cur.execute("PRAGMA legacy_alter_table=ON")     # don't rewrite other tables' FK references
        cur.execute("BEGIN")
        indexes = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table.name,))]
        for name in indexes:
            cur.execute(f'DROP INDEX "{name}"')
        cur.execute(f'ALTER TABLE "{table.name}" RENAME TO "{old}"')
        cur.execute(str(CreateTable(table).compile(dialect=engine.dialect)))
        for index in table.indexes:
            cur.execute(str(CreateIndex(index).compile(dialect=engine.dialect)))
        cols = ", ".join(f'"{c.name}"' for c in table.columns if c.name in existing_columns)
        cur.execute(f'INSERT INTO "{table.name}" ({cols}) SELECT {cols} FROM "{old}"')
        cur.execute(f'DROP TABLE "{old}"')
        cur.execute("COMMIT")
    except Exception:
        raw.rollback()
        raise
    finally:
        cur = raw.cursor()
        cur.execute("PRAGMA legacy_alter_table=OFF")
        cur.execute("PRAGMA foreign_keys=ON")
        raw.close()


def apply_additive_migrations(engine: Engine) -> list[str]:
    applied: list[str] = _relax_not_null(engine)
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
