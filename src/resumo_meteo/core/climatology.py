"""Source-neutral calendar-window climatology from daily basin totals.

This module is intentionally pure with respect to external systems: adapters
download and validate their own products, then pass normalized daily basin
records here.  Calendar 5/4-day samples are always sums of the underlying
daily precipitation before distribution statistics are calculated; daily
percentiles are never added together.  The JSON writer is the only function
with filesystem side effects.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from resumo_meteo.core.basins import DEFAULT_BASIN_REGISTRY
from resumo_meteo.core.ensemble import quantile

CLIMATOLOGY_SCHEMA_VERSION = 2
CANONICAL_BASIN_DEFINITION_VERSION = (
    f"{DEFAULT_BASIN_REGISTRY.catalog_id}@{DEFAULT_BASIN_REGISTRY.catalog_version}"
)
DEFAULT_DURATIONS_DAYS: tuple[int, ...] = (4, 5)
_REFERENCE_PERIOD_PATTERN = re.compile(r"^(?P<start>\d{4})-(?P<end>\d{4})$")
_WINDOW_KEY_PATTERN = re.compile(r"^(?P<month>\d{2})-(?P<day>\d{2}):(?P<duration>\d+)$")


class ClimatologyError(ValueError):
    """Base error for invalid or insufficient climatology input."""


class IncompleteClimatologyError(ClimatologyError):
    """Raised when one or more calendar windows lack complete source years."""

    def __init__(self, failures: Sequence[Mapping[str, object]]):
        self.failures = tuple(dict(item) for item in failures)
        examples = ", ".join(
            f"{item['basin_id']}/{item['window_key']}="
            f"{item['sample_count']}/{item['minimum_complete_years']}"
            for item in self.failures[:8]
        )
        suffix = "" if len(self.failures) <= 8 else f" (+{len(self.failures) - 8})"
        super().__init__(
            "Climatologia sem o minimo de anos completos em "
            f"{len(self.failures)} janela(s): {examples}{suffix}"
        )


@dataclass(frozen=True, slots=True)
class DailyBasinPrecipitationRecord:
    basin_id: str
    observed_date: date
    precipitation_mm: float
    coverage_pct: float | None


@dataclass(frozen=True, slots=True)
class ClimatologyBundle:
    climatology_id: str
    version: str
    reference_period: str
    provider: str
    source_product: str
    source_path: str | None
    source_sha256: str | None
    basin_definition_version: str
    source_mask_version: str | None
    source_mask_limitation: str | None
    sha256: str
    path: str
    data: dict

    def basin_window(self, basin_id: str, *, start_date: date, duration_days: int) -> dict | None:
        basin = (self.data.get("basins") or {}).get(str(basin_id))
        if not isinstance(basin, dict):
            return None
        windows = basin.get("windows") if isinstance(basin.get("windows"), dict) else basin
        keys = (
            f"{start_date:%m-%d}:{int(duration_days)}",
            f"{start_date.timetuple().tm_yday:03d}:{int(duration_days)}",
        )
        for key in keys:
            value = windows.get(key)
            if isinstance(value, dict):
                return dict(value)
        return None


def _finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool):
        raise ClimatologyError(f"{field} deve ser numerico")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ClimatologyError(f"{field} deve ser numerico") from exc
    if not math.isfinite(numeric):
        raise ClimatologyError(f"{field} deve ser finito")
    return numeric


def _parse_record_date(value: object, *, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ClimatologyError(f"{field} deve usar YYYY-MM-DD")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ClimatologyError(f"{field} deve usar YYYY-MM-DD") from exc


def _reference_bounds(reference_period: str) -> tuple[date, date]:
    match = _REFERENCE_PERIOD_PATTERN.fullmatch(str(reference_period or "").strip())
    if not match:
        raise ClimatologyError("reference_period deve usar YYYY-YYYY")
    start_year = int(match.group("start"))
    end_year = int(match.group("end"))
    if start_year > end_year:
        raise ClimatologyError("reference_period deve estar em ordem crescente")
    return date(start_year, 1, 1), date(end_year, 12, 31)


def _calendar_days() -> tuple[tuple[int, int], ...]:
    # Year 2000 is only a neutral template that enumerates all 366 valid
    # month/day keys, including 29 February.  It contributes no data.
    current = date(2000, 1, 1)
    end = date(2000, 12, 31)
    result: list[tuple[int, int]] = []
    while current <= end:
        result.append((current.month, current.day))
        current += timedelta(days=1)
    return tuple(result)


def _normalize_durations(durations_days: Iterable[int]) -> tuple[int, ...]:
    durations: list[int] = []
    for value in durations_days:
        if isinstance(value, bool):
            raise ClimatologyError("durations_days deve conter inteiros positivos")
        try:
            duration = int(value)
        except (TypeError, ValueError) as exc:
            raise ClimatologyError("durations_days deve conter inteiros positivos") from exc
        if duration <= 0 or duration != value:
            raise ClimatologyError("durations_days deve conter inteiros positivos")
        durations.append(duration)
    if not durations or len(set(durations)) != len(durations):
        raise ClimatologyError("durations_days deve ser uma lista nao vazia e sem duplicatas")
    return tuple(sorted(durations))


def normalize_daily_basin_records(
    records: Iterable[Mapping[str, object]],
    *,
    require_coverage: bool = False,
) -> tuple[DailyBasinPrecipitationRecord, ...]:
    """Validate source-neutral daily basin aggregates without inferring rain.

    Required fields are ``basin_id``, ``date`` and precipitation as ``mm``.
    ``coverage_pct`` is optional for products already published as a basin
    total. When ``require_coverage`` is true, it is mandatory and is never
    defaulted.
    """

    normalized: list[DailyBasinPrecipitationRecord] = []
    seen: set[tuple[str, date]] = set()
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, Mapping):
            raise ClimatologyError(f"registro {index} deve ser um objeto")
        basin_id = str(raw.get("basin_id") or "").strip()
        if not basin_id:
            raise ClimatologyError(f"registro {index} sem basin_id")
        observed_date = _parse_record_date(raw.get("date"), field=f"registro {index}.date")

        if raw.get("mm") is None:
            raise ClimatologyError(f"registro {index} sem mm")
        precipitation_mm = _finite_number(raw.get("mm"), field=f"registro {index}.mm")
        if precipitation_mm < 0:
            raise ClimatologyError(f"registro {index}.mm nao pode ser negativo")

        if require_coverage and raw.get("coverage_pct") is None:
            raise ClimatologyError(f"registro {index} sem coverage_pct")
        coverage_pct = None
        if raw.get("coverage_pct") is not None:
            coverage_pct = _finite_number(
                raw.get("coverage_pct"), field=f"registro {index}.coverage_pct"
            )
            if not 0 <= coverage_pct <= 100:
                raise ClimatologyError(
                    f"registro {index}.coverage_pct deve estar entre 0 e 100"
                )

        identity = (basin_id, observed_date)
        if identity in seen:
            raise ClimatologyError(
                f"registro diario duplicado para {basin_id}/{observed_date.isoformat()}"
            )
        seen.add(identity)
        normalized.append(
            DailyBasinPrecipitationRecord(
                basin_id=basin_id,
                observed_date=observed_date,
                precipitation_mm=precipitation_mm,
                coverage_pct=coverage_pct,
            )
        )
    if not normalized:
        raise ClimatologyError("ao menos um agregado diario de bacia e obrigatorio")
    return tuple(sorted(normalized, key=lambda item: (item.basin_id, item.observed_date)))


def aggregate_daily_basin_records(
    records: Iterable[Mapping[str, object]],
    *,
    basin_members: Mapping[str, Mapping[str, float]],
) -> tuple[dict[str, object], ...]:
    """Aggregate daily basin means with explicit, normalized area weights.

    Historical MERGE/INPE data are already basin means.  When a reviewed
    operational contour combines historical basins, its historical counterpart is
    therefore the daily area-weighted mean of those source series.  This does
    not claim that the historical masks equal the new geometry; callers must keep
    that limitation in the bundle provenance.
    """

    normalized = normalize_daily_basin_records(records)
    if not basin_members:
        raise ClimatologyError("ao menos uma bacia agregada e obrigatoria")
    source_by_date: dict[date, dict[str, DailyBasinPrecipitationRecord]] = {}
    for record in normalized:
        source_by_date.setdefault(record.observed_date, {})[record.basin_id] = record

    result: list[dict[str, object]] = []
    for basin_id, raw_members in basin_members.items():
        if not isinstance(basin_id, str) or not basin_id.strip() or not raw_members:
            raise ClimatologyError("definição de agregação de bacia inválida")
        weights: dict[str, float] = {}
        for source_basin_id, raw_weight in raw_members.items():
            source_id = str(source_basin_id).strip()
            weight = _finite_number(raw_weight, field=f"peso de {basin_id}/{source_id}")
            if not source_id or weight <= 0:
                raise ClimatologyError(f"peso inválido para {basin_id}/{source_id}")
            if source_id in weights:
                raise ClimatologyError(f"membro duplicado em {basin_id}: {source_id}")
            weights[source_id] = weight
        total_weight = sum(weights.values())
        for observed_date, source_records in source_by_date.items():
            missing = sorted(set(weights) - set(source_records))
            if missing:
                raise ClimatologyError(
                    f"séries históricas ausentes para {basin_id}/{observed_date}: {missing}"
                )
            precipitation_mm = sum(
                source_records[source_id].precipitation_mm * weight
                for source_id, weight in weights.items()
            ) / total_weight
            coverages = [
                source_records[source_id].coverage_pct
                for source_id in weights
                if source_records[source_id].coverage_pct is not None
            ]
            result.append(
                {
                    "basin_id": basin_id,
                    "date": observed_date.isoformat(),
                    "mm": precipitation_mm,
                    "coverage_pct": min(coverages) if coverages else None,
                }
            )
    return tuple(result)


def _window_sample(
    daily: Mapping[date, DailyBasinPrecipitationRecord],
    *,
    start_date: date,
    duration_days: int,
    reference_start: date,
    reference_end: date,
    minimum_daily_coverage_pct: float | None,
) -> tuple[dict | None, dict | None]:
    end_date = start_date + timedelta(days=duration_days - 1)
    if start_date < reference_start or end_date > reference_end:
        return None, {
            "year": start_date.year,
            "reason": "outside_reference_period",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }

    dates = tuple(start_date + timedelta(days=offset) for offset in range(duration_days))
    missing_dates = [day.isoformat() for day in dates if day not in daily]
    low_coverage_dates = []
    if minimum_daily_coverage_pct is not None:
        low_coverage_dates = [
            day.isoformat()
            for day in dates
            if day in daily
            and (
                daily[day].coverage_pct is None
                or daily[day].coverage_pct < minimum_daily_coverage_pct
            )
        ]
    if missing_dates or low_coverage_dates:
        exclusion: dict[str, object] = {
            "year": start_date.year,
            "reason": (
                "missing_and_low_coverage_days"
                if missing_dates and low_coverage_dates
                else "missing_days"
                if missing_dates
                else "coverage_below_threshold"
            ),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        if missing_dates:
            exclusion["missing_dates"] = missing_dates
        if low_coverage_dates:
            exclusion["low_coverage_dates"] = low_coverage_dates
        return None, exclusion

    rows = tuple(daily[day] for day in dates)
    coverages = tuple(item.coverage_pct for item in rows if item.coverage_pct is not None)
    sample = {
        "year": start_date.year,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "accumulated_mm": sum(item.precipitation_mm for item in rows),
    }
    if coverages:
        sample["minimum_daily_coverage_pct"] = min(coverages)
        sample["mean_daily_coverage_pct"] = statistics.fmean(coverages)
    return sample, None


def build_calendar_window_climatology(
    records: Iterable[Mapping[str, object]],
    *,
    climatology_id: str,
    version: str,
    reference_period: str,
    basin_definition_version: str,
    minimum_complete_years: int,
    provider: str,
    source_product: str,
    source_path: str | None = None,
    source_sha256: str | None = None,
    source_mask_version: str | None = None,
    source_mask_limitation: str | None = None,
    minimum_daily_coverage_pct: float | None = None,
    durations_days: Iterable[int] = DEFAULT_DURATIONS_DAYS,
    expected_basin_ids: Iterable[str] = (),
) -> dict:
    """Build a deterministic 366-day source-neutral climatology bundle.

    Every ``MM-DD:duration`` entry is based only on years whose daily values
    are all present and whose daily coverage meets the configured threshold.
    The whole build fails if any basin/window has fewer than
    ``minimum_complete_years`` samples.  This prevents a partial climatology
    from silently entering the daily summary.
    """

    metadata = {
        "climatology_id": str(climatology_id or "").strip(),
        "version": str(version or "").strip(),
        "basin_definition_version": str(basin_definition_version or "").strip(),
        "provider": str(provider or "").strip(),
        "source_product": str(source_product or "").strip(),
    }
    missing_metadata = [key for key, value in metadata.items() if not value]
    if missing_metadata:
        raise ClimatologyError(
            f"metadados obrigatorios ausentes: {', '.join(sorted(missing_metadata))}"
        )
    normalized_source_path = str(source_path or "").strip() or None
    normalized_source_sha256 = str(source_sha256 or "").strip().lower() or None
    normalized_mask_version = str(source_mask_version or "").strip() or None
    normalized_mask_limitation = str(source_mask_limitation or "").strip() or None
    if normalized_source_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", normalized_source_sha256
    ):
        raise ClimatologyError("source_sha256 deve usar 64 caracteres hexadecimais")
    if isinstance(minimum_complete_years, bool) or not isinstance(minimum_complete_years, int):
        raise ClimatologyError("minimum_complete_years deve ser inteiro positivo")
    if minimum_complete_years <= 0:
        raise ClimatologyError("minimum_complete_years deve ser inteiro positivo")
    coverage_threshold = None
    if minimum_daily_coverage_pct is not None:
        coverage_threshold = _finite_number(
            minimum_daily_coverage_pct, field="minimum_daily_coverage_pct"
        )
        if not 0 <= coverage_threshold <= 100:
            raise ClimatologyError("minimum_daily_coverage_pct deve estar entre 0 e 100")
    durations = _normalize_durations(durations_days)
    reference_start, reference_end = _reference_bounds(reference_period)

    normalized = normalize_daily_basin_records(
        records,
        require_coverage=coverage_threshold is not None,
    )
    configured_basin_ids = tuple(str(item).strip() for item in expected_basin_ids)
    if any(not item for item in configured_basin_ids) or len(set(configured_basin_ids)) != len(
        configured_basin_ids
    ):
        raise ClimatologyError("expected_basin_ids deve conter IDs unicos e nao vazios")
    actual_basin_ids = {item.basin_id for item in normalized}
    if configured_basin_ids:
        expected = set(configured_basin_ids)
        if actual_basin_ids != expected:
            missing = sorted(expected - actual_basin_ids)
            extra = sorted(actual_basin_ids - expected)
            raise ClimatologyError(
                f"agregados com bacias divergentes; ausentes={missing}, extras={extra}"
            )
        basin_ids = configured_basin_ids
    else:
        basin_ids = tuple(sorted(actual_basin_ids))

    records_by_basin: dict[str, dict[date, DailyBasinPrecipitationRecord]] = {
        basin_id: {} for basin_id in basin_ids
    }
    used_record_count = 0
    outside_record_count = 0
    for item in normalized:
        if reference_start <= item.observed_date <= reference_end:
            records_by_basin[item.basin_id][item.observed_date] = item
            used_record_count += 1
        else:
            outside_record_count += 1

    calendar_days = _calendar_days()
    years = range(reference_start.year, reference_end.year + 1)
    basins: dict[str, dict[str, object]] = {}
    failures: list[dict[str, object]] = []

    for basin_id in basin_ids:
        daily = records_by_basin[basin_id]
        windows: dict[str, dict[str, object]] = {}
        excluded_sample_count = 0
        for month, day in calendar_days:
            for duration in durations:
                window_key = f"{month:02d}-{day:02d}:{duration}"
                samples: list[dict[str, object]] = []
                exclusions: list[dict[str, object]] = []
                for year in years:
                    try:
                        start_date = date(year, month, day)
                    except ValueError:
                        exclusions.append(
                            {
                                "year": year,
                                "reason": "invalid_calendar_date",
                                "calendar_day": f"{month:02d}-{day:02d}",
                            }
                        )
                        continue
                    sample, exclusion = _window_sample(
                        daily,
                        start_date=start_date,
                        duration_days=duration,
                        reference_start=reference_start,
                        reference_end=reference_end,
                        minimum_daily_coverage_pct=coverage_threshold,
                    )
                    if sample is not None:
                        samples.append(sample)
                    elif exclusion is not None:
                        exclusions.append(exclusion)

                excluded_sample_count += len(exclusions)
                if len(samples) < minimum_complete_years:
                    failures.append(
                        {
                            "basin_id": basin_id,
                            "window_key": window_key,
                            "sample_count": len(samples),
                            "minimum_complete_years": minimum_complete_years,
                            "sample_years": [item["year"] for item in samples],
                            "excluded_years": exclusions,
                        }
                    )
                    continue

                values = tuple(float(item["accumulated_mm"]) for item in samples)
                window_entry = {
                    "duration_days": duration,
                    "mean_mm": round(statistics.fmean(values), 6),
                    "median_mm": round(quantile(values, 0.50), 6),
                    "p10_mm": round(quantile(values, 0.10), 6),
                    "p25_mm": round(quantile(values, 0.25), 6),
                    "p75_mm": round(quantile(values, 0.75), 6),
                    "p90_mm": round(quantile(values, 0.90), 6),
                    "sample_count": len(samples),
                    "minimum_complete_years": minimum_complete_years,
                }
                if exclusions:
                    window_entry["excluded_years"] = exclusions
                windows[window_key] = window_entry
        basins[basin_id] = {
            "windows": windows,
            "quality": {
                "daily_records_in_reference_period": len(daily),
                "calendar_windows_expected": len(calendar_days) * len(durations),
                "calendar_windows_built": len(windows),
                "excluded_year_samples": excluded_sample_count,
            },
        }

    if failures:
        raise IncompleteClimatologyError(failures)

    expected_window_count = len(calendar_days) * len(durations)
    return {
        "schema_version": CLIMATOLOGY_SCHEMA_VERSION,
        "climatology_id": metadata["climatology_id"],
        "version": metadata["version"],
        "reference_period": reference_period,
        "provider": metadata["provider"],
        "source_product": metadata["source_product"],
        "source_path": normalized_source_path,
        "source_sha256": normalized_source_sha256,
        "basin_definition_version": metadata["basin_definition_version"],
        "source_mask_version": normalized_mask_version,
        "source_mask_limitation": normalized_mask_limitation,
        "minimum_complete_years": minimum_complete_years,
        "minimum_daily_coverage_pct": coverage_threshold,
        "durations_days": list(durations),
        "basins": basins,
        "quality": {
            "calendar_complete": True,
            "calendar_days": len(calendar_days),
            "calendar_windows_per_basin": expected_window_count,
            "basin_count": len(basin_ids),
            "input_daily_records": len(normalized),
            "used_daily_records": used_record_count,
            "ignored_records_outside_reference_period": outside_record_count,
        },
        "provenance": {
            "input_kind": "normalized_daily_basin_precipitation",
            "network_access": False,
            "algorithm": "calendar_window_sum_r7_quantiles_v1",
            "distribution_rule": "sum_raw_daily_values_then_compute_distribution",
            "daily_percentiles_summed": False,
        },
    }


def _validate_window_entry(
    key: str,
    entry: object,
    *,
    minimum_complete_years: int | None,
) -> None:
    match = _WINDOW_KEY_PATTERN.fullmatch(key)
    if not match:
        raise ClimatologyError(f"chave de janela invalida: {key!r}")
    try:
        date(2000, int(match.group("month")), int(match.group("day")))
    except ValueError as exc:
        raise ClimatologyError(f"data calendario invalida na janela: {key!r}") from exc
    duration = int(match.group("duration"))
    if duration <= 0:
        raise ClimatologyError(f"duracao invalida na janela: {key!r}")
    if not isinstance(entry, dict):
        raise ClimatologyError(f"janela {key!r} deve ser um objeto")
    if entry.get("duration_days") is not None and entry.get("duration_days") != duration:
        raise ClimatologyError(f"duration_days divergente na janela {key!r}")

    statistic_fields = ("mean_mm", "median_mm", "p10_mm", "p25_mm", "p75_mm", "p90_mm")
    values: dict[str, float] = {}
    for field in statistic_fields:
        if field not in entry:
            raise ClimatologyError(f"janela {key!r} sem {field}")
        values[field] = _finite_number(entry[field], field=f"{key}.{field}")
        if values[field] < 0:
            raise ClimatologyError(f"janela {key!r} possui precipitacao negativa")
    ordered = (
        values["p10_mm"],
        values["p25_mm"],
        values["median_mm"],
        values["p75_mm"],
        values["p90_mm"],
    )
    if any(left > right for left, right in zip(ordered, ordered[1:])):
        raise ClimatologyError(f"quantis nao monotonicos na janela {key!r}")

    if minimum_complete_years is not None:
        sample_count = entry.get("sample_count")
        if isinstance(sample_count, bool) or not isinstance(sample_count, int):
            raise ClimatologyError(f"janela {key!r} sem sample_count inteiro")
        if sample_count < minimum_complete_years:
            raise ClimatologyError(
                f"janela {key!r} tem {sample_count} amostras; minimo={minimum_complete_years}"
            )
        sample_years = entry.get("sample_years")
        if sample_years is not None and (
            not isinstance(sample_years, list) or len(sample_years) != sample_count
        ):
            raise ClimatologyError(
                f"janela {key!r} possui sample_years divergente de sample_count"
            )
        samples = entry.get("samples")
        if samples is not None and (
            not isinstance(samples, list) or len(samples) != sample_count
        ):
            raise ClimatologyError(f"janela {key!r} possui samples divergente de sample_count")


def _validate_bundle_data(data: object) -> dict:
    if not isinstance(data, dict):
        raise ClimatologyError("Climatologia deve ser um objeto JSON")
    data = dict(data)
    schema_version = data.get("schema_version")
    if schema_version != CLIMATOLOGY_SCHEMA_VERSION:
        raise ClimatologyError(
            f"schema_version de climatologia nao suportada: {schema_version!r}"
        )
    required = (
        "climatology_id",
        "version",
        "reference_period",
        "provider",
        "source_product",
        "basin_definition_version",
        "basins",
    )
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ClimatologyError(f"Climatologia sem campos obrigatorios: {', '.join(missing)}")
    _reference_bounds(str(data["reference_period"]))
    source_sha256 = data.get("source_sha256")
    if source_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", str(source_sha256)):
        raise ClimatologyError("source_sha256 invalido na climatologia")
    basins = data.get("basins")
    if not isinstance(basins, dict) or not basins:
        raise ClimatologyError("Climatologia deve conter um objeto basins nao vazio")

    minimum_complete_years = data.get("minimum_complete_years")
    if minimum_complete_years is not None:
        if (
            isinstance(minimum_complete_years, bool)
            or not isinstance(minimum_complete_years, int)
            or minimum_complete_years <= 0
        ):
            raise ClimatologyError("minimum_complete_years deve ser inteiro positivo")
    for basin_id, basin in basins.items():
        if not isinstance(basin_id, str) or not basin_id.strip() or not isinstance(basin, dict):
            raise ClimatologyError("cadastro de bacias invalido na climatologia")
        windows = basin.get("windows") if isinstance(basin.get("windows"), dict) else basin
        if not windows:
            raise ClimatologyError(f"bacia {basin_id!r} sem janelas de climatologia")
        for key, entry in windows.items():
            _validate_window_entry(
                str(key), entry, minimum_complete_years=minimum_complete_years
            )

    quality = data.get("quality")
    if not isinstance(quality, dict) or quality.get("calendar_complete") is not True:
        raise ClimatologyError("bundle v2 deve declarar quality.calendar_complete=true")
    expected_count = quality.get("calendar_windows_per_basin")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count <= 0:
        raise ClimatologyError("bundle v2 sem calendar_windows_per_basin valido")
    for basin_id, basin in basins.items():
        windows = basin.get("windows") if isinstance(basin.get("windows"), dict) else basin
        if len(windows) != expected_count:
            raise ClimatologyError(
                f"bacia {basin_id!r} possui {len(windows)} janelas; esperado={expected_count}"
            )
    provenance = data.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("algorithm") != "calendar_window_sum_r7_quantiles_v1"
        or provenance.get("daily_percentiles_summed") is not False
    ):
        raise ClimatologyError(
            "bundle v2 deve declarar soma diaria bruta antes dos quantis R7"
        )
    if not data.get("source_mask_version") and not data.get("source_mask_limitation"):
        raise ClimatologyError(
            "bundle v2 sem versao da mascara deve declarar source_mask_limitation"
        )
    return data


def load_climatology_bundle(
    path: str | Path, *, expected_basin_ids: tuple[str, ...] = ()
) -> ClimatologyBundle:
    resolved = Path(path).expanduser().resolve()
    try:
        content = resolved.read_bytes()
    except OSError as exc:
        raise ClimatologyError(f"Nao foi possivel ler a climatologia: {resolved}") from exc
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClimatologyError(f"Climatologia JSON invalida: {resolved}") from exc
    data = _validate_bundle_data(data)
    basin_ids = set(data["basins"])
    if expected_basin_ids and basin_ids != set(expected_basin_ids):
        absent = sorted(set(expected_basin_ids) - basin_ids)
        extra = sorted(basin_ids - set(expected_basin_ids))
        raise ClimatologyError(
            f"Climatologia com cadastro de bacias divergente; ausentes={absent}, extras={extra}"
        )
    return ClimatologyBundle(
        climatology_id=str(data["climatology_id"]),
        version=str(data["version"]),
        reference_period=str(data["reference_period"]),
        provider=str(data["provider"]),
        source_product=str(data["source_product"]),
        source_path=(str(data["source_path"]) if data.get("source_path") else None),
        source_sha256=(str(data["source_sha256"]) if data.get("source_sha256") else None),
        basin_definition_version=str(data["basin_definition_version"]),
        source_mask_version=(
            str(data["source_mask_version"]) if data.get("source_mask_version") else None
        ),
        source_mask_limitation=(
            str(data["source_mask_limitation"])
            if data.get("source_mask_limitation")
            else None
        ),
        sha256=hashlib.sha256(content).hexdigest(),
        path=str(resolved),
        data=data,
    )


def serialize_climatology_bundle(data: Mapping[str, object]) -> bytes:
    """Validate and deterministically serialize a climatology bundle."""

    validated = _validate_bundle_data(dict(data))
    return (
        json.dumps(validated, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def write_climatology_bundle_atomic(data: Mapping[str, object], path: str | Path) -> Path:
    """Validate and atomically publish a climatology JSON on the same filesystem."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = serialize_climatology_bundle(data)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as file_obj:
            temporary_path = Path(file_obj.name)
            file_obj.write(payload)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary_path, destination)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ClimatologyError(f"Nao foi possivel escrever climatologia: {destination}") from exc
    return destination
