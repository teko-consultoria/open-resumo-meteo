"""Automatic, deterministic construction of the editorial analysis packet."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import unicodedata
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from ..core.basins import DEFAULT_BASIN_REGISTRY
from ..core.climatology import (
    ClimatologyBundle,
    ClimatologyError,
    load_climatology_bundle,
)
from ..core.consensus import compute_two_family_consensus
from ..core.ensemble import EnsembleStatistics, ensemble_statistics
from ..core.geometry_loader import (
    GeometryCatalog,
    GeometryLoadError,
    cells_near_geometry,
    load_geometry_catalog,
)
from ..core.grid_io import (
    AccumulatedGrid,
    DailyGrid,
    EnsembleInput,
    GridInputError,
    accumulate_daily_grids,
    load_daily_grids,
    load_ensemble_input,
    resolve_verified_artifact_file,
)
from ..core.observed_aggregation import observed_basin_aggregates_match_contract
from ..core.schema import AnalysisPacket
from ..core.spatial import (
    BasinGeometry,
    BasinPrecipitationAggregation,
    BasinSpatialWeights,
    _point_in_or_on_ring,
    aggregate_with_spatial_weights,
    compute_basin_spatial_weights,
)
from ..core.summary_scope import DEFAULT_SUMMARY_BASIN_SCOPE, SummaryBasinScope
from ..core.synoptic_features import (
    TEMPERATURE_CLIMATOLOGY_SOURCE_ID,
    detect_structured_synoptic_features,
)
from ..core.synoptic_regions import (
    SYNOPTIC_REGION_IDS,
    TEMPERATURE_CLIMATOLOGY_PATH,
    load_temperature_climatology,
)
from ..core.windows import build_forecast_windows

PIPELINE_VERSION = "weather-summary-packet-builder.v3"
FIELD_CONFIG_VERSION = "open-resumo-meteo-fields@1.0.0"
OBJECTIVE_CLASSIFIER_VERSION = "weather-summary-objective-classifiers.v2"
EXPECTED_MEMBERS = {"gfs": 1, "gefs": 31, "ec": 1, "ecens": 50}
EXPECTED_DAYS = {"gfs": 14, "gefs": 14, "ec": 10, "ecens": 14}
FAMILIES = {"gfs": "ncep", "gefs": "ncep", "ec": "ecmwf", "ecens": "ecmwf"}
PROVIDERS = {"gfs": "NOAA", "gefs": "NOAA", "ec": "ECMWF", "ecens": "ECMWF"}
WINDOW_OFFSETS = {"p1": (1, 5), "p2": (6, 10), "p3": (11, 14)}
WINDOW_DAYS = {"p1": 5, "p2": 5, "p3": 4}
SYNOPTIC_SCHEMA_VERSION = "weather-summary-synoptic.v3"
DEFAULT_CLIMATOLOGY_PATH = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "data"
    / "merge_inpe_2001_2020.climatology.json"
)
SYNOPTIC_WINDOW_BOUNDS = {"p1": (0, 120), "p2": (120, 240), "p3": (240, 336)}
FIELD_LEVELS = {
    "mean_sea_level_pressure": "surface",
    "temperature_2m": "surface",
    "wind_u_10m": "surface",
    "wind_v_10m": "surface",
    "shortwave_surface": "surface",
    "cape_surface": "surface",
    "temperature_850": "850",
    "relative_humidity_850": "850",
    "wind_u_850": "850",
    "wind_v_850": "850",
    "geopotential_500": "500",
    "vorticity_500": "500",
    "geopotential_250": "upper",
    "wind_u_250": "upper",
    "wind_v_250": "upper",
}
ENERGY_FIELD_IDS = {
    "wind": ("wind_u_10m", "wind_v_10m"),
    "temperature": ("temperature_2m",),
    "solar": ("shortwave_surface",),
    "severe_weather": ("cape_surface",),
}
# These classifiers deliberately use only deterministic GFS/EC fields already
# materialized by the pipeline.  A threshold is not enough on its own: the
# corresponding point extreme must fall inside one of the canonical basin
# geometries, the run must be complete and the unit must be comparable.  The
# resulting signals therefore describe point evidence, never basin averages,
# asset output or probabilities.
OBJECTIVE_THRESHOLDS = {
    "low_pressure_hpa": 990.0,
    "high_pressure_hpa": 1030.0,
    "heat_degc": 40.0,
    "cold_degc": 0.0,
    "wind_component_ms": 15.0,
    "cape_jkg": 3000.0,
}
_COMPARABLE_UNITS = {
    "pressure": frozenset(("hpa",)),
    "temperature": frozenset(("degc", "°c")),
    "wind": frozenset(("m s-1", "m s**-1", "m/s")),
    "cape": frozenset(("j kg-1", "j kg**-1", "j/kg")),
}
TIMELINE_FIELD_PRIORITY = (
    "mean_sea_level_pressure",
    "temperature_2m",
    "wind_u_850",
    "wind_v_850",
    "geopotential_500",
    "wind_u_250",
    "cape_surface",
)
_SPATIAL_WEIGHT_CACHE_MAX_ENTRIES = 128
_SPATIAL_WEIGHT_CACHE: OrderedDict[
    tuple[str, str, str], BasinSpatialWeights
] = OrderedDict()
_SPATIAL_WEIGHT_CACHE_LOCK = RLock()


class PacketBuildBlocked(RuntimeError):
    """A safe packet cannot be built; ``assessment`` is suitable for audit."""

    def __init__(self, message: str, *, assessment: Mapping[str, Any] | None = None) -> None:
        self.assessment = dict(assessment or {})
        super().__init__(message)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cutoff_utc deve incluir fuso horario")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_token(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9._:-]+", "-", text.lower()).strip("-._:")
    return text or "unknown"


def _block(assessment: dict[str, Any], code: str, message: str, **details) -> None:
    assessment.setdefault("blockers", []).append(code)
    if details:
        assessment.setdefault("blocker_details", {})[code] = details
    assessment["quality_manifest"] = {
        "generation_allowed": False,
        "blockers": list(assessment["blockers"]),
        "warnings": list(assessment.get("warnings", [])),
    }
    raise PacketBuildBlocked(message, assessment=assessment)


def _expected_dates(issue_date: date, window_id: str) -> tuple[date, ...]:
    start, end = WINDOW_OFFSETS[window_id]
    return tuple(issue_date + timedelta(days=offset) for offset in range(start, end + 1))


def _status(value: object, default: str = "partial") -> str:
    token = str(value or default).lower()
    return token if token in {"complete", "partial", "delayed", "missing", "failed", "not_applicable"} else default


def _aware_or(value: object, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return fallback


def _source_id(model: str, record) -> str:
    return f"{PROVIDERS[model].lower()}-{model}-{record.reference_date:%Y%m%d}-00"


def _manifest_checksum(record, ensemble: EnsembleInput | None = None) -> str | None:
    tokens = [str(item.get("sha256") or "") for item in record.grid_files if item.get("sha256")]
    processed = record.product_data.processed_data if isinstance(record.product_data.processed_data, dict) else {}
    contract = processed.get("weather_summary_ensemble") if isinstance(processed.get("weather_summary_ensemble"), dict) else {}
    if contract.get("manifest_sha256"):
        tokens.append(str(contract["manifest_sha256"]))
    synoptic = processed.get("weather_summary_synoptic") if isinstance(processed.get("weather_summary_synoptic"), dict) else {}
    if synoptic.get("manifest_sha256"):
        tokens.append(str(synoptic["manifest_sha256"]))
    if not tokens:
        return None
    return hashlib.sha256("\n".join(sorted(tokens)).encode("ascii", "ignore")).hexdigest()


def _source_entry(model: str, record, cutoff: datetime, status: str, ensemble=None) -> dict:
    collected = _aware_or(getattr(record.product_data, "processed_at", None), cutoff)
    latency = max((collected - record.run_datetime_utc).total_seconds() / 60.0, 0.0)
    return {
        "source_id": _source_id(model, record),
        "provider": PROVIDERS[model],
        "category": "forecast",
        "url": None,
        "collected_at": collected,
        "data_time": record.run_datetime_utc,
        "status": status,
        "latency_minutes": latency,
        "checksum_sha256": _manifest_checksum(record, ensemble),
        "license_or_terms": None,
        "variables": ["precipitation"],
        "limitations": list(record.issues),
    }


def _missing_source_entry(model: str, cutoff: datetime) -> dict:
    return {
        "source_id": f"{PROVIDERS[model].lower()}-{model}-missing-00",
        "provider": PROVIDERS[model],
        "category": "forecast",
        "url": None,
        "collected_at": cutoff,
        "data_time": None,
        "status": "missing",
        "latency_minutes": None,
        "checksum_sha256": None,
        "license_or_terms": None,
        "variables": ["precipitation"],
        "limitations": ["Rodada 00z não encontrada até o cutoff."],
    }


def _spatial_layout_signature(cells) -> str:
    digest = hashlib.sha256()
    for cell in cells:
        digest.update(cell.cell_id.encode("ascii"))
        digest.update(struct.pack("!4d", cell.west, cell.east, cell.south, cell.north))
    return digest.hexdigest()


def _aggregate(
    geometry,
    accumulated: AccumulatedGrid,
    *,
    weight_cache: dict[tuple[str, str, str], BasinSpatialWeights] | None = None,
) -> BasinPrecipitationAggregation | None:
    if not accumulated.cells:
        return None
    nearby = cells_near_geometry(geometry, accumulated.cells)
    if not nearby:
        return None
    cache_key = (
        geometry.geometry_version,
        geometry.basin_id,
        _spatial_layout_signature(nearby),
    )
    weights = weight_cache.get(cache_key) if weight_cache is not None else None
    if weights is None:
        with _SPATIAL_WEIGHT_CACHE_LOCK:
            weights = _SPATIAL_WEIGHT_CACHE.get(cache_key)
            if weights is not None:
                _SPATIAL_WEIGHT_CACHE.move_to_end(cache_key)
        if weights is None:
            computed = compute_basin_spatial_weights(geometry, nearby)
            with _SPATIAL_WEIGHT_CACHE_LOCK:
                weights = _SPATIAL_WEIGHT_CACHE.get(cache_key)
                if weights is None:
                    weights = computed
                    _SPATIAL_WEIGHT_CACHE[cache_key] = weights
                    while len(_SPATIAL_WEIGHT_CACHE) > _SPATIAL_WEIGHT_CACHE_MAX_ENTRIES:
                        _SPATIAL_WEIGHT_CACHE.popitem(last=False)
                else:
                    _SPATIAL_WEIGHT_CACHE.move_to_end(cache_key)
        if weight_cache is not None:
            weight_cache[cache_key] = weights
    return aggregate_with_spatial_weights(
        weights,
        {cell.cell_id: cell.value_mm for cell in nearby},
        minimum_valid_coverage_fraction=0.80,
    )


def _compact_basin_aggregation(raw: Mapping[str, object]) -> BasinPrecipitationAggregation:
    return BasinPrecipitationAggregation(
        basin_id=str(raw.get("basin_id") or ""),
        basin_mean_mm=(
            float(raw["basin_mean_mm"])
            if raw.get("basin_mean_mm") is not None
            else None
        ),
        spatial_p10_mm=(float(raw["spatial_p10_mm"]) if raw.get("spatial_p10_mm") is not None else None),
        spatial_median_mm=(float(raw["spatial_median_mm"]) if raw.get("spatial_median_mm") is not None else None),
        spatial_p90_mm=(float(raw["spatial_p90_mm"]) if raw.get("spatial_p90_mm") is not None else None),
        point_max_mm=(float(raw["point_max_mm"]) if raw.get("point_max_mm") is not None else None),
        point_max_cell_id=(str(raw["point_max_cell_id"]) if raw.get("point_max_cell_id") else None),
        grid_coverage_fraction=float(raw.get("grid_coverage_fraction") or 0),
        valid_data_coverage_fraction=float(raw.get("valid_data_coverage_fraction") or 0),
        covered_area=float(raw.get("covered_area") or 0),
        valid_data_area=float(raw.get("valid_data_area") or 0),
        area_above_threshold_fraction={
            str(key): float(value)
            for key, value in (
                raw.get("area_above_threshold_fraction") or {}
            ).items()
        },
        quality_flags=tuple(str(value) for value in (raw.get("quality_flags") or ())),
    )


def _ensemble_value(value: EnsembleStatistics, source_id: str) -> dict:
    return {
        "members_expected": int(value.expected_member_count or value.valid_member_count),
        "members_available": value.valid_member_count,
        "p10_mm": value.p10_mm,
        "p25_mm": value.p25_mm,
        "p50_mm": value.p50_mm,
        "p75_mm": value.p75_mm,
        "p90_mm": value.p90_mm,
        "mean_mm": value.mean_mm,
        "spread_mm": value.spread_mm,
        "coefficient_of_variation": value.coefficient_of_variation,
        "probabilities": {
            "gt_5mm": value.probability_gt(5),
            "gt_10mm": value.probability_gt(10),
            "gt_25mm": value.probability_gt(25),
            "gt_50mm": value.probability_gt(50),
            "gt_100mm": value.probability_gt(100),
        },
        "source_ids": [source_id],
    }


def _family_value(
    family: str,
    deterministic: tuple[float, str] | None,
    ensemble: tuple[EnsembleStatistics, str] | None,
) -> dict | None:
    if deterministic is None and ensemble is None:
        return None
    if ensemble is not None:
        stats, ensemble_source = ensemble
        central = stats.p50_mm
        inside = (
            stats.p10_mm <= deterministic[0] <= stats.p90_mm
            if deterministic is not None
            else None
        )
        confidence = "high" if stats.completeness_fraction == 1 and inside is not False else "medium"
        source_ids = ([deterministic[1]] if deterministic else []) + [ensemble_source]
    else:
        central = deterministic[0]  # type: ignore[index]
        inside = None
        confidence = "low"
        source_ids = [deterministic[1]]  # type: ignore[index]
    return {
        "family": family,
        "ensemble_median_mm": central,
        "calibrated_median_mm": None,
        "deterministic_mm": deterministic[0] if deterministic else None,
        "deterministic_inside_ensemble_p10_p90": inside,
        "confidence": confidence,
        "source_ids": source_ids,
    }


def _climatology_percentile(value: float, raw: Mapping[str, object]) -> float:
    points = (
        (0.0, 0.0),
        (float(raw["p10_mm"]), 10.0),
        (float(raw["p25_mm"]), 25.0),
        (float(raw["median_mm"]), 50.0),
        (float(raw["p75_mm"]), 75.0),
        (float(raw["p90_mm"]), 90.0),
    )
    if value <= points[0][0]:
        return 0.0
    for (left_value, left_pct), (right_value, right_pct) in zip(points, points[1:]):
        if value <= right_value:
            if math.isclose(left_value, right_value):
                return right_pct
            return left_pct + (value - left_value) * (right_pct - left_pct) / (right_value - left_value)
    p90 = points[-1][0]
    return min(100.0, 90.0 + 10.0 * (value - p90) / max(p90, 1.0))


def _climatology(raw: Mapping[str, object], bundle: ClimatologyBundle, opinion_mm: float) -> dict:
    required = ("mean_mm", "median_mm", "p10_mm", "p25_mm", "p75_mm", "p90_mm")
    try:
        values = {key: float(raw[key]) for key in required}
    except (KeyError, TypeError, ValueError) as exc:
        raise ClimatologyError(f"registro climatologico sem estatisticas: {required}") from exc
    if any(not math.isfinite(value) or value < 0 for value in values.values()):
        raise ClimatologyError("climatologia contem valor invalido")
    mean = values["mean_mm"]
    anomaly = opinion_mm - mean
    return {
        "climatology_id": bundle.climatology_id,
        **values,
        "anomaly_mm": anomaly,
        "anomaly_pct": (100.0 * anomaly / mean if mean > 0 else None),
        "percentile": _climatology_percentile(opinion_mm, values),
    }


def _round_opinion(value: float, spread: float) -> float:
    step = 10.0 if value > 100 or (value >= 20 and spread >= 20) else 5.0
    return max(round(value / step) * step, 0.0)


def _unavailable_level(label: str) -> dict:
    return {
        "summary": f"Indisponível: campos derivados de {label} não foram materializados.",
        "signals": [],
        "supported_by_families": [],
        "confidence": "low",
        "source_fields": [],
    }


def _synoptic_window() -> dict:
    return {
        "dominant_pattern": "indisponível",
        "timeline": [],
        "surface": _unavailable_level("superfície"),
        "low_levels_850_hpa": _unavailable_level("850 hPa"),
        "mid_levels_500_hpa": _unavailable_level("500 hPa"),
        "upper_levels_200_250_hpa": _unavailable_level("200/250 hPa"),
        "supported_features": [],
        "structured_features": [],
        "regional_temperature": [],
        "family_agreement": "not_available",
        "uncertainty": "Diagnóstico sinótico indisponível; nenhum sinal foi inferido apenas da chuva.",
    }


def _load_synoptic_input(record, *, repository) -> tuple[dict | None, str | None]:
    processed = (
        record.product_data.processed_data
        if isinstance(record.product_data.processed_data, dict)
        else {}
    )
    metadata = processed.get("weather_summary_synoptic")
    if not isinstance(metadata, dict):
        return None, None
    if str(metadata.get("status") or "") == "failed":
        return None, "weather_summary_synoptic marcado como failed"
    model = str(metadata.get("model") or "").lower()
    family = str(metadata.get("family") or "").lower()
    if (
        str(metadata.get("schema_version") or "") != SYNOPTIC_SCHEMA_VERSION
        or model != record.model_key
        or model not in {"gfs", "ec"}
        or family != record.family
        or str(metadata.get("cycle") or "") != "00"
        or str(metadata.get("reference_date") or "") != record.reference_date.isoformat()
    ):
        return None, "metadata weather_summary_synoptic divergente da rodada selecionada"
    json_path = str(metadata.get("json_path") or "").strip()
    if not json_path:
        return None, "metadata weather_summary_synoptic sem json_path"
    try:
        resolved = resolve_verified_artifact_file(
            metadata,
            repository=repository,
            manifest_schema_version="weather-summary-synoptic-artifacts.v3",
        )
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return None, f"JSON weather_summary_synoptic invalido: {type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "JSON weather_summary_synoptic deve ser objeto"
    for field, expected in (
        ("schema_version", SYNOPTIC_SCHEMA_VERSION),
        ("model", model),
        ("family", family),
        ("cycle", "00"),
        ("reference_date", record.reference_date.isoformat()),
    ):
        if str(payload.get(field) or "") != expected:
            return None, f"JSON weather_summary_synoptic com {field} divergente"
    if not isinstance(payload.get("timeline"), list) or not isinstance(payload.get("windows"), list):
        return None, "JSON weather_summary_synoptic sem timeline/windows"
    if not isinstance(payload.get("configured_fields"), list):
        return None, "JSON weather_summary_synoptic sem configured_fields"
    configured_field_ids = set()
    for raw_config in payload["configured_fields"]:
        if not isinstance(raw_config, dict):
            return None, "configured_fields contem item invalido"
        field_id = str(raw_config.get("field_id") or "")
        if field_id not in FIELD_LEVELS or field_id in configured_field_ids:
            return None, f"configured_fields contem field_id invalido/duplicado: {field_id}"
        configured_field_ids.add(field_id)
    available_field_ids = {
        str(value) for value in (metadata.get("available_field_ids") or ())
    }
    if not available_field_ids.issubset(configured_field_ids):
        return None, "available_field_ids diverge de configured_fields"
    try:
        step_hours = int(payload.get("step_hours") or metadata.get("step_hours"))
    except (TypeError, ValueError):
        return None, "step_hours sinotico invalido"
    if step_hours != 12:
        return None, "weather_summary_synoptic deve usar passo de 12 horas"
    declared_windows = {}
    for raw_window in payload["windows"]:
        if not isinstance(raw_window, dict):
            return None, "janela sinotica nao e objeto"
        window_id = str(raw_window.get("window_id") or "").lower()
        if window_id not in SYNOPTIC_WINDOW_BOUNDS:
            return None, f"janela sinotica desconhecida: {window_id}"
        try:
            bounds = int(raw_window.get("start_lead_hour")), int(raw_window.get("end_lead_hour"))
        except (TypeError, ValueError):
            return None, f"janela sinotica {window_id} sem limites inteiros"
        if bounds != SYNOPTIC_WINDOW_BOUNDS[window_id]:
            return None, f"janela sinotica {window_id} nao cobre integralmente {bounds}"
        if model == "ec" and window_id == "p3":
            return None, "EC nao pode fornecer diagnostico sinotico p3"
        declared_windows[window_id] = bounds
    if not declared_windows:
        return None, "JSON weather_summary_synoptic sem janela valida"

    normalized_timeline = []
    seen_lead_hours = set()
    for index, raw_step in enumerate(payload["timeline"]):
        if not isinstance(raw_step, dict):
            return None, f"timeline sinotica {index} nao e objeto"
        window_id = str(raw_step.get("window_id") or "").lower()
        if window_id not in declared_windows:
            return None, f"timeline sinotica {index} referencia janela nao declarada"
        try:
            lead_hour = int(raw_step.get("lead_hour"))
            valid_time = datetime.fromisoformat(
                str(raw_step.get("valid_time_utc") or "").replace("Z", "+00:00")
            ).astimezone(UTC)
        except (TypeError, ValueError):
            return None, f"timeline sinotica {index} sem lead/valid_time valido"
        start_lead, end_lead = declared_windows[window_id]
        expected_window_id = (
            "p1" if 0 <= lead_hour < 120
            else "p2" if 120 <= lead_hour < 240
            else "p3" if 240 <= lead_hour < 336
            else None
        )
        if (
            lead_hour < 0
            or lead_hour % step_hours != 0
            or lead_hour in seen_lead_hours
            or expected_window_id != window_id
            or not start_lead <= lead_hour < end_lead
        ):
            return None, f"timeline sinotica {index} fora da janela {window_id}"
        seen_lead_hours.add(lead_hour)
        if valid_time != record.run_datetime_utc + timedelta(hours=lead_hour):
            return None, f"timeline sinotica {index} com tempo valido inconsistente"
        if not isinstance(raw_step.get("fields"), list):
            return None, f"timeline sinotica {index} sem fields"
        fields = []
        for raw_field in raw_step["fields"]:
            if not isinstance(raw_field, dict):
                return None, f"campo sinotico {index} nao e objeto"
            field_id = str(raw_field.get("field_id") or "")
            if field_id not in configured_field_ids or field_id not in available_field_ids:
                return None, f"field_id sinotico nao declarado/disponivel: {field_id}"
            units = str(raw_field.get("units") or "").strip()
            try:
                point_count = int(raw_field.get("point_count"))
                values = {
                    key: float(raw_field.get(key))
                    for key in ("min", "p10", "p50", "mean", "p90", "max")
                }
            except (TypeError, ValueError):
                return None, f"estatisticas invalidas para {field_id}"
            ordered = (values["min"], values["p10"], values["p50"], values["p90"], values["max"])
            if (
                not units
                or point_count <= 0
                or any(not math.isfinite(value) for value in values.values())
                or any(left > right for left, right in zip(ordered, ordered[1:]))
                or not values["min"] <= values["mean"] <= values["max"]
            ):
                return None, f"estatisticas inseguras para {field_id}"
            raw_regions = raw_field.get("regional_summaries")
            if not isinstance(raw_regions, dict) or set(raw_regions) != set(
                SYNOPTIC_REGION_IDS
            ):
                return None, f"resumos regionais ausentes para {field_id}"
            regional_summaries = {}
            for region_id in SYNOPTIC_REGION_IDS:
                raw_region = raw_regions.get(region_id)
                if not isinstance(raw_region, dict):
                    return None, f"resumo regional invalido para {field_id}/{region_id}"
                try:
                    region_point_count = int(raw_region.get("point_count"))
                    region_values = {
                        key: float(raw_region.get(key))
                        for key in ("min", "p10", "p50", "mean", "p90", "max")
                    }
                except (TypeError, ValueError):
                    return None, f"estatisticas regionais invalidas para {field_id}/{region_id}"
                region_ordered = (
                    region_values["min"],
                    region_values["p10"],
                    region_values["p50"],
                    region_values["p90"],
                    region_values["max"],
                )
                if (
                    region_point_count <= 0
                    or any(not math.isfinite(value) for value in region_values.values())
                    or any(left > right for left, right in zip(region_ordered, region_ordered[1:]))
                    or not region_values["min"] <= region_values["mean"] <= region_values["max"]
                ):
                    return None, f"estatisticas regionais inseguras para {field_id}/{region_id}"
                regional_summaries[region_id] = {
                    "point_count": region_point_count,
                    **region_values,
                }
            fields.append(
                {
                    "field_id": field_id,
                    "units": units,
                    "point_count": point_count,
                    **values,
                    "min_location": raw_field.get("min_location"),
                    "max_location": raw_field.get("max_location"),
                    "accumulation_semantics": str(
                        raw_field.get("accumulation_semantics") or "unknown"
                    ),
                    "regional_summaries": regional_summaries,
                }
            )
        normalized_timeline.append(
            {
                "lead_hour": lead_hour,
                "valid_time": valid_time,
                "window_id": window_id,
                "fields": fields,
            }
        )
    return {
        "model": model,
        "family": family,
        "status": _status(payload.get("status") or metadata.get("status")),
        "step_hours": step_hours,
        "timeline": normalized_timeline,
        "available_field_ids": sorted(
            {field["field_id"] for step in normalized_timeline for field in step["fields"]}
        ),
        "quality": payload.get("quality") if isinstance(payload.get("quality"), dict) else {},
    }, None


def _field_text(field_id: str, rows: list[dict]) -> tuple[str, dict]:
    units = {row["units"] for row in rows}
    unit = next(iter(units)) if len(units) == 1 else "unidades divergentes"
    minimum = min(row["min"] for row in rows)
    maximum = max(row["max"] for row in rows)
    point_total = sum(row["point_count"] for row in rows)
    mean = sum(row["mean"] * row["point_count"] for row in rows) / point_total
    p10 = min(row["p10"] for row in rows)
    p90 = max(row["p90"] for row in rows)
    text = (
        f"{field_id}: {minimum:.1f} a {maximum:.1f} {unit}; "
        f"média espacial-temporal {mean:.1f} {unit}, faixa P10–P90 {p10:.1f}–{p90:.1f} {unit}."
    )
    return text, {
        "field_id": field_id,
        "unit": unit,
        "min": minimum,
        "max": maximum,
        "mean": mean,
        "locations": [
            location
            for row in rows
            for location in (row.get("min_location"), row.get("max_location"))
            if location
        ],
    }


def _location_label(value: object) -> str | None:
    if isinstance(value, dict):
        try:
            latitude = float(value.get("latitude"))
            longitude = float(value.get("longitude"))
        except (TypeError, ValueError):
            return None
        return f"grade em {latitude:.2f}°, {longitude:.2f}°"
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return f"grade em {float(value[1]):.2f}°, {float(value[0]):.2f}°"
        except (TypeError, ValueError):
            return None
    return None


def _objective_timeline_events(
    *,
    window,
    samples_by_valid_time: Mapping[datetime, Mapping[str, list[tuple[str, str, dict]]]],
    inputs: Mapping[str, dict],
) -> list[dict]:
    """Keep the 12-hour evolution without assigning a named mechanism."""

    events = []
    for valid_time in sorted(samples_by_valid_time):
        raw_end = valid_time + timedelta(hours=12)
        start = max(valid_time, window.start_utc)
        end = min(raw_end, window.end_utc)
        if end <= start:
            continue
        field_samples = samples_by_valid_time[valid_time]
        selected_fields = [
            field_id for field_id in TIMELINE_FIELD_PRIORITY if field_id in field_samples
        ][:5]
        if not selected_fields:
            selected_fields = sorted(field_samples)[:5]
        descriptions = []
        contributing_models = set()
        contributing_families = set()
        for field_id in selected_fields:
            values = field_samples[field_id]
            contributing_models.update(item[0] for item in values)
            contributing_families.update(item[1] for item in values)
            by_family: dict[str, list[dict]] = {}
            for _model, family, field in values:
                by_family.setdefault(family, []).append(field)
            family_values = []
            units = {field["units"] for _model, _family, field in values}
            unit = next(iter(units)) if len(units) == 1 else "unidades divergentes"
            for family in sorted(by_family):
                rows = by_family[family]
                weighted_total = sum(row["point_count"] for row in rows)
                objective_mean = (
                    sum(row["mean"] * row["point_count"] for row in rows) / weighted_total
                )
                maximum_row = max(rows, key=lambda row: row["max"])
                maximum_location = _location_label(maximum_row.get("max_location"))
                location_suffix = f", máximo {maximum_location}" if maximum_location else ""
                family_values.append(
                    f"{family.upper()} {objective_mean:.1f}{location_suffix}"
                )
            descriptions.append(
                f"{field_id} {' / '.join(family_values)} {unit}"
            )
        confidence = (
            "high"
            if contributing_families == {"ncep", "ecmwf"}
            and all(inputs[model]["status"] == "complete" for model in contributing_models)
            else "medium"
            if contributing_models
            and all(inputs[model]["status"] == "complete" for model in contributing_models)
            else "low"
        )
        family_label = ", ".join(family.upper() for family in sorted(contributing_families))
        events.append(
            {
                "start_utc": start,
                "end_utc": end,
                "description": (
                    f"Passo objetivo de 12 h. Famílias: {family_label}. "
                    f"Campos-chave: {'; '.join(descriptions)}."
                ),
                "timing_confidence": confidence,
            }
        )
    for previous, current in zip(events, events[1:]):
        if previous["end_utc"] > current["start_utc"]:
            raise ValueError("eventos objetivos da timeline nao podem se sobrepor")
    return events


def _unit_token(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip().casefold())


def _location_point(value: object) -> tuple[float, float] | None:
    """Return a validated ``(longitude, latitude)`` point from stored evidence."""

    try:
        if isinstance(value, dict):
            longitude = float(value.get("longitude"))
            latitude = float(value.get("latitude"))
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            longitude = float(value[0])
            latitude = float(value[1])
        else:
            return None
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(longitude)
        or not math.isfinite(latitude)
        or not -180.0 <= longitude <= 180.0
        or not -90.0 <= latitude <= 90.0
    ):
        return None
    return longitude, latitude


def _point_inside_geometry(point: tuple[float, float], geometry: BasinGeometry) -> bool:
    return any(
        _point_in_or_on_ring(point, polygon.exterior)
        and not any(_point_in_or_on_ring(point, hole) for hole in polygon.holes)
        for polygon in geometry.polygons
    )


def _basin_ids_at_location(
    value: object,
    geometry_catalog: GeometryCatalog,
    basin_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Locate point evidence without replacing a missing canonical contour."""

    point = _location_point(value)
    if point is None or geometry_catalog.crs.strip().upper() != "EPSG:4326":
        return ()
    return tuple(
        basin_id
        for basin_id in basin_ids
        if (geometry := geometry_catalog.geometries.get(basin_id)) is not None
        and _point_inside_geometry(point, geometry)
    )


def _threshold_hits(
    samples: Mapping[str, list[tuple[str, str, dict]]],
    *,
    field_ids: tuple[str, ...],
    inputs: Mapping[str, dict],
    geometry_catalog: GeometryCatalog,
    basin_ids: tuple[str, ...],
    unit_kind: str,
    extrema: tuple[str, ...],
    threshold_test,
    absolute: bool = False,
) -> tuple[list[dict], dict[str, object]]:
    """Select only complete, comparable and basin-located point extremes."""

    hits: list[dict] = []
    audit: dict[str, object] = {
        "incomplete": False,
        "outside_geometry": False,
        "invalid_location": False,
        "unit_mismatches": set(),
    }
    accepted_units = _COMPARABLE_UNITS[unit_kind]
    for field_id in field_ids:
        for model, family, field in samples.get(field_id, ()):  # type: ignore[arg-type]
            if inputs[model]["status"] != "complete":
                audit["incomplete"] = True
                continue
            unit = _unit_token(field.get("units"))
            if unit not in accepted_units:
                audit["unit_mismatches"].add(str(field.get("units") or "vazia"))  # type: ignore[union-attr]
                continue
            for extreme in extrema:
                raw_value = float(field[extreme])
                tested_value = abs(raw_value) if absolute else raw_value
                if not threshold_test(tested_value):
                    continue
                location = field.get(f"{extreme}_location")
                if _location_point(location) is None:
                    audit["invalid_location"] = True
                    continue
                located_basin_ids = _basin_ids_at_location(location, geometry_catalog, basin_ids)
                if not located_basin_ids:
                    audit["outside_geometry"] = True
                    continue
                hits.append(
                    {
                        "model": model,
                        "family": family,
                        "field_id": field_id,
                        "value": tested_value,
                        "raw_value": raw_value,
                        "basin_ids": located_basin_ids,
                    }
                )
    return hits, audit


def _families_share_basin(hits: list[dict]) -> bool:
    basins_by_family = {
        family: {
            str(basin_id)
            for hit in hits
            if hit["family"] == family
            for basin_id in hit["basin_ids"]
        }
        for family in ("ncep", "ecmwf")
    }
    return bool(basins_by_family["ncep"] & basins_by_family["ecmwf"])


def _signal_confidence(hits: list[dict]) -> str:
    return "high" if _families_share_basin(hits) else "medium"


def _signal_sources(hits: list[dict], source_ids: Mapping[str, str]) -> list[str]:
    models = {str(hit["model"]) for hit in hits}
    return [source_ids[model] for model in ("gfs", "ec") if model in models]


def _signal_areas(hits: list[dict], *, basin_ids: tuple[str, ...]) -> list[str]:
    hit_basin_ids = {
        str(basin_id)
        for hit in hits
        for basin_id in hit["basin_ids"]
    }
    return [
        DEFAULT_BASIN_REGISTRY.require_id(basin_id).locative_reference
        for basin_id in basin_ids
        if basin_id in hit_basin_ids
    ]


def _material_signal(
    *,
    signal_id: str,
    window_id: str,
    category: str,
    hits: list[dict],
    unit: str,
    meteorological_basis: str,
    operational_interpretation: str,
    source_ids: Mapping[str, str],
    limitations: tuple[str, ...],
    basin_ids: tuple[str, ...],
    allow_high_confidence: bool = True,
) -> dict:
    values = [float(hit["value"]) for hit in hits]
    families = {str(hit["family"]) for hit in hits}
    signal_limitations = list(limitations)
    if families != {"ncep", "ecmwf"}:
        signal_limitations.append(
            "O limiar foi sustentado por apenas uma das duas famílias independentes."
        )
    elif not _families_share_basin(hits):
        signal_limitations.append(
            "As duas famílias excederam o limiar somente em bacias distintas; a confiança não foi elevada."
        )
    return {
        "signal_id": signal_id,
        "window_id": window_id,
        "category": category,
        "material": True,
        "areas_or_assets": _signal_areas(hits, basin_ids=basin_ids),
        "meteorological_basis": meteorological_basis,
        "operational_interpretation": operational_interpretation,
        "value_min": round(min(values), 1),
        "value_max": round(max(values), 1),
        "unit": unit,
        "confidence": _signal_confidence(hits) if allow_high_confidence else "medium",
        "source_ids": _signal_sources(hits, source_ids),
        "limitations": signal_limitations,
    }


def _append_rejection_warnings(
    warnings: list[str],
    *,
    window_id: str,
    label: str,
    audit: Mapping[str, object],
) -> None:
    mismatches = sorted(str(value) for value in audit["unit_mismatches"])
    if mismatches:
        warnings.append(
            f"{window_id}: {label} não promovido com unidade não comparável: "
            f"{', '.join(mismatches)}."
        )
    if audit["incomplete"]:
        warnings.append(
            f"{window_id}: {label} de rodada sinótica parcial não foi promovido."
        )
    if audit["invalid_location"]:
        warnings.append(
            f"{window_id}: {label} excedeu o limiar sem coordenada extrema válida e não foi promovido."
        )
    if audit["outside_geometry"]:
        warnings.append(
            f"{window_id}: {label} excedeu o limiar somente fora dos contornos canônicos e não foi promovido."
        )


def _pressure_classifiers(
    samples: Mapping[str, list[tuple[str, str, dict]]],
    *,
    inputs: Mapping[str, dict],
    geometry_catalog: GeometryCatalog,
    basin_ids: tuple[str, ...],
) -> tuple[list[str], dict[str, list[dict]], dict[str, object], dict[str, object]]:
    low_hits, low_audit = _threshold_hits(
        samples,
        field_ids=("mean_sea_level_pressure",),
        inputs=inputs,
        geometry_catalog=geometry_catalog,
        basin_ids=basin_ids,
        unit_kind="pressure",
        extrema=("min",),
        threshold_test=lambda value: value <= OBJECTIVE_THRESHOLDS["low_pressure_hpa"],
    )
    high_hits, high_audit = _threshold_hits(
        samples,
        field_ids=("mean_sea_level_pressure",),
        inputs=inputs,
        geometry_catalog=geometry_catalog,
        basin_ids=basin_ids,
        unit_kind="pressure",
        extrema=("max",),
        threshold_test=lambda value: value >= OBJECTIVE_THRESHOLDS["high_pressure_hpa"],
    )
    features = []
    support: dict[str, list[dict]] = {}
    if low_hits:
        features.append("baixa pressão")
        support["baixa pressão"] = low_hits
    if high_hits:
        features.append("alta pressão")
        support["alta pressão"] = high_hits
    return features, support, low_audit, high_audit


def _mechanism_agreement(features: list[str], support: Mapping[str, list[dict]]) -> str:
    if not features:
        return "not_available"
    families_by_feature = {
        feature: {str(hit["family"]) for hit in support[feature]}
        for feature in features
    }
    if len(features) == 2 and families_by_feature[features[0]].isdisjoint(
        families_by_feature[features[1]]
    ):
        return "low"
    if all(_families_share_basin(support[feature]) for feature in features):
        return "high"
    return "medium"


def _objective_synoptic_and_energy(
    inputs: Mapping[str, dict],
    *,
    forecast_windows,
    source_ids: Mapping[str, str],
    geometry_catalog: GeometryCatalog,
    basin_ids: tuple[str, ...],
) -> tuple[dict, dict, dict[str, set[str]], list[str]]:
    diagnostics = {}
    energy = {"wind": [], "temperature": [], "solar": [], "severe_weather": []}
    fields_by_model: dict[str, set[str]] = {}
    warnings = []
    window_by_id = {item.window_id: item for item in forecast_windows}
    for model, value in inputs.items():
        fields_by_model[model] = set(value["available_field_ids"])

    for window_id in WINDOW_OFFSETS:
        samples: dict[str, list[tuple[str, str, dict]]] = {}
        timeline_samples: dict[
            datetime, dict[str, list[tuple[str, str, dict]]]
        ] = {}
        for model, value in inputs.items():
            for step in value["timeline"]:
                if step["window_id"] != window_id:
                    continue
                for field in step["fields"]:
                    samples.setdefault(field["field_id"], []).append((model, value["family"], field))
                    timeline_samples.setdefault(step["valid_time"], {}).setdefault(
                        field["field_id"], []
                    ).append((model, value["family"], field))
        if not samples:
            diagnostics[window_id] = _synoptic_window()
            continue

        level_values = {}
        for level in ("surface", "850", "500", "upper"):
            level_fields = {
                field_id: values
                for field_id, values in samples.items()
                if FIELD_LEVELS[field_id] == level
            }
            if not level_fields:
                label = {"surface": "superfície", "850": "850 hPa", "500": "500 hPa", "upper": "200/250 hPa"}[level]
                level_values[level] = _unavailable_level(label)
                continue
            signals = []
            families = set()
            statuses = []
            for field_id, values in sorted(level_fields.items()):
                text, _summary = _field_text(field_id, [item[2] for item in values])
                signals.append(text)
                families.update(item[1] for item in values)
                statuses.extend(inputs[item[0]]["status"] for item in values)
            confidence = (
                "high" if families == {"ncep", "ecmwf"} and all(item == "complete" for item in statuses)
                else "medium" if families else "low"
            )
            level_values[level] = {
                "summary": " ".join(signals),
                "signals": signals,
                "supported_by_families": sorted(families),
                "confidence": confidence,
                "source_fields": sorted(level_fields),
            }
        pressure_features, pressure_support, low_pressure_audit, high_pressure_audit = (
            _pressure_classifiers(
                samples,
                inputs=inputs,
                geometry_catalog=geometry_catalog,
                basin_ids=basin_ids,
            )
        )
        _append_rejection_warnings(
            warnings,
            window_id=window_id,
            label="baixa pressão objetiva",
            audit=low_pressure_audit,
        )
        _append_rejection_warnings(
            warnings,
            window_id=window_id,
            label="alta pressão objetiva",
            audit=high_pressure_audit,
        )
        if pressure_features == ["baixa pressão"]:
            dominant_pattern = (
                "Extremo objetivo de baixa pressão ao nível do mar dentro dos contornos canônicos."
            )
        elif pressure_features == ["alta pressão"]:
            dominant_pattern = (
                "Extremo objetivo de alta pressão ao nível do mar dentro dos contornos canônicos."
            )
        elif pressure_features:
            dominant_pattern = (
                "Extremos objetivos de baixa e alta pressão ao nível do mar dentro dos contornos canônicos."
            )
        else:
            dominant_pattern = (
                "Campos objetivos disponíveis; nenhum sistema sinótico foi nomeado automaticamente."
            )
        forecast_window = window_by_id[window_id]
        diagnostics[window_id] = {
            "dominant_pattern": dominant_pattern,
            "timeline": _objective_timeline_events(
                window=forecast_window,
                samples_by_valid_time=timeline_samples,
                inputs=inputs,
            ),
            "surface": level_values["surface"],
            "low_levels_850_hpa": level_values["850"],
            "mid_levels_500_hpa": level_values["500"],
            "upper_levels_200_250_hpa": level_values["upper"],
            "supported_features": pressure_features,
            "family_agreement": _mechanism_agreement(pressure_features, pressure_support),
            "uncertainty": (
                "Os únicos mecanismos classificáveis são extremos pontuais de pressão ao nível do mar "
                "dentro das bacias. O método não identifica frente, cavado, ciclone ou anticiclone, "
                "não mede probabilidade ensemble e não transforma extremos em médias de bacia."
            ),
        }

        heat_hits, heat_audit = _threshold_hits(
            samples,
            field_ids=ENERGY_FIELD_IDS["temperature"],
            inputs=inputs,
            geometry_catalog=geometry_catalog,
            basin_ids=basin_ids,
            unit_kind="temperature",
            extrema=("max",),
            threshold_test=lambda value: value >= OBJECTIVE_THRESHOLDS["heat_degc"],
        )
        cold_hits, cold_audit = _threshold_hits(
            samples,
            field_ids=ENERGY_FIELD_IDS["temperature"],
            inputs=inputs,
            geometry_catalog=geometry_catalog,
            basin_ids=basin_ids,
            unit_kind="temperature",
            extrema=("min",),
            threshold_test=lambda value: value <= OBJECTIVE_THRESHOLDS["cold_degc"],
        )
        wind_hits, wind_audit = _threshold_hits(
            samples,
            field_ids=ENERGY_FIELD_IDS["wind"],
            inputs=inputs,
            geometry_catalog=geometry_catalog,
            basin_ids=basin_ids,
            unit_kind="wind",
            extrema=("min", "max"),
            threshold_test=lambda value: value >= OBJECTIVE_THRESHOLDS["wind_component_ms"],
            absolute=True,
        )
        cape_hits, cape_audit = _threshold_hits(
            samples,
            field_ids=ENERGY_FIELD_IDS["severe_weather"],
            inputs=inputs,
            geometry_catalog=geometry_catalog,
            basin_ids=basin_ids,
            unit_kind="cape",
            extrema=("max",),
            threshold_test=lambda value: value >= OBJECTIVE_THRESHOLDS["cape_jkg"],
        )
        for label, audit in (
            ("calor extremo", heat_audit),
            ("frio extremo", cold_audit),
            ("vento forte", wind_audit),
            ("CAPE extremo", cape_audit),
        ):
            _append_rejection_warnings(
                warnings,
                window_id=window_id,
                label=label,
                audit=audit,
            )
        if heat_hits:
            energy["temperature"].append(
                _material_signal(
                    signal_id=f"temperature-heat-{window_id}",
                    window_id=window_id,
                    category="temperature",
                    hits=heat_hits,
                    unit="degC",
                    meteorological_basis=(
                        f"Máximo pontual de temperatura igual ou superior a "
                        f"{OBJECTIVE_THRESHOLDS['heat_degc']:.0f} °C dentro de bacia canônica."
                    ),
                    operational_interpretation=(
                        "Extremo de calor relevante para monitoramento meteorológico da carga; "
                        "não quantifica demanda ou MW."
                    ),
                    source_ids=source_ids,
                    limitations=(
                        "Evidência determinística e pontual, não média nem anomalia de bacia.",
                        "Não há modelo de carga acoplado, cadastro de ativos ou conversão para MW.",
                    ),
                    basin_ids=basin_ids,
                )
            )
        if cold_hits:
            energy["temperature"].append(
                _material_signal(
                    signal_id=f"temperature-cold-{window_id}",
                    window_id=window_id,
                    category="temperature",
                    hits=cold_hits,
                    unit="degC",
                    meteorological_basis=(
                        f"Mínimo pontual de temperatura igual ou inferior a "
                        f"{OBJECTIVE_THRESHOLDS['cold_degc']:.0f} °C dentro de bacia canônica."
                    ),
                    operational_interpretation=(
                        "Extremo de frio relevante para monitoramento meteorológico da carga; "
                        "não quantifica demanda ou MW."
                    ),
                    source_ids=source_ids,
                    limitations=(
                        "Evidência determinística e pontual, não média nem anomalia de bacia.",
                        "Não há modelo de carga acoplado, cadastro de ativos ou conversão para MW.",
                    ),
                    basin_ids=basin_ids,
                )
            )
        if wind_hits:
            energy["wind"].append(
                _material_signal(
                    signal_id=f"wind-component-{window_id}",
                    window_id=window_id,
                    category="wind",
                    hits=wind_hits,
                    unit="m s-1",
                    meteorological_basis=(
                        f"Módulo de componente pontual do vento a 10 m igual ou superior a "
                        f"{OBJECTIVE_THRESHOLDS['wind_component_ms']:.0f} m s-1 dentro de bacia canônica."
                    ),
                    operational_interpretation=(
                        "Vento forte em ponto de grade merece atenção operacional; "
                        "não estima geração eólica."
                    ),
                    source_ids=source_ids,
                    limitations=(
                        "O valor é módulo de uma componente u/v, limite inferior da velocidade vetorial, não velocidade calculada.",
                        "Evidência determinística e pontual, sem mapa de parques, altura de cubo ou curva de potência.",
                    ),
                    basin_ids=basin_ids,
                )
            )
        if cape_hits:
            energy["severe_weather"].append(
                _material_signal(
                    signal_id=f"severe-cape-{window_id}",
                    window_id=window_id,
                    category="severe_weather",
                    hits=cape_hits,
                    unit="J kg-1",
                    meteorological_basis=(
                        f"CAPE pontual igual ou superior a {OBJECTIVE_THRESHOLDS['cape_jkg']:.0f} "
                        "J kg-1 dentro de bacia canônica."
                    ),
                    operational_interpretation=(
                        "Ambiente termodinâmico extremo requer monitoramento convectivo; "
                        "isoladamente não confirma ocorrência de tempestade severa."
                    ),
                    source_ids=source_ids,
                    limitations=(
                        "CAPE isolado não representa cisalhamento, gatilho, rajada, granizo nem probabilidade de tempestade.",
                        "A definição de CAPE e da parcela pode diferir entre os provedores.",
                        "Evidência determinística e pontual, sem nowcasting ou observação convectiva.",
                    ),
                    basin_ids=basin_ids,
                    allow_high_confidence=False,
                )
            )

        if "shortwave_surface" in samples:
            solar_units = sorted(
                {str(field["units"]) for _model, _family, field in samples["shortwave_surface"]}
            )
            solar_semantics = sorted(
                {
                    str(field.get("accumulation_semantics") or "unknown")
                    for _model, _family, field in samples["shortwave_surface"]
                }
            )
            warnings.append(
                f"{window_id}: solar não promovido; shortwave_surface mantém unidade/semântica "
                f"não comparável entre provedores (unidades={solar_units}, semântica={solar_semantics}) "
                "e não há normalização de intervalo ou referência climatológica de irradiância."
            )
    structured, regional_temperature, classifier_warnings = (
        detect_structured_synoptic_features(
            inputs,
            forecast_windows=forecast_windows,
            model_source_ids=source_ids,
        )
    )
    warnings.extend(classifier_warnings)
    for window_id in WINDOW_OFFSETS:
        diagnostic = diagnostics.setdefault(window_id, _synoptic_window())
        window_features = structured.get(window_id, [])
        diagnostic["structured_features"] = window_features
        diagnostic["regional_temperature"] = regional_temperature.get(window_id, [])
        if not window_features:
            continue
        canonical_names = list(
            dict.fromkeys(str(item["canonical_name"]) for item in window_features)
        )
        diagnostic["supported_features"] = list(
            dict.fromkeys(
                [*(diagnostic.get("supported_features") or ()), *canonical_names]
            )
        )
        diagnostic["dominant_pattern"] = " ".join(
            str(item["description"]) for item in window_features[:3]
        )
        confidences = {str(item["confidence"]) for item in window_features}
        diagnostic["family_agreement"] = (
            "high" if confidences == {"high"} else "medium"
        )
        diagnostic["uncertainty"] = (
            "Mecanismos nomeados somente após regras físicas concordantes entre "
            "GFS e EC; datas, regiões e efeitos permanecem limitados aos eventos "
            "estruturados. O diagnóstico determinístico não é probabilidade ensemble."
        )
    return diagnostics, energy, fields_by_model, warnings


_ZCAS_NORTH_CENTRAL_BASINS = frozenset(
    ("madeira", "xingu", "tocantins")
)
_ZCAS_SOUTHEAST_EAST_BASINS = frozenset(
    (
        "paranaiba",
        "grande",
        "tiete",
        "paranapanema",
        "sao_francisco",
    )
)
_UNKNOWN_ZCAS_BASINS = (
    _ZCAS_NORTH_CENTRAL_BASINS | _ZCAS_SOUTHEAST_EAST_BASINS
) - set(DEFAULT_SUMMARY_BASIN_SCOPE.basin_ids)
if _UNKNOWN_ZCAS_BASINS:
    raise ValueError(
        f"classificador ZCAS possui bacias fora do escopo editorial: {sorted(_UNKNOWN_ZCAS_BASINS)}"
    )


def _longest_consecutive_dates(values: set[date]) -> int:
    longest = current = 0
    previous = None
    for value in sorted(values):
        if previous is not None and value == previous + timedelta(days=1):
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        previous = value
    return longest


def _corridor_days_from_forecast(
    daily_basin_aggregates: Mapping[
        str,
        Mapping[date, Mapping[str, BasinPrecipitationAggregation | None]],
    ],
    *,
    model: str,
    threshold_mm: float = 10.0,
) -> set[date]:
    dates = {
        valid_date
        for by_date in daily_basin_aggregates.values()
        for valid_date in by_date
    }
    supported = set()
    for valid_date in dates:
        north_wet = sum(
            1
            for basin_id in _ZCAS_NORTH_CENTRAL_BASINS
            if (
                (value := daily_basin_aggregates.get(basin_id, {}).get(valid_date, {}).get(model))
                is not None
                and value.basin_mean_mm is not None
                and value.basin_mean_mm >= threshold_mm
            )
        )
        southeast_wet = sum(
            1
            for basin_id in _ZCAS_SOUTHEAST_EAST_BASINS
            if (
                (value := daily_basin_aggregates.get(basin_id, {}).get(valid_date, {}).get(model))
                is not None
                and value.basin_mean_mm is not None
                and value.basin_mean_mm >= threshold_mm
            )
        )
        if north_wet >= 1 and southeast_wet >= 2:
            supported.add(valid_date)
    return supported


def _corridor_days_from_observed(
    daily_basin_aggregates: Mapping[
        str,
        Mapping[date, BasinPrecipitationAggregation | None],
    ],
    *,
    threshold_mm: float = 10.0,
) -> set[date]:
    dates = {
        valid_date
        for by_date in daily_basin_aggregates.values()
        for valid_date in by_date
    }
    supported = set()
    for valid_date in dates:
        north_wet = sum(
            1
            for basin_id in _ZCAS_NORTH_CENTRAL_BASINS
            if (
                (value := daily_basin_aggregates.get(basin_id, {}).get(valid_date))
                is not None
                and value.basin_mean_mm is not None
                and value.basin_mean_mm >= threshold_mm
            )
        )
        southeast_wet = sum(
            1
            for basin_id in _ZCAS_SOUTHEAST_EAST_BASINS
            if (
                (value := daily_basin_aggregates.get(basin_id, {}).get(valid_date))
                is not None
                and value.basin_mean_mm is not None
                and value.basin_mean_mm >= threshold_mm
            )
        )
        if north_wet >= 1 and southeast_wet >= 2:
            supported.add(valid_date)
    return supported


def _regional_moisture_support(inputs: Mapping[str, dict], *, window_id: str) -> set[str]:
    supported_models = set()
    for model in ("gfs", "ec"):
        value = inputs.get(model)
        if not isinstance(value, dict):
            continue
        consecutive = longest = 0
        for step in sorted(value.get("timeline") or (), key=lambda item: item["lead_hour"]):
            if step.get("window_id") != window_id:
                continue
            field = next(
                (
                    item
                    for item in step.get("fields") or ()
                    if item.get("field_id") == "relative_humidity_850"
                ),
                None,
            )
            regions = field.get("regional_summaries") if isinstance(field, dict) else {}
            central = regions.get("central_brazil") if isinstance(regions, dict) else None
            southeast = regions.get("southeast_brazil") if isinstance(regions, dict) else None
            wet = (
                isinstance(central, dict)
                and isinstance(southeast, dict)
                and float(central.get("p50") or 0) >= 65.0
                and float(southeast.get("p50") or 0) >= 65.0
            )
            if wet:
                consecutive += 1
                longest = max(longest, consecutive)
            else:
                consecutive = 0
        if longest >= 5:
            supported_models.add(model)
    return supported_models


def _apply_persistent_moisture_episode_diagnostics(
    diagnostics: dict,
    *,
    issue_date: date,
    synoptic_inputs: Mapping[str, dict],
    daily_basin_aggregates: Mapping[
        str,
        Mapping[date, Mapping[str, BasinPrecipitationAggregation | None]],
    ],
    observed_daily_basin_aggregates: Mapping[
        str,
        Mapping[date, BasinPrecipitationAggregation | None],
    ],
) -> None:
    """Promote only multi-day, spatially coherent moisture episodes.

    A forecast signal needs agreement between GFS and EC daily rain corridors
    plus persistent 850-hPa humidity in both deterministic families.  `zcas`
    is promoted only when the same broad corridor was also observed for at
    least three consecutive complete days.  This intentionally favors false
    negatives over naming a mechanism from a single wet pêntada.
    """

    forecast_corridor = {
        model: _corridor_days_from_forecast(
            daily_basin_aggregates,
            model=model,
        )
        for model in ("gfs", "ec")
    }
    observed_corridor_days = _corridor_days_from_observed(
        observed_daily_basin_aggregates
    )
    observed_persistence = _longest_consecutive_dates(observed_corridor_days)
    for window_id in SYNOPTIC_WINDOW_BOUNDS:
        if window_id == "p3":
            continue
        window_dates = set(_expected_dates(issue_date, window_id))
        common_corridor_days = (
            forecast_corridor["gfs"]
            & forecast_corridor["ec"]
            & window_dates
        )
        moisture_models = _regional_moisture_support(
            synoptic_inputs,
            window_id=window_id,
        )
        if (
            _longest_consecutive_dates(common_corridor_days) < 3
            or moisture_models != {"gfs", "ec"}
        ):
            continue
        diagnostic = diagnostics[window_id]
        features = list(diagnostic.get("supported_features") or ())
        if "convergência de umidade" not in features:
            features.append("convergência de umidade")
        if observed_persistence >= 3 and "zcas" not in features:
            features.append("zcas")
        diagnostic["supported_features"] = features
        diagnostic["family_agreement"] = "high"
        diagnostic["dominant_pattern"] = (
            "Corredor persistente de chuva e umidade em 850 hPa sustentado pelas duas famílias."
            if observed_persistence < 3
            else "Persistência observada e prevista compatível com ZCAS, sustentada pelas duas famílias."
        )
        diagnostic["uncertainty"] = (
            "Classificação multidiária exige corredor de chuva em bacias do Norte/Centro e Sudeste, "
            "umidade regional persistente em 850 hPa e concordância GFS/EC; ZCAS exige ainda três "
            "dias completos de suporte observado IMERG."
        )


def _model_run(model: str, record, daily_grids, ensemble, source_id: str) -> dict:
    expected_steps = EXPECTED_DAYS[model]
    if record is None:
        return {
            "model": model,
            "family": FAMILIES[model],
            "init_time": None,
            "available_at": None,
            "status": "missing",
            "cycle": "00",
            "version": None,
            "max_lead_h": 0,
            "expected_members": EXPECTED_MEMBERS[model],
            "available_members": 0,
            "expected_steps": expected_steps,
            "available_steps": 0,
            "available_variables": [],
            "missing_valid_times": [],
            "source_ids": [source_id],
            "limitations": ["Rodada 00z ausente."],
        }
    if model in {"gfs", "ec"}:
        available_steps = min(len(daily_grids or ()), expected_steps)
        available_members = 1 if available_steps else 0
    else:
        present_windows = {row.window_id for row in ensemble.rows} if ensemble else set()
        available_steps = sum(WINDOW_DAYS[item] for item in present_windows)
        available_members = ensemble.member_count if ensemble else 0
    status = _status(record.status)
    if available_steps < expected_steps or available_members < EXPECTED_MEMBERS[model]:
        status = "partial" if available_steps or available_members else "missing"
    if status == "complete" and (
        available_steps != expected_steps or available_members != EXPECTED_MEMBERS[model]
    ):
        status = "partial"
    limitations = list(record.issues)
    if model == "ec":
        limitations.append("EC determinístico termina em 240 h e não é extrapolado na p3.")
    return {
        "model": model,
        "family": FAMILIES[model],
        "init_time": record.run_datetime_utc,
        "available_at": _aware_or(getattr(record.product_data, "processed_at", None), record.run_datetime_utc),
        "status": status,
        "cycle": "00",
        "version": None,
        "max_lead_h": min(int(record.max_lead_h), 240 if model == "ec" else 336),
        "expected_members": EXPECTED_MEMBERS[model],
        "available_members": available_members,
        "expected_steps": expected_steps,
        "available_steps": available_steps,
        "available_variables": ["precipitation"] if available_steps else [],
        "missing_valid_times": [],
        "source_ids": [source_id],
        "limitations": limitations,
    }


def build_analysis_packet(
    issue_date: date,
    cutoff_utc: datetime,
    *,
    repository=None,
    climatology_path: str | Path | None = None,
    geometry_catalog: GeometryCatalog | None = None,
    summary_basin_scope: SummaryBasinScope | None = None,
    allow_missing_climatology: bool = False,
    anchor_hour_utc: int = 0,
) -> AnalysisPacket:
    """Build a validated packet from normalized 00z model and IMERG artefacts."""

    cutoff = _utc(cutoff_utc)
    if not 0 <= int(anchor_hour_utc) <= 23:
        raise ValueError("anchor_hour_utc deve estar entre 0 e 23")
    if isinstance(issue_date, datetime) or not isinstance(issue_date, date):
        raise TypeError("issue_date deve ser date")
    summary_scope = summary_basin_scope or DEFAULT_SUMMARY_BASIN_SCOPE
    summary_basin_ids = summary_scope.basin_ids
    assessment: dict[str, Any] = {
        "issue_date": issue_date.isoformat(),
        "cutoff_utc": _iso(cutoff),
        "required_cycle": "00",
        "authorized_models": ["gfs", "gefs", "ec", "ecens"],
        "observed_source": "gpm_imerg",
        "blockers": [],
        "warnings": [],
        "summary_basin_scope": summary_scope.to_dict(),
    }
    if repository is None:
        raise TypeError("repository standalone e obrigatorio")
    repo = repository
    try:
        model_records, repository_manifest = repo.select_model_inputs(
            cutoff_utc=cutoff,
            target_reference_date=issue_date,
        )
    except Exception as exc:
        _block(assessment, "model_input_selection_failed", "Falha selecionando inputs meteorológicos.", error=str(exc))
    assessment["repository_source_manifest"] = repository_manifest

    init_times = {record.run_datetime_utc for record in model_records.values()}
    if any(value.hour != 0 or value.minute != 0 or value.second != 0 for value in init_times):
        _block(assessment, "non_00z_model_run", "Foi encontrada rodada diferente de 00z.")
    if len(init_times) > 1:
        _block(
            assessment,
            "mixed_model_initializations",
            "Rodadas de inicializações diferentes não podem ser misturadas.",
            init_times=sorted(_iso(value) for value in init_times),
        )
    expected_init = datetime.combine(issue_date, time(), tzinfo=UTC)
    if init_times and init_times != {expected_init}:
        _block(
            assessment,
            "model_initialization_issue_date_mismatch",
            "A rodada 00z selecionada não corresponde à data de emissão.",
            issue_date=issue_date.isoformat(),
            init_times=sorted(_iso(value) for value in init_times),
        )
    if not model_records:
        _block(assessment, "forecast_precipitation_absent", "Nenhuma precipitação prevista 00z disponível.")

    try:
        geometries = geometry_catalog or load_geometry_catalog()
    except (GeometryLoadError, OSError, ValueError) as exc:
        _block(assessment, "basin_geometry_invalid", "Geometrias operacionais inválidas.", error=str(exc))
    assessment["geometry"] = {
        "version": geometries.version,
        "available_basin_ids": sorted(geometries.geometries),
        "missing_basin_ids": list(geometries.missing_basin_ids),
    }
    missing_summary_geometries = sorted(set(summary_basin_ids) - set(geometries.geometries))
    if missing_summary_geometries:
        _block(
            assessment,
            "summary_basin_geometry_missing",
            "Uma bacia do escopo do resumo não possui contorno canônico materializado.",
            missing_basin_ids=missing_summary_geometries,
        )
    if geometries.missing_basin_ids:
        assessment["warnings"].append(
            f"{len(geometries.missing_basin_ids)} bacias canônicas não possuem "
            "contorno direto; nenhuma geometria foi inventada."
        )

    climate: ClimatologyBundle | None = None
    climate_path = str(climatology_path or DEFAULT_CLIMATOLOGY_PATH).strip()
    if not climate_path:
        if allow_missing_climatology:
            assessment["warnings"].append(
                "Climatologia indisponível por decisão operacional explícita; não comparar com a média histórica."
            )
        else:
            _block(assessment, "climatology_not_configured", "Climatologia operacional não foi configurada.")
    else:
        try:
            climate = load_climatology_bundle(climate_path)
        except (OSError, ClimatologyError, ValueError) as exc:
            if allow_missing_climatology:
                assessment["warnings"].append(
                    f"Climatologia ignorada por indisponibilidade ({type(exc).__name__}); não comparar com a média histórica."
                )
                climate = None
            else:
                _block(assessment, "climatology_invalid", "Climatologia operacional inválida.", error=str(exc))
        if climate is not None:
            climate_basin_ids = set(climate.data.get("basins") or {})
            if climate_basin_ids != set(summary_basin_ids) and allow_missing_climatology:
                assessment["warnings"].append(
                    "Climatologia ignorada por incompatibilidade com o escopo do resumo; "
                    "não comparar com a média histórica."
                )
                climate = None
            elif climate_basin_ids != set(summary_basin_ids):
                _block(
                    assessment,
                    "climatology_basin_ids_mismatch",
                    "Climatologia não contém exatamente as bacias do escopo do resumo.",
                    climatology_basin_ids=sorted(climate_basin_ids),
                    expected_basin_ids=list(summary_basin_ids),
                )
            if (
                climate is not None
                and climate.source_mask_version
                and climate.source_mask_version != geometries.version
            ):
                if allow_missing_climatology:
                    assessment["warnings"].append(
                        "Climatologia ignorada porque sua máscara diverge da geometria operacional."
                    )
                    climate = None
                else:
                    _block(
                        assessment,
                        "climatology_geometry_version_mismatch",
                        "Climatologia e contornos usam máscaras incompatíveis.",
                        climatology_geometry_version=climate.source_mask_version,
                        basin_geometry_version=geometries.version,
                    )
            if climate is not None and climate.source_mask_limitation:
                assessment["warnings"].append(climate.source_mask_limitation)

    daily: dict[str, tuple[DailyGrid, ...]] = {}
    ensemble_inputs: dict[str, EnsembleInput] = {}
    input_errors: dict[str, str] = {}
    for model, record in model_records.items():
        try:
            if model in {"gfs", "ec"}:
                daily[model] = load_daily_grids(record.grid_files, repository=repo)
            else:
                value = load_ensemble_input(record, repository=repo)
                if value is not None:
                    ensemble_basin_ids = {row.basin_id for row in value.rows}
                    if ensemble_basin_ids != set(summary_basin_ids):
                        raise GridInputError(
                            f"{model} diverge das bacias do escopo do resumo: "
                            f"ensemble={sorted(ensemble_basin_ids)} "
                            f"escopo={sorted(summary_basin_ids)}"
                        )
                    ensemble_geometry_versions = {
                        row.geometry_version for row in value.rows
                    }
                    if ensemble_geometry_versions != {geometries.version}:
                        raise GridInputError(
                            f"{model} usa geometry_version divergente do catalogo atual"
                        )
                    ensemble_inputs[model] = value
        except (GridInputError, OSError, ValueError) as exc:
            input_errors[model] = str(exc)
    assessment["input_errors"] = input_errors

    accumulations: dict[str, dict[str, AccumulatedGrid]] = {}
    spatial_weight_cache: dict[tuple[str, str, str], BasinSpatialWeights] = {}
    basin_aggregates: dict[str, dict[str, dict[str, BasinPrecipitationAggregation | None]]] = {
        basin_id: {window: {} for window in WINDOW_OFFSETS} for basin_id in summary_basin_ids
    }
    for model in ("gfs", "ec"):
        if model not in daily:
            continue
        accumulations[model] = {}
        for window_id in WINDOW_OFFSETS:
            if model == "ec" and window_id == "p3":
                continue
            accumulated = accumulate_daily_grids(
                daily[model], expected_dates=_expected_dates(issue_date, window_id)
            )
            accumulations[model][window_id] = accumulated
            for basin_id in summary_basin_ids:
                geometry = geometries.geometries[basin_id]
                basin_aggregates[basin_id][window_id][model] = _aggregate(
                    geometry,
                    accumulated,
                    weight_cache=spatial_weight_cache,
                )

    # Daily deterministic timing is derived once and reused by all editorial
    # windows. It is never treated as a third independent forecast family.
    daily_basin_aggregates: dict[
        str,
        dict[date, dict[str, BasinPrecipitationAggregation | None]],
    ] = {basin_id: {} for basin_id in summary_basin_ids}
    for model in ("gfs", "ec"):
        for grid in daily.get(model, ()):
            single_day = AccumulatedGrid(
                cells=grid.cells,
                valid_days=1,
                expected_days=1,
                missing_dates=(),
            )
            for basin_id in summary_basin_ids:
                geometry = geometries.geometries[basin_id]
                daily_basin_aggregates[basin_id].setdefault(grid.valid_date, {})[model] = (
                    _aggregate(
                        geometry,
                        single_day,
                        weight_cache=spatial_weight_cache,
                    )
                )

    ensemble_stats: dict[str, dict[str, dict[str, EnsembleStatistics]]] = {}
    for model, value in ensemble_inputs.items():
        grouped: dict[tuple[str, str], list[float]] = {}
        for row in value.rows:
            if row.basin_id in summary_basin_ids:
                grouped.setdefault((row.basin_id, row.window_id), []).append(row.basin_mean_mm)
        for (basin_id, window_id), member_values in grouped.items():
            ensemble_stats.setdefault(basin_id, {}).setdefault(window_id, {})[model] = ensemble_statistics(
                member_values,
                expected_member_count=value.expected_member_count,
            )

    missing_forecast_windows = []
    missing_spatial_windows = []
    for basin_id in summary_basin_ids:
        for window_id in WINDOW_OFFSETS:
            det = basin_aggregates[basin_id][window_id]
            ens = ensemble_stats.get(basin_id, {}).get(window_id, {})
            complete_deterministic = any(
                item
                and item.basin_mean_mm is not None
                and accumulations.get(model, {}).get(window_id) is not None
                and accumulations[model][window_id].complete
                for model, item in det.items()
            )
            if not complete_deterministic and not ens:
                missing_forecast_windows.append(f"{basin_id}:{window_id}")
            if not complete_deterministic:
                missing_spatial_windows.append(f"{basin_id}:{window_id}")
    if missing_forecast_windows:
        _block(
            assessment,
            "forecast_precipitation_windows_missing",
            "Há bacias/janelas sem qualquer precipitação prevista.",
            basin_windows=missing_forecast_windows,
        )
    if missing_spatial_windows:
        _block(
            assessment,
            "spatial_distribution_unavailable",
            "A distribuição espacial requer ao menos uma grade determinística válida por janela.",
            basin_windows=missing_spatial_windows,
        )

    try:
        imerg_records, imerg_source = repo.select_imerg_history(
            cutoff_utc=cutoff,
            days=30,
            end_date=issue_date - timedelta(days=1),
        )
    except Exception as exc:
        imerg_records, imerg_source = (), None
        assessment["warnings"].append(f"Histórico IMERG indisponível: {type(exc).__name__}.")
    imerg_daily: tuple[DailyGrid, ...] = ()
    imerg_compact_history = bool(imerg_records) and all(
        observed_basin_aggregates_match_contract(
            getattr(record, "basin_aggregates", None),
            geometry_version=geometries.version,
            basin_ids=summary_basin_ids,
        )
        for record in imerg_records
    )
    if imerg_records and not imerg_compact_history:
        try:
            all_files = [item for record in imerg_records for item in record.grid_files]
            imerg_daily = load_daily_grids(all_files, repository=repo)
        except (GridInputError, OSError, ValueError) as exc:
            assessment["warnings"].append(f"Grades IMERG inválidas: {exc}")
            imerg_daily = ()
    elif imerg_compact_history:
        assessment.setdefault("checks", []).append(
            "imerg_basin_aggregates_reused_without_grid_reload"
        )

    imerg_partial_record = None
    imerg_partial_grid: DailyGrid | None = None
    imerg_partial_compact_compatible = False
    if hasattr(repo, "select_imerg_input"):
        try:
            candidate, _candidate_source = repo.select_imerg_input(cutoff_utc=cutoff)
            if (
                candidate is not None
                and candidate.temporal_resolution == "30_minutes"
                and not candidate.period_complete
                and candidate.observation_date == issue_date
            ):
                imerg_partial_record = candidate
                candidate_aggregates = getattr(candidate, "basin_aggregates", None)
                imerg_partial_compact_compatible = (
                    observed_basin_aggregates_match_contract(
                        candidate_aggregates,
                        geometry_version=geometries.version,
                        basin_ids=summary_basin_ids,
                    )
                )
                if not imerg_partial_compact_compatible:
                    partial_grids = load_daily_grids(candidate.grid_files, repository=repo)
                    if len(partial_grids) != 1:
                        raise GridInputError("Snapshot IMERG parcial deve conter uma grade")
                    imerg_partial_grid = partial_grids[0]
        except (GridInputError, OSError, ValueError) as exc:
            assessment["warnings"].append(
                f"Snapshot IMERG de 30 minutos indisponível: {type(exc).__name__}."
            )

    model_source_ids = {
        model: (_source_id(model, record) if record else f"{PROVIDERS[model].lower()}-{model}-missing-00")
        for model, record in ((model, model_records.get(model)) for model in EXPECTED_MEMBERS)
    }
    windows = build_forecast_windows(
        issue_date,
        boundary_hour_utc=int(anchor_hour_utc),
    )
    synoptic_inputs = {}
    synoptic_input_errors = {}
    for model in ("gfs", "ec"):
        record = model_records.get(model)
        if record is None:
            continue
        synoptic_value, synoptic_error = _load_synoptic_input(record, repository=repo)
        if synoptic_value is not None:
            synoptic_inputs[model] = synoptic_value
        elif synoptic_error:
            synoptic_input_errors[model] = synoptic_error
    assessment["synoptic_input_errors"] = synoptic_input_errors
    synoptic_diagnostics, energy_signals, synoptic_fields_by_model, synoptic_warnings = (
        _objective_synoptic_and_energy(
            synoptic_inputs,
            forecast_windows=windows,
            source_ids=model_source_ids,
            geometry_catalog=geometries,
            basin_ids=summary_basin_ids,
        )
    )
    assessment["warnings"].extend(synoptic_warnings)
    assessment["warnings"].extend(
        f"Diagnóstico sinótico {model} ignorado: {message}"
        for model, message in sorted(synoptic_input_errors.items())
    )
    if not synoptic_inputs:
        assessment["warnings"].append("Diagnósticos sinóticos não materializados.")
    if not synoptic_inputs and not any(energy_signals.values()):
        assessment["warnings"].append("Sinais de energia não materializados.")
    source_manifest = []
    model_runs = {}
    for model in EXPECTED_MEMBERS:
        record = model_records.get(model)
        ensemble_value = ensemble_inputs.get(model)
        run = _model_run(model, record, daily.get(model), ensemble_value, model_source_ids[model])
        run["available_variables"] = sorted(
            set(run["available_variables"]) | synoptic_fields_by_model.get(model, set())
        )
        model_runs[model] = run
        source_entry = (
            _source_entry(model, record, cutoff, run["status"], ensemble_value)
            if record
            else _missing_source_entry(model, cutoff)
        )
        source_entry["variables"] = sorted(
            set(source_entry["variables"]) | synoptic_fields_by_model.get(model, set())
        )
        source_manifest.append(source_entry)

    temperature_climatology = load_temperature_climatology()
    temperature_source = temperature_climatology["source"]
    source_manifest.append(
        {
            "source_id": TEMPERATURE_CLIMATOLOGY_SOURCE_ID,
            "provider": str(temperature_source["provider"]),
            "category": "climatology",
            "url": str(temperature_source["url"]),
            "collected_at": datetime.combine(
                date.fromisoformat(str(temperature_source["retrieved_once_on"])),
                time(),
                tzinfo=UTC,
            ),
            "data_time": datetime(2020, 12, 31, tzinfo=UTC),
            "status": "complete",
            "latency_minutes": None,
            "checksum_sha256": hashlib.sha256(
                TEMPERATURE_CLIMATOLOGY_PATH.read_bytes()
            ).hexdigest(),
            "license_or_terms": None,
            "variables": ["temperature_2m_climatology"],
            "limitations": [
                "Médias mensais regionais 1991–2020; não substituem climatologia diária local."
            ],
        }
    )

    latest_imerg_date = (
        max((record.observation_date for record in imerg_records), default=None)
        if imerg_compact_history
        else max((grid.valid_date for grid in imerg_daily), default=None)
    )
    if latest_imerg_date:
        imerg_history_end = datetime.combine(
            latest_imerg_date + timedelta(days=1), time(), tzinfo=UTC
        )
        imerg_history_status = _status(getattr(imerg_source, "status", "partial"))
        history_collected = _aware_or(
            getattr(imerg_records[-1].product_data, "processed_at", None), cutoff
        )
        history_latency = max(
            (cutoff - imerg_history_end).total_seconds() / 60.0,
            0.0,
        )
    else:
        imerg_history_end = None
        imerg_history_status = "missing"
        history_collected = cutoff
        history_latency = None
    imerg_history_source_id = "gpm-imerg-observed-history"
    source_manifest.append(
        {
            "source_id": imerg_history_source_id,
            "provider": "GPM_IMERG",
            "category": "observation",
            "url": None,
            "collected_at": history_collected,
            "data_time": imerg_history_end,
            "status": imerg_history_status,
            "latency_minutes": history_latency,
            "checksum_sha256": None,
            "license_or_terms": None,
            "variables": ["precipitation"],
            "limitations": list(getattr(imerg_source, "issues", ()) or ()),
        }
    )
    imerg_partial_source_id = None
    if imerg_partial_record is not None and (
        imerg_partial_grid is not None
        or imerg_partial_compact_compatible
    ):
        imerg_partial_source_id = "gpm-imerg-early-30min-latest"
        partial_end = imerg_partial_record.data_end_utc
        partial_collected = _aware_or(
            getattr(imerg_partial_record.product_data, "processed_at", None), cutoff
        )
        partial_latency = (
            max((cutoff - partial_end).total_seconds() / 60.0, 0.0)
            if partial_end is not None
            else None
        )
        source_manifest.append(
            {
                "source_id": imerg_partial_source_id,
                "provider": "GPM_IMERG",
                "category": "observation",
                "url": None,
                "collected_at": partial_collected,
                "data_time": partial_end,
                "status": _status(imerg_partial_record.status),
                "latency_minutes": partial_latency,
                "checksum_sha256": None,
                "license_or_terms": None,
                "variables": ["precipitation_30_minute_accumulation"],
                "limitations": list(imerg_partial_record.issues),
            }
        )
    if climate is not None:
        climate_source_id = (
            f"{_source_token(climate.provider)}-climatology-{_source_token(climate.version)}"
        )
        source_manifest.append(
            {
                "source_id": climate_source_id,
                "provider": climate.provider,
                "category": "climatology",
                "url": None,
                "collected_at": datetime.fromtimestamp(Path(climate.path).stat().st_mtime, tz=UTC),
                "data_time": None,
                "status": "complete",
                "latency_minutes": None,
                "checksum_sha256": climate.source_sha256 or climate.sha256,
                "license_or_terms": None,
                "variables": ["precipitation_climatology"],
                "limitations": (
                    [climate.source_mask_limitation]
                    if climate.source_mask_limitation
                    else []
                ),
            }
        )

    observed_accumulations: dict[int, AccumulatedGrid] = {}
    recent_observed_grids: tuple[DailyGrid, ...] = ()
    compact_observed_by_date: dict[
        date,
        dict[str, BasinPrecipitationAggregation],
    ] = {}
    if imerg_compact_history:
        for record in imerg_records:
            basin_rows = (
                getattr(record, "basin_aggregates", None) or {}
            ).get("basins") or []
            compact_observed_by_date[record.observation_date] = {
                str(raw["basin_id"]): _compact_basin_aggregation(raw)
                for raw in basin_rows
                if isinstance(raw, dict)
            }
    elif latest_imerg_date:
        for days in (1, 3, 5, 10, 30):
            dates = tuple(
                latest_imerg_date - timedelta(days=offset)
                for offset in reversed(range(days))
            )
            observed_accumulations[days] = accumulate_daily_grids(
                imerg_daily,
                expected_dates=dates,
            )
        recent_observed_grids = tuple(
            grid
            for grid in imerg_daily
            if grid.valid_date
            >= latest_imerg_date - timedelta(days=6)
        )
    partial_observed_accumulation = (
        AccumulatedGrid(
            cells=imerg_partial_grid.cells,
            valid_days=1,
            expected_days=1,
            missing_dates=(),
        )
        if imerg_partial_grid is not None
        else None
    )
    compact_partial_by_basin = (
        {
            str(raw["basin_id"]): _compact_basin_aggregation(raw)
            for raw in (
                (
                    getattr(imerg_partial_record, "basin_aggregates", None)
                    or {}
                ).get("basins")
                or []
            )
            if isinstance(raw, dict)
        }
        if imerg_partial_record is not None and imerg_partial_compact_compatible
        else {}
    )
    observed_daily_basin_aggregates: dict[
        str,
        dict[date, BasinPrecipitationAggregation | None],
    ] = {basin_id: {} for basin_id in summary_basin_ids}
    if imerg_compact_history and latest_imerg_date is not None:
        for valid_date, basin_values in compact_observed_by_date.items():
            if valid_date < latest_imerg_date - timedelta(days=6):
                continue
            for basin_id, value in basin_values.items():
                observed_daily_basin_aggregates[basin_id][valid_date] = value
    else:
        for grid in recent_observed_grids:
            single_day = AccumulatedGrid(
                cells=grid.cells,
                valid_days=1,
                expected_days=1,
                missing_dates=(),
            )
            for basin_id in summary_basin_ids:
                geometry = geometries.geometries[basin_id]
                observed_daily_basin_aggregates[basin_id][grid.valid_date] = _aggregate(
                    geometry,
                    single_day,
                    weight_cache=spatial_weight_cache,
                )

    _apply_persistent_moisture_episode_diagnostics(
        synoptic_diagnostics,
        issue_date=issue_date,
        synoptic_inputs=synoptic_inputs,
        daily_basin_aggregates=daily_basin_aggregates,
        observed_daily_basin_aggregates=observed_daily_basin_aggregates,
    )

    basins = []
    forecast_coverages = []
    observed_coverages = []
    for basin_id in summary_basin_ids:
        definition = DEFAULT_BASIN_REGISTRY.require_id(basin_id)
        basin_geometry = geometries.geometries[definition.basin_id]
        if imerg_compact_history:
            compact_daily = {
                valid_date: values.get(definition.basin_id)
                for valid_date, values in compact_observed_by_date.items()
            }
            latest_observed = (
                compact_daily.get(latest_imerg_date)
                if latest_imerg_date is not None
                else None
            )

            def complete_observed_mean(
                days: int,
                compact_daily=compact_daily,
                latest_imerg_date=latest_imerg_date,
            ):
                if latest_imerg_date is None:
                    return None
                expected = tuple(
                    latest_imerg_date - timedelta(days=offset)
                    for offset in reversed(range(days))
                )
                values = [compact_daily.get(valid_date) for valid_date in expected]
                if any(
                    value is None or value.basin_mean_mm is None
                    for value in values
                ):
                    return None
                return sum(value.basin_mean_mm for value in values)
        else:
            observed_values: dict[int, BasinPrecipitationAggregation | None] = {
                days: _aggregate(
                    basin_geometry,
                    accumulation,
                    weight_cache=spatial_weight_cache,
                )
                for days, accumulation in observed_accumulations.items()
            }
            latest_observed = (
                observed_values.get(1)
                if observed_accumulations.get(1)
                and observed_accumulations[1].complete
                else None
            )

            def complete_observed_mean(
                days: int,
                observed_accumulations=observed_accumulations,
                observed_values=observed_values,
            ):
                accumulation = observed_accumulations.get(days)
                aggregation = observed_values.get(days)
                return (
                    aggregation.basin_mean_mm
                    if accumulation and accumulation.complete and aggregation
                    else None
                )
        coverage_pct = (
            latest_observed.valid_data_coverage_fraction * 100 if latest_observed else None
        )
        if coverage_pct is not None:
            observed_coverages.append(coverage_pct)
        recent_daily = []
        for observed_date, daily_value in sorted(
            observed_daily_basin_aggregates[definition.basin_id].items()
        ):
            if daily_value is None or daily_value.basin_mean_mm is None:
                continue
            start_utc = datetime.combine(observed_date, time(), tzinfo=UTC)
            recent_daily.append(
                {
                    "date": observed_date,
                    "start_utc": start_utc,
                    "end_utc": start_utc + timedelta(days=1),
                    "basin_mean_mm": daily_value.basin_mean_mm,
                    "coverage_pct": daily_value.valid_data_coverage_fraction * 100,
                    "complete": True,
                }
            )
        partial_value = compact_partial_by_basin.get(definition.basin_id)
        if partial_value is None and partial_observed_accumulation is not None:
            partial_value = _aggregate(
                basin_geometry,
                partial_observed_accumulation,
                weight_cache=spatial_weight_cache,
            )
        observed_source_ids = [imerg_history_source_id]
        if imerg_partial_source_id:
            observed_source_ids.append(imerg_partial_source_id)
        observed_status = (
            _status(imerg_partial_record.status)
            if imerg_partial_record is not None
            else imerg_history_status
        )
        observation_data_end = (
            imerg_partial_record.data_end_utc
            if imerg_partial_record is not None
            else imerg_history_end
        )
        observation_limitations = []
        if imerg_history_status != "complete":
            observation_limitations.append("Histórico IMERG de dias completos está incompleto.")
        if imerg_partial_record is not None:
            observation_limitations.extend(imerg_partial_record.issues)
        observed = {
            "status": observed_status,
            "data_end_utc": observation_data_end,
            "rain_24h_mm": latest_observed.basin_mean_mm if latest_observed else None,
            "rain_72h_mm": complete_observed_mean(3),
            "rain_5d_mm": complete_observed_mean(5),
            "rain_10d_mm": complete_observed_mean(10),
            "rain_30d_mm": complete_observed_mean(30),
            "rain_30d_anomaly_pct": None,
            "event_accumulation_mm": None,
            "antecedent_index": None,
            "percentile": None,
            "coverage_pct": coverage_pct,
            "latest_partial_start_utc": (
                imerg_partial_record.data_start_utc
                if partial_value is not None and imerg_partial_record is not None
                else None
            ),
            "latest_partial_end_utc": (
                imerg_partial_record.data_end_utc
                if partial_value is not None and imerg_partial_record is not None
                else None
            ),
            "rain_latest_partial_mm": (
                partial_value.basin_mean_mm if partial_value is not None else None
            ),
            "latest_partial_coverage_pct": (
                partial_value.valid_data_coverage_fraction * 100
                if partial_value is not None
                else None
            ),
            "recent_daily": recent_daily,
            "source_ids": observed_source_ids,
            "limitations": list(dict.fromkeys(observation_limitations)),
        }

        basin_windows = {}
        for window_id in WINDOW_OFFSETS:
            det_values = basin_aggregates[definition.basin_id][window_id]
            stats_values = ensemble_stats.get(definition.basin_id, {}).get(window_id, {})
            deterministic_payload = {}
            for model in ("gfs", "ec"):
                aggregation = det_values.get(model)
                accumulated = accumulations.get(model, {}).get(window_id)
                deterministic_payload[model] = (
                    {
                        "raw_basin_mean_mm": aggregation.basin_mean_mm,
                        "corrected_basin_mean_mm": None,
                        "spatial_coverage_pct": aggregation.valid_data_coverage_fraction * 100,
                        "valid_steps": accumulated.valid_days,
                        "source_ids": [model_source_ids[model]],
                    }
                    if aggregation and aggregation.basin_mean_mm is not None and accumulated and accumulated.complete
                    else None
                )
            deterministic_payload["ec"] = None if window_id == "p3" else deterministic_payload["ec"]
            ensemble_payload = {
                model: (_ensemble_value(stats_values[model], model_source_ids[model]) if model in stats_values else None)
                for model in ("gefs", "ecens")
            }
            ncep_det = (
                (deterministic_payload["gfs"]["raw_basin_mean_mm"], model_source_ids["gfs"])
                if deterministic_payload["gfs"] else None
            )
            ecmwf_det = (
                (deterministic_payload["ec"]["raw_basin_mean_mm"], model_source_ids["ec"])
                if deterministic_payload["ec"] else None
            )
            ncep_ens = ((stats_values["gefs"], model_source_ids["gefs"]) if "gefs" in stats_values else None)
            ecmwf_ens = ((stats_values["ecens"], model_source_ids["ecens"]) if "ecens" in stats_values else None)
            family_ncep = _family_value("ncep", ncep_det, ncep_ens)
            family_ecmwf = _family_value("ecmwf", ecmwf_det, ecmwf_ens)
            consensus_inputs = {
                "gfs": ncep_det[0] if ncep_det else None,
                "gefs": ncep_ens[0] if ncep_ens else None,
                "ec": ecmwf_det[0] if ecmwf_det else None,
                "ecens": ecmwf_ens[0] if ecmwf_ens else None,
            }
            consensus = compute_two_family_consensus(consensus_inputs)
            ranges = []
            for item in (ncep_ens, ecmwf_ens):
                if item:
                    ranges.append((item[0].p25_mm, item[0].p75_mm))
            if not ranges:
                ranges = [(estimate.central_mm, estimate.central_mm) for estimate in consensus.family_estimates]
            lower, upper = min(item[0] for item in ranges), max(item[1] for item in ranges)
            spread = upper - lower
            rounded = _round_opinion(consensus.opinion_mm, spread)
            # Keep the auditable raw family/ensemble envelope while ensuring
            # the editorial rounded value is genuinely on the configured
            # 5/10 mm grid instead of being clamped to an arbitrary boundary.
            lower = min(lower, rounded)
            upper = max(upper, rounded)
            complete_spatial_models = [
                model
                for model in ("gfs", "ec")
                if deterministic_payload.get(model) is not None
                and det_values.get(model) is not None
            ]
            if not complete_spatial_models:
                raise GridInputError(
                    f"sem grade deterministica completa para {definition.basin_id}/{window_id}"
                )
            spatial_aggregation = det_values[complete_spatial_models[0]]
            family_confidences = [
                family["confidence"]
                for family in (family_ncep, family_ecmwf)
                if family is not None
            ]
            confidence = (
                "high"
                if consensus.agreement == "high"
                and len(consensus.family_estimates) == 2
                and family_confidences == ["high", "high"]
                and spatial_aggregation.valid_data_coverage_fraction >= 0.95
                else "medium" if len(consensus.family_estimates) == 2
                else "low"
            )
            forecast_coverages.append(spatial_aggregation.valid_data_coverage_fraction * 100)
            climatology_value = None
            if climate is not None:
                climate_raw = climate.basin_window(
                    definition.basin_id,
                    start_date=issue_date + timedelta(days=WINDOW_OFFSETS[window_id][0] - 1),
                    duration_days=WINDOW_DAYS[window_id],
                )
                if climate_raw is None:
                    if allow_missing_climatology:
                        assessment["warnings"].append(
                            f"Climatologia ausente para {definition.basin_id}/{window_id}; comparação histórica omitida."
                        )
                    else:
                        _block(
                            assessment,
                            "climatology_window_missing",
                            "Climatologia sem bacia/janela necessária.",
                            basin_id=definition.basin_id,
                            window_id=window_id,
                        )
                else:
                    climatology_value = _climatology(climate_raw, climate, consensus.opinion_mm)
            family_difference = (
                abs(family_ncep["ensemble_median_mm"] - family_ecmwf["ensemble_median_mm"])
                if family_ncep and family_ecmwf else None
            )
            agreement = consensus.agreement if consensus.agreement in {"high", "medium", "low"} else "low"
            warnings = list(consensus.quality_flags)
            missing_fields = [model for model, item in {**deterministic_payload, **ensemble_payload}.items() if item is None]
            if window_id == "p3" and "ec" in missing_fields:
                missing_fields.remove("ec")
            daily_timing = []
            for valid_date in _expected_dates(issue_date, window_id):
                daily_models = daily_basin_aggregates[definition.basin_id].get(
                    valid_date,
                    {},
                )
                gfs_daily = daily_models.get("gfs")
                ec_daily = daily_models.get("ec")
                gfs_mm = (
                    gfs_daily.basin_mean_mm
                    if gfs_daily is not None
                    else None
                )
                ec_mm = (
                    ec_daily.basin_mean_mm
                    if ec_daily is not None and window_id != "p3"
                    else None
                )
                available_daily = [
                    value for value in (gfs_mm, ec_mm) if value is not None
                ]
                center = (
                    sum(available_daily) / len(available_daily)
                    if available_daily
                    else None
                )
                difference = (
                    abs(gfs_mm - ec_mm)
                    if gfs_mm is not None and ec_mm is not None
                    else None
                )
                daily_timing.append(
                    {
                        "date": valid_date,
                        "gfs_basin_mean_mm": gfs_mm,
                        "ec_basin_mean_mm": ec_mm,
                        "deterministic_center_mm": center,
                        "family_difference_mm": difference,
                        "confidence": (
                            "high"
                            if difference is not None and difference <= 10
                            else "medium"
                            if difference is not None
                            else "low"
                        ),
                    }
                )
            basin_windows[window_id] = {
                "gfs": deterministic_payload["gfs"],
                "ec": deterministic_payload["ec"],
                "gefs": ensemble_payload["gefs"],
                "ecens": ensemble_payload["ecens"],
                "spatial": {
                    "basin_mean_mm": spatial_aggregation.basin_mean_mm,
                    "basin_median_mm": spatial_aggregation.spatial_median_mm,
                    "basin_p10_mm": spatial_aggregation.spatial_p10_mm,
                    "basin_p90_mm": spatial_aggregation.spatial_p90_mm,
                    "area_gt_5mm_pct": spatial_aggregation.area_above_threshold_fraction["gt_5_mm"] * 100,
                    "area_gt_10mm_pct": spatial_aggregation.area_above_threshold_fraction["gt_10_mm"] * 100,
                    "area_gt_25mm_pct": spatial_aggregation.area_above_threshold_fraction["gt_25_mm"] * 100,
                    "area_gt_50mm_pct": spatial_aggregation.area_above_threshold_fraction["gt_50_mm"] * 100,
                    "point_max_mm": spatial_aggregation.point_max_mm,
                    "point_max_is_gridpoint": True,
                },
                "climatology": climatology_value,
                "families": {
                    "ncep": family_ncep,
                    "ecmwf": family_ecmwf,
                    "agreement": agreement,
                    "difference_mm": family_difference,
                    "same_signal": (
                        (family_ncep["ensemble_median_mm"] >= climatology_value["mean_mm"])
                        == (family_ecmwf["ensemble_median_mm"] >= climatology_value["mean_mm"])
                        if family_ncep and family_ecmwf and climatology_value is not None else None
                    ),
                    "same_spatial_pattern": None,
                    "notes": warnings,
                },
                "opinion": {
                    "range_mm": [lower, upper],
                    "rounded_mm": rounded,
                    "confidence": confidence,
                    "basis": "two_family_consensus" if len(consensus.family_estimates) == 2 else "single_family",
                    "family_weights": consensus.normalized_family_weights,
                    "uncertainty_note": (
                        None if confidence == "high" else "Confiança reduzida por cobertura/completude ou apenas uma família disponível."
                    ),
                },
                "quality": {
                    "status": "complete" if not missing_fields else "partial",
                    "warnings": warnings,
                    "missing_fields": missing_fields,
                    "coverage_pct": spatial_aggregation.valid_data_coverage_fraction * 100,
                },
                "daily_timing": daily_timing,
            }
        basins.append(
            {
                "basin_id": definition.basin_id,
                "name": definition.canonical_name,
                "editorial_reference": definition.locative_reference,
                "aliases": list(definition.aliases),
                "subsystem": definition.subsystem,
                "geometry_version": geometries.version,
                "geometry_status": "complete",
                "geometry_source_name": geometries.source_name,
                "geometry_limitations": [],
                "area_km2": geometries.area_km2[definition.basin_id],
                "observed": observed,
                "windows": basin_windows,
            }
        )

    missing_sources = [
        item["source_id"]
        for item in source_manifest
        if item["status"] in {"missing", "failed"}
    ]
    delayed_sources = [item["source_id"] for item in source_manifest if item["status"] == "delayed"]
    incomplete_sources = [item["source_id"] for item in source_manifest if item["status"] == "partial"]
    family_presence = {
        FAMILIES[model] for model in model_records if daily.get(model) or ensemble_inputs.get(model)
    }
    if len(family_presence) < 2:
        assessment["warnings"].append("Apenas uma família independente contribui para a previsão.")
    manifest_hash = _json_hash(source_manifest)
    packet_payload = {
        "schema_version": 2,
        "issue": {
            "date": issue_date,
            "time": cutoff.astimezone(ZoneInfo("America/Sao_Paulo")).time().replace(tzinfo=None),
            "timezone": "America/Sao_Paulo",
            "cutoff_utc": cutoff,
        },
        "forecast_windows": [item.to_dict() for item in windows],
        "model_runs": model_runs,
        "basins": basins,
        "synoptic_diagnostics": synoptic_diagnostics,
        "energy_signals": energy_signals,
        "material_change": None,
        "rounding_mm": {},
        "source_manifest": source_manifest,
        "quality": {
            "generation_allowed": True,
            "blockers": [],
            "warnings": assessment["warnings"],
            "missing_sources": missing_sources,
            "delayed_sources": delayed_sources,
            "incomplete_sources": incomplete_sources,
            "climatology_id": climate.climatology_id if climate is not None else None,
            "basin_geometry_version": geometries.version,
            "summary_basin_scope_id": summary_scope.scope_id,
            "summary_basin_scope_version": summary_scope.scope_version,
            "summary_basin_ids": list(summary_basin_ids),
            "observed_coverage_pct": (
                sum(observed_coverages) / len(summary_basin_ids)
                if observed_coverages
                else None
            ),
            "forecast_coverage_pct": sum(forecast_coverages)
            / (len(summary_basin_ids) * len(WINDOW_OFFSETS)),
            "checks_passed": [
                "authorized_models_only",
                "cycle_00z_only",
                "no_mixed_initializations",
                "windows_5_5_4",
                "ec_not_extrapolated",
                "canonical_basin_ids",
                "summary_basin_scope_versioned",
                "area_weighted_basin_aggregation",
                "forecast_observed_geometry_catalog_aligned",
                "two_independent_families_without_double_counting",
                "objective_energy_and_pressure_classifiers_fail_closed",
                "structured_synoptic_features_require_gfs_ec_agreement",
                "temperature_2m_checked_against_static_1991_2020_climatology",
            ]
            + (
                ["imerg_basin_aggregates_reused_without_grid_reload"]
                if imerg_compact_history
                else []
            )
            + (["climatology_versioned"] if climate is not None else ["climatology_absence_audited"]),
        },
        "audit": {
            "packet_id": f"weather-summary:{issue_date:%Y%m%d}:{manifest_hash[:16]}",
            "created_at": datetime.now(UTC),
            "pipeline_version": PIPELINE_VERSION,
            "field_config_version": FIELD_CONFIG_VERSION,
            "code_revision": None,
            "input_manifest_sha256": manifest_hash,
            "notes": [
                "Chuva observada exclusivamente de grades normalizadas GPM IMERG.",
                (
                    "Chuva prevista e observada agregadas com o mesmo catálogo geométrico "
                    f"{geometries.version}."
                ),
                f"Classificadores objetivos: {OBJECTIVE_CLASSIFIER_VERSION}.",
                (
                    f"Resumo usa {len(summary_basin_ids)} bacias do escopo {summary_scope.scope_id} "
                    f"sobre catálogo canônico completo de {len(geometries.geometries)} contornos."
                ),
                *(
                    ["Execução degradada autorizada sem climatologia; nenhuma anomalia histórica foi calculada."]
                    if climate is None
                    else []
                ),
            ],
        },
    }
    assessment["source_manifest"] = source_manifest
    try:
        return AnalysisPacket.model_validate(packet_payload)
    except (ValidationError, ValueError, TypeError) as exc:
        errors = exc.errors(include_url=False) if isinstance(exc, ValidationError) else [{"message": str(exc)}]
        _block(
            assessment,
            "analysis_packet_invalid",
            "Pacote montado não passou no schema tipado.",
            errors=errors,
        )


__all__ = ["PacketBuildBlocked", "build_analysis_packet"]
