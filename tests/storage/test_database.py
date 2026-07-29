from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from resumo_meteo.storage.database import (
    DatabaseLockError,
    create_database_engine,
    create_session_factory,
    normalize_database_url,
    run_migrations,
    sqlite_integrity_check,
    sqlite_process_lock,
)
from resumo_meteo.storage.models import SchemaMigration


def test_initial_migration_is_recorded_once(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'db.sqlite3'}")

    assert run_migrations(engine) == ("0001_initial", "0002_active_source_claim")
    assert run_migrations(engine) == ()

    with create_session_factory(engine)() as session:
        assert session.scalar(select(func.count()).select_from(SchemaMigration)) == 2


def test_postgres_url_is_normalized_to_psycopg() -> None:
    assert normalize_database_url("postgresql://u:p@db/name") == "postgresql+psycopg://u:p@db/name"


def test_unsupported_database_is_rejected() -> None:
    with pytest.raises(ValueError, match="SQLite and PostgreSQL"):
        normalize_database_url("mysql://localhost/weather")


def test_sqlite_uses_durable_journal_settings_and_passes_integrity_check(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'db.sqlite3'}")
    run_migrations(engine)

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 2
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 60000
        assert str(connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()).lower() == "wal"
    sqlite_integrity_check(engine)


def test_sqlite_process_lock_rejects_a_second_pipeline_invocation(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'db.sqlite3'}"

    with sqlite_process_lock(database_url):
        with pytest.raises(DatabaseLockError, match="another resumo-meteo process"):
            with sqlite_process_lock(database_url, timeout_seconds=0):
                pass
