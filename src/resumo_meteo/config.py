"""Configuracao de runtime sem dependencia de framework."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _resolve_path(value: str, *, root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _editorial_api_key(base_url: str) -> str | None:
    configured = os.getenv("WEATHER_SUMMARY_LLM_API_KEY")
    if configured:
        return configured
    if "api.openai.com" in base_url.casefold():
        return os.getenv("OPENAI_API_KEY")
    return None


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    database_url: str
    artifact_root: Path
    output_root: Path
    timezone: str
    cycle: str
    llm_provider: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str | None
    llm_api_mode: str
    llm_timeout_seconds: int
    llm_max_attempts: int
    llm_max_output_tokens: int
    nasa_earthdata_token: str | None
    download_workers: int
    log_level: str
    anchor_hour_utc: int
    max_source_age_hours: int
    source_retention_days: int

    @classmethod
    def from_env(cls, *, project_root: Path | None = None) -> Settings:
        root = (project_root or Path.cwd()).resolve()
        load_dotenv(root / ".env", override=False)
        database_url = os.getenv(
            "RESUMO_METEO_DATABASE_URL", "sqlite:///var/resumo_meteo.sqlite3"
        )
        if database_url.startswith("sqlite:///"):
            sqlite_path = database_url.removeprefix("sqlite:///")
            if sqlite_path and not Path(sqlite_path).is_absolute():
                database_url = f"sqlite:///{root / sqlite_path}"
        llm_base_url = os.getenv(
            "WEATHER_SUMMARY_LLM_BASE_URL",
            "https://chat.maritaca.ai/api",
        )
        return cls(
            project_root=root,
            database_url=database_url,
            artifact_root=_resolve_path(
                os.getenv("RESUMO_METEO_ARTIFACT_ROOT", "var/artifacts"), root=root
            ),
            output_root=_resolve_path(
                os.getenv("RESUMO_METEO_OUTPUT_ROOT", "var/outputs"), root=root
            ),
            timezone=os.getenv("RESUMO_METEO_TIMEZONE", "America/Sao_Paulo"),
            cycle=os.getenv("RESUMO_METEO_CYCLE", "00"),
            llm_provider=os.getenv("WEATHER_SUMMARY_LLM_PROVIDER", "maritaca"),
            llm_base_url=llm_base_url,
            llm_model=os.getenv("WEATHER_SUMMARY_LLM_MODEL", "sabia-4"),
            llm_api_key=_editorial_api_key(llm_base_url),
            llm_api_mode=os.getenv(
                "WEATHER_SUMMARY_LLM_API_MODE", "chat_completions"
            ),
            llm_timeout_seconds=max(
                1, int(os.getenv("WEATHER_SUMMARY_LLM_TIMEOUT_SECONDS", "180"))
            ),
            llm_max_attempts=max(
                1, int(os.getenv("WEATHER_SUMMARY_LLM_MAX_ATTEMPTS", "2"))
            ),
            llm_max_output_tokens=max(
                256,
                int(os.getenv("WEATHER_SUMMARY_LLM_MAX_OUTPUT_TOKENS", "1200")),
            ),
            nasa_earthdata_token=os.getenv("NASA_EARTHDATA_TOKEN"),
            download_workers=max(
                1, int(os.getenv("RESUMO_METEO_DOWNLOAD_WORKERS", "6"))
            ),
            log_level=os.getenv("RESUMO_METEO_LOG_LEVEL", "INFO").upper(),
            anchor_hour_utc=int(
                os.getenv("RESUMO_METEO_ANCHOR_HOUR_UTC", "0")
            ),
            max_source_age_hours=max(
                1,
                int(os.getenv("RESUMO_METEO_MAX_SOURCE_AGE_HOURS", "42")),
            ),
            source_retention_days=max(
                1, int(os.getenv("RESUMO_METEO_SOURCE_RETENTION_DAYS", "3"))
            ),
        )

    def ensure_directories(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite:///"):
            Path(self.database_url.removeprefix("sqlite:///")).parent.mkdir(
                parents=True, exist_ok=True
            )
