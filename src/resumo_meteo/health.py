"""Diagnosticos locais seguros para operacao e containers."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import text

from .config import Settings
from .core.basins import CANONICAL_BASIN_IDS
from .core.climatology import load_climatology_bundle
from .core.geometry_loader import load_geometry_catalog
from .core.openai_client import PROMPT_BUNDLE, PROMPT_DIRECTORY
from .core.summary_scope import DEFAULT_SUMMARY_BASIN_SCOPE
from .core.synoptic_regions import load_temperature_climatology
from .pipeline.packet_builder import DEFAULT_CLIMATOLOGY_PATH
from .storage.database import create_database_engine, sqlite_integrity_check


@dataclass(frozen=True, slots=True)
class HealthCheck:
    name: str
    ok: bool
    detail: str
    required: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def run_health_checks(settings: Settings, *, include_database: bool = True) -> tuple[HealthCheck, ...]:
    checks: list[HealthCheck] = []

    checks.append(
        HealthCheck(
            "cycle",
            settings.cycle == "00",
            f"configured={settings.cycle!r}; required='00'",
        )
    )
    try:
        geometries = load_geometry_catalog()
        ok = not geometries.missing_basin_ids and set(geometries.geometries) == set(
            CANONICAL_BASIN_IDS
        )
        detail = (
            f"{len(geometries.geometries)}/{len(CANONICAL_BASIN_IDS)}; "
            f"version={geometries.version}"
        )
    except Exception as exc:  # doctor must report every independent failure.
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    checks.append(HealthCheck("basin_geometries", ok, detail))

    try:
        climate = load_climatology_bundle(
            DEFAULT_CLIMATOLOGY_PATH,
            expected_basin_ids=DEFAULT_SUMMARY_BASIN_SCOPE.basin_ids,
        )
        ok, detail = (
            True,
            f"{climate.provider}; {climate.reference_period}; {climate.version}; "
            f"{len(DEFAULT_SUMMARY_BASIN_SCOPE.basin_ids)} bacias do resumo",
        )
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    checks.append(HealthCheck("precipitation_climatology", ok, detail))

    try:
        temperature = load_temperature_climatology()
        ok, detail = bool(temperature), str(temperature.get("climatology_id") or "loaded")
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    checks.append(HealthCheck("temperature_climatology", ok, detail))

    prompt_files = {path.name for path in PROMPT_DIRECTORY.glob("*.txt")}
    expected_prompt_files = set(PROMPT_BUNDLE.values())
    missing_prompt_files = sorted(expected_prompt_files - prompt_files)
    unexpected_prompt_files = sorted(prompt_files - expected_prompt_files)
    prompts_ok = not missing_prompt_files and not unexpected_prompt_files
    prompt_detail = f"{len(prompt_files)}/{len(expected_prompt_files)} arquivos vigentes"
    if missing_prompt_files:
        prompt_detail += f"; ausentes={','.join(missing_prompt_files)}"
    if unexpected_prompt_files:
        prompt_detail += f"; inesperados={','.join(unexpected_prompt_files)}"
    checks.append(HealthCheck("editorial_prompts", prompts_ok, prompt_detail))

    try:
        import eccodes  # noqa: F401, PLC0415

        ok, detail = True, "Python binding available"
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    checks.append(HealthCheck("eccodes", ok, detail))

    checks.extend(
        (
            HealthCheck(
                "llm_api_key",
                bool(settings.llm_api_key),
                "configured" if settings.llm_api_key else "missing",
            ),
            HealthCheck(
                "nasa_earthdata_token",
                bool(settings.nasa_earthdata_token),
                "configured" if settings.nasa_earthdata_token else "missing",
            ),
        )
    )

    if include_database:
        try:
            engine = create_database_engine(settings.database_url)
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            sqlite_integrity_check(engine)
            detail = engine.url.get_backend_name()
            if detail == "sqlite":
                with engine.connect() as connection:
                    journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
                detail = f"sqlite; integrity=ok; journal_mode={journal_mode}"
            engine.dispose()
            ok = True
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        checks.append(HealthCheck("database", ok, detail))

    return tuple(checks)


__all__ = ["HealthCheck", "run_health_checks"]
