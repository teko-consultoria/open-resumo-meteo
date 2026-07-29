"""Database engine, sessions and lightweight schema migrations."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateIndex

from .models import ACTIVE_SOURCE_IDENTITY_INDEX, Base, SchemaMigration

INITIAL_SCHEMA_VERSION = "0001_initial"
ACTIVE_SOURCE_CLAIM_VERSION = "0002_active_source_claim"
SQLITE_LOCK_TIMEOUT_SECONDS = 120


class DatabaseIntegrityError(RuntimeError):
    """Raised before writes when SQLite reports structural corruption."""


class DatabaseLockError(RuntimeError):
    """Raised when another pipeline process owns the SQLite catalog."""


def normalize_database_url(database_url: str) -> str:
    """Validate supported URLs and select the psycopg PostgreSQL driver."""

    value = str(database_url or "").strip()
    if not value:
        raise ValueError("database_url is required")
    if value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value[len("postgresql://") :]
    url = make_url(value)
    if url.get_backend_name() not in {"sqlite", "postgresql"}:
        raise ValueError("only SQLite and PostgreSQL database URLs are supported")
    return value


def create_database_engine(database_url: str, *, echo: bool = False) -> Engine:
    normalized = normalize_database_url(database_url)
    url = make_url(normalized)
    if url.get_backend_name() == "sqlite" and url.database not in {None, "", ":memory:"}:
        Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    engine_kwargs: dict[str, object] = {"echo": echo, "future": True, "pool_pre_ping": True}
    if url.get_backend_name() == "sqlite":
        # SQLite has one writer.  A generous driver timeout lets the repository's
        # small write transactions queue instead of failing under ingestion threads.
        engine_kwargs["connect_args"] = {"timeout": 60}
    engine = create_engine(normalized, **engine_kwargs)
    if url.get_backend_name() == "sqlite":
        journal_mode = os.getenv("RESUMO_METEO_SQLITE_JOURNAL_MODE", "WAL").upper()
        if journal_mode not in {"WAL", "DELETE"}:
            raise ValueError(
                "RESUMO_METEO_SQLITE_JOURNAL_MODE must be either 'WAL' or 'DELETE'"
            )

        @event.listens_for(engine, "connect")
        def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=60000")
            # FULL favors durability over a marginal write-speed gain.  DELETE is
            # deliberately used by the container image: WAL's shared-memory sidecar
            # is not a safe default for a database carried through a host bind mount.
            cursor.execute("PRAGMA synchronous=FULL")
            if url.database not in {None, "", ":memory:"}:
                cursor.execute(f"PRAGMA journal_mode={journal_mode}")
            cursor.close()

    return engine


def sqlite_integrity_check(engine: Engine) -> None:
    """Run SQLite's full structural check and fail closed on a damaged catalog."""

    if engine.url.get_backend_name() != "sqlite":
        return
    with engine.connect() as connection:
        rows = [str(row[0]) for row in connection.execute(text("PRAGMA integrity_check"))]
    if rows != ["ok"]:
        detail = "; ".join(rows[:3]) or "no result returned"
        raise DatabaseIntegrityError(
            "SQLite integrity_check failed. Stop concurrent access, preserve a copy of "
            f"the database, then recover it before another run. Detail: {detail}"
        )


@contextmanager
def sqlite_process_lock(database_url: str, *, timeout_seconds: int = SQLITE_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Serialize CLI access to a file-backed SQLite catalog.

    The lock covers the whole command, not merely individual transactions. It
    prevents two scheduler/manual invocations of this application from sharing a
    catalog while source workers and publication are in flight. PostgreSQL already
    provides multi-process coordination and is intentionally left unlocked here.
    """

    url = make_url(normalize_database_url(database_url))
    if url.get_backend_name() != "sqlite" or url.database in {None, "", ":memory:"}:
        yield
        return
    try:
        import fcntl
    except ImportError:  # pragma: no cover - supported deployments are POSIX.
        yield
        return

    database_path = Path(url.database).expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = database_path.with_name(f"{database_path.name}.lock")
    deadline = time.monotonic() + max(int(timeout_seconds), 0)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise DatabaseLockError(
                        "another resumo-meteo process is using this SQLite catalog; "
                        "wait for it to finish instead of running a concurrent command"
                    ) from exc
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def run_migrations(engine: Engine) -> tuple[str, ...]:
    """Apply the small ordered portable schema migrations idempotently."""

    sqlite_integrity_check(engine)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    applied: list[str] = []
    with session_factory.begin() as session:
        existing = session.scalar(
            select(SchemaMigration).where(SchemaMigration.version == INITIAL_SCHEMA_VERSION)
        )
        if existing is None:
            session.add(
                SchemaMigration(
                    version=INITIAL_SCHEMA_VERSION,
                    description="Standalone source, artifact, summary and audit schema",
                )
            )
            applied.append(INITIAL_SCHEMA_VERSION)
    with session_factory.begin() as session:
        existing = session.scalar(
            select(SchemaMigration).where(
                SchemaMigration.version == ACTIVE_SOURCE_CLAIM_VERSION
            )
        )
        if existing is None:
            # New databases already receive the index through create_all;
            # checkfirst also upgrades databases created by 0001.
            with engine.begin() as connection:
                connection.execute(
                    CreateIndex(ACTIVE_SOURCE_IDENTITY_INDEX, if_not_exists=True)
                )
            session.add(
                SchemaMigration(
                    version=ACTIVE_SOURCE_CLAIM_VERSION,
                    description="Prevent concurrent ingestion of one logical source run",
                )
            )
            applied.append(ACTIVE_SOURCE_CLAIM_VERSION)
    sqlite_integrity_check(engine)
    return tuple(applied)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
