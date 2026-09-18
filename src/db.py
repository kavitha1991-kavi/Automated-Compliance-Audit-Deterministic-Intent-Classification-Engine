"""
Database engine helper. Defaults to a local SQLite file so the whole
project runs with zero external services. Set DATABASE_URL to point at
PostgreSQL 15+ instead (e.g. postgresql+psycopg2://user:pass@host/db) -
nothing else in the codebase needs to change, because every query in
sql/ and every insert in etl_pipeline.py is plain parameterised SQL run
through SQLAlchemy's engine-agnostic `text()` construct.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_PATH = PROJECT_ROOT / "data" / "compliance_audit.db"
SCHEMA_DDL_PATH = PROJECT_ROOT / "sql" / "00_schema_ddl.sql"


def _sqlite_regexp(pattern: str, value) -> bool:
    """Backs the REGEXP operator used by sql/11_pii_assurance_check.sql.
    SQLite has no built-in regex; PostgreSQL's native `~` operator is the
    direct equivalent there (see the note at the top of that file)."""
    if value is None:
        return False
    return re.search(pattern, str(value)) is not None


def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or os.environ.get("DATABASE_URL") or f"sqlite:///{DEFAULT_SQLITE_PATH}"
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _register_regexp(dbapi_conn, _):
            dbapi_conn.create_function("REGEXP", 2, _sqlite_regexp)

        with engine.connect() as conn:
            conn.execute(text("PRAGMA foreign_keys = ON"))
    return engine


def ensure_schema(engine: Engine) -> None:
    """Create every table/view if they don't already exist (idempotent:
    safe to call on every run)."""
    with engine.connect() as conn:
        existing = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
            if engine.url.get_backend_name() == "sqlite"
            else text(
                "SELECT table_name AS name FROM information_schema.tables WHERE table_schema='public'"
            )
        ).fetchall()
        table_names = {row[0] for row in existing}

    if "interactions" in table_names:
        return  # schema already built

    ddl_script = SCHEMA_DDL_PATH.read_text(encoding="utf-8")
    with engine.begin() as conn:
        if engine.url.get_backend_name() == "sqlite":
            raw_conn = conn.connection.driver_connection
            raw_conn.executescript(ddl_script)
        else:
            for statement in _split_sql_statements(ddl_script):
                conn.execute(text(statement))


def _split_sql_statements(script: str) -> list[str]:
    """Very small helper for non-SQLite backends: splits on ';' outside
    of string literals. The DDL script has no semicolons inside string
    literals, so a straightforward split is safe here."""
    statements = [s.strip() for s in script.split(";")]
    return [s for s in statements if s and not s.startswith("--")]
