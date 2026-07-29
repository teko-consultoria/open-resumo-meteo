from __future__ import annotations

from pathlib import Path

import pytest

from resumo_meteo.storage import WeatherStorageRepository


@pytest.fixture
def repository(tmp_path: Path) -> WeatherStorageRepository:
    return WeatherStorageRepository(
        database_url=f"sqlite:///{tmp_path / 'metadata.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        bootstrap=True,
    )
