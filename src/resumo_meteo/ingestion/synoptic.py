"""Objective GFS/EC 00 UTC synoptic-field materialization.

This adapter downloads only configured byte ranges, summarizes the normalized
South-America fields, and deletes every GRIB message immediately. It produces
no front/cyclone/ridge interpretation; those mechanisms require later analysis
of the objective evidence and must never be invented here.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

from resumo_meteo.core.constants import (
    ForecastModel,
    model_family,
    normalize_model_id,
    require_editorial_cycle,
)
from resumo_meteo.core.field_config import DEFAULT_MODEL_FIELD_CONFIG
from resumo_meteo.core.grib_fields import (
    FieldSelector,
    NormalizedWeatherField,
    read_selected_fields,
)
from resumo_meteo.core.horizons import validate_forecast_leads
from resumo_meteo.core.synoptic_regions import region_bounds
from resumo_meteo.sources.catalog import EC_PRODUCT_SLUG, GFS_PRODUCT_SLUG
from resumo_meteo.sources.ecmwf import (
    ECMWFOpenDataPrecipitationClient,
    select_ecmwf_index_records,
)
from resumo_meteo.sources.noaa import (
    NOAADownloadSelection,
    NOAAPrecipitationClient,
    select_noaa_index_records,
)

SCHEMA_VERSION = "weather-summary-synoptic.v3"
DEFAULT_OUTPUT_SUBDIR = "weather_summary_inputs/synoptic"
DEFAULT_STEP_HOURS = 12
MODEL_MAX_LEAD_HOURS = {ForecastModel.GFS: 336, ForecastModel.EC: 240}
WINDOWS = (("p1", 0, 120), ("p2", 120, 240), ("p3", 240, 336))
SYNOPTIC_REGIONS = region_bounds()
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class SynopticIngestionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True, slots=True)
class SynopticFieldSpec:
    field_id: str
    group: str
    parameter: str
    level_type: str
    level_hpa: int | None
    required: bool
    units_out: str
    noaa_variable: str
    noaa_level: str
    ecmwf_param: str | tuple[str, ...]
    ecmwf_levtype: str
    grib_short_names: tuple[str, ...]
    grib_type_of_level: str
    grib_level: int | None = None
    accumulation_semantics: str = "instantaneous"

    def to_dict(self) -> dict[str, object]:
        return {
            "field_id": self.field_id,
            "group": self.group,
            "parameter": self.parameter,
            "level_type": self.level_type,
            "level_hpa": self.level_hpa,
            "required": self.required,
            "units_out": self.units_out,
            "accumulation_semantics": self.accumulation_semantics,
            "providers": {
                "gfs": {"variable": self.noaa_variable, "level": self.noaa_level},
                "ec": {
                    "param": (
                        list(self.ecmwf_param)
                        if isinstance(self.ecmwf_param, tuple)
                        else self.ecmwf_param
                    ),
                    "levtype": self.ecmwf_levtype,
                    "level": self.level_hpa,
                },
            },
        }


_GRIB_SELECTORS = {
    "mean_sea_level_pressure": (("prmsl", "msl"), "meanSea", None),
    "temperature_2m": (("2t",), "heightAboveGround", 2),
    "wind_u_10m": (("10u",), "heightAboveGround", 10),
    "wind_v_10m": (("10v",), "heightAboveGround", 10),
    "shortwave_surface": (("dswrf", "ssrd"), "surface", None),
    "cape_surface": (("mucape", "cape"), "surface", None),
    "temperature_850": (("t",), "isobaricInhPa", 850),
    "relative_humidity_850": (("r",), "isobaricInhPa", 850),
    "wind_u_850": (("u",), "isobaricInhPa", 850),
    "wind_v_850": (("v",), "isobaricInhPa", 850),
    "geopotential_500": (("gh", "z"), "isobaricInhPa", 500),
    "vorticity_500": (("absv", "vo"), "isobaricInhPa", 500),
    "geopotential_250": (("gh", "z"), "isobaricInhPa", 250),
    "wind_u_250": (("u",), "isobaricInhPa", 250),
    "wind_v_250": (("v",), "isobaricInhPa", 250),
}


def _build_synoptic_field_specs() -> tuple[SynopticFieldSpec, ...]:
    gfs = {
        str(item["key"]): item
        for item in DEFAULT_MODEL_FIELD_CONFIG.fields("gfs")
        if item["role"] != "precipitation"
    }
    ec = {
        str(item["key"]): item
        for item in DEFAULT_MODEL_FIELD_CONFIG.fields("ec")
        if item["role"] != "precipitation"
    }
    if tuple(gfs) != tuple(ec) or set(gfs) != set(_GRIB_SELECTORS):
        raise ValueError("GFS/EC e seletores sinoticos divergem de model_fields.v1")
    specs = []
    for field_id, gfs_field in gfs.items():
        ec_field = ec[field_id]
        short_names, type_of_level, grib_level = _GRIB_SELECTORS[field_id]
        level_hpa = ec_field.get("level")
        role = str(gfs_field["role"])
        group = {
            "850_hpa": "850_hpa",
            "500_hpa": "500_hpa",
            "200_250_hpa": "250_hpa",
        }.get(role, "surface")
        level_type = "pressure" if level_hpa is not None else "surface"
        specs.append(
            SynopticFieldSpec(
                field_id=field_id,
                group=group,
                parameter=field_id,
                level_type=level_type,
                level_hpa=int(level_hpa) if level_hpa is not None else None,
                required=bool(gfs_field["required"] or ec_field["required"]),
                units_out=str(gfs_field["units_out"]),
                noaa_variable=str(gfs_field["variable"]),
                noaa_level=str(gfs_field["level"]),
                ecmwf_param=(
                    tuple(str(value) for value in ec_field["param"])
                    if isinstance(ec_field["param"], list)
                    else str(ec_field["param"])
                ),
                ecmwf_levtype=str(ec_field["levtype"]),
                grib_short_names=short_names,
                grib_type_of_level=type_of_level,
                grib_level=grib_level,
                accumulation_semantics=(
                    "provider_native_interval_or_accumulation"
                    if field_id == "shortwave_surface"
                    else "instantaneous"
                ),
            )
        )
    return tuple(specs)


SYNOPTIC_FIELD_SPECS = _build_synoptic_field_specs()


@dataclass(frozen=True, slots=True)
class SynopticFieldEvidence:
    field_id: str
    group: str
    parameter: str
    level_type: str
    level_hpa: int | None
    accumulation_semantics: str
    units: str
    point_count: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    p10: float | None
    p50: float | None
    p90: float | None
    min_location: Mapping[str, float] | None
    max_location: Mapping[str, float] | None
    source_message: Mapping[str, object]
    regional_summaries: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "field_id": self.field_id,
            "group": self.group,
            "parameter": self.parameter,
            "level_type": self.level_type,
            "level_hpa": self.level_hpa,
            "accumulation_semantics": self.accumulation_semantics,
            "units": self.units,
            "point_count": self.point_count,
            "min": self.minimum,
            "max": self.maximum,
            "mean": self.mean,
            "p10": self.p10,
            "p50": self.p50,
            "p90": self.p90,
            "min_location": dict(self.min_location) if self.min_location else None,
            "max_location": dict(self.max_location) if self.max_location else None,
            "regional_summaries": {
                key: dict(value)
                for key, value in self.regional_summaries.items()
            },
            "source_message": dict(self.source_message),
        }


@dataclass(frozen=True, slots=True)
class FetchedSynopticStep:
    lead_hour: int
    fields: tuple[SynopticFieldEvidence, ...]
    missing_fields: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class SynopticIngestionResult:
    model: str
    family: str
    source_id: str
    status: str
    reference_date: date
    cycle: str
    max_lead_h: int
    step_hours: int
    generated_at_utc: str
    configured_fields: tuple[SynopticFieldSpec, ...]
    timeline: tuple[dict[str, object], ...]
    missing_fields: tuple[dict[str, object], ...]
    failed_steps: tuple[dict[str, object], ...]
    quality_issues: tuple[dict[str, object], ...]

    @property
    def available_field_ids(self) -> tuple[str, ...]:
        available = {
            str(field["field_id"])
            for step in self.timeline
            for field in step.get("fields", [])
        }
        return tuple(spec.field_id for spec in self.configured_fields if spec.field_id in available)

    @property
    def field_count(self) -> int:
        return sum(len(step.get("fields", [])) for step in self.timeline)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "model": self.model,
            "family": self.family,
            "source_id": self.source_id,
            "status": self.status,
            "reference_date": self.reference_date.isoformat(),
            "cycle": self.cycle,
            "init_time_utc": datetime.combine(
                self.reference_date,
                datetime.min.time(),
                tzinfo=UTC,
            ).isoformat().replace("+00:00", "Z"),
            "max_lead_h": self.max_lead_h,
            "step_hours": self.step_hours,
            "generated_at_utc": self.generated_at_utc,
            "windows": [
                {"window_id": window_id, "start_lead_hour": start, "end_lead_hour": end}
                for window_id, start, end in WINDOWS
                if start < self.max_lead_h
            ],
            "configured_fields": [spec.to_dict() for spec in self.configured_fields],
            "available_field_ids": list(self.available_field_ids),
            "timeline": list(self.timeline),
            "missing_fields": list(self.missing_fields),
            "failed_steps": list(self.failed_steps),
            "quality": {"status": self.status, "issues": list(self.quality_issues)},
        }


@dataclass(frozen=True, slots=True)
class PersistedSynopticArtifacts:
    directory: str
    json_path: str
    csv_path: str
    manifest_path: str
    manifest_sha256: str
    files: tuple[dict[str, object], ...]


FieldReader = Callable[[Path, SynopticFieldSpec], NormalizedWeatherField]
StepFetcher = Callable[[date, str, int, Path], FetchedSynopticStep]


def _read_field(path: Path, spec: SynopticFieldSpec) -> NormalizedWeatherField:
    fields = read_selected_fields(
        path,
        selectors=(
            FieldSelector(
                key=spec.field_id,
                short_names=spec.grib_short_names,
                type_of_level=spec.grib_type_of_level,
                level=spec.grib_level,
            ),
        ),
    )
    if len(fields) != 1:
        raise SynopticIngestionError(
            "unexpected_grib_message_count",
            f"{spec.field_id}: esperada uma mensagem, recebidas {len(fields)}",
        )
    return fields[0]


def _summarize_values(values, longitudes, latitudes) -> dict[str, object]:
    finite = np.isfinite(values)
    selected = values[finite]
    if not selected.size:
        return {"point_count": 0}
    selected_lons = longitudes[finite]
    selected_lats = latitudes[finite]
    minimum_index = int(np.argmin(selected))
    maximum_index = int(np.argmax(selected))
    return {
        "point_count": int(selected.size),
        "min": float(selected[minimum_index]),
        "max": float(selected[maximum_index]),
        "mean": float(np.mean(selected)),
        "p10": float(np.quantile(selected, 0.10)),
        "p50": float(np.quantile(selected, 0.50)),
        "p90": float(np.quantile(selected, 0.90)),
        "min_location": {
            "longitude": float(selected_lons[minimum_index]),
            "latitude": float(selected_lats[minimum_index]),
        },
        "max_location": {
            "longitude": float(selected_lons[maximum_index]),
            "latitude": float(selected_lats[maximum_index]),
        },
    }


def _evidence(
    spec: SynopticFieldSpec,
    field: NormalizedWeatherField,
    source_message: Mapping[str, object],
) -> SynopticFieldEvidence:
    summary = _summarize_values(field.values, field.longitudes, field.latitudes)
    regional_summaries = {}
    for region_id, bounds in SYNOPTIC_REGIONS.items():
        mask = (
            (field.longitudes >= bounds["lon_min"])
            & (field.longitudes <= bounds["lon_max"])
            & (field.latitudes >= bounds["lat_min"])
            & (field.latitudes <= bounds["lat_max"])
        )
        regional_summaries[region_id] = _summarize_values(
            field.values[mask],
            field.longitudes[mask],
            field.latitudes[mask],
        )
    return SynopticFieldEvidence(
        field_id=spec.field_id,
        group=spec.group,
        parameter=spec.parameter,
        level_type=spec.level_type,
        level_hpa=spec.level_hpa,
        accumulation_semantics=spec.accumulation_semantics,
        units=field.units,
        point_count=int(summary.get("point_count") or 0),
        minimum=summary.get("min"),
        maximum=summary.get("max"),
        mean=summary.get("mean"),
        p10=summary.get("p10"),
        p50=summary.get("p50"),
        p90=summary.get("p90"),
        min_location=summary.get("min_location"),
        max_location=summary.get("max_location"),
        regional_summaries=regional_summaries,
        source_message=source_message,
    )


def _missing(spec: SynopticFieldSpec, lead_hour: int, code: str, message: str) -> dict[str, object]:
    return {
        "field_id": spec.field_id,
        "group": spec.group,
        "level_hpa": spec.level_hpa,
        "lead_hour": lead_hour,
        "code": code,
        "message": message,
    }


class GFSSynopticStepFetcher:
    def __init__(self, *, client=None, field_reader: FieldReader = _read_field):
        self.client = client or NOAAPrecipitationClient()
        self.field_reader = field_reader

    def __call__(self, reference_date: date, cycle: str, lead_hour: int, work_dir: Path) -> FetchedSynopticStep:
        cycle = require_editorial_cycle(cycle)
        records = self.client.fetch_index(
            GFS_PRODUCT_SLUG,
            reference_date,
            cycle=cycle,
            forecast_hour=lead_hour,
        )
        grib_url = self.client.grib_url(
            GFS_PRODUCT_SLUG,
            reference_date,
            cycle=cycle,
            forecast_hour=lead_hour,
        )
        index_url = f"{grib_url}.idx"
        fields: list[SynopticFieldEvidence] = []
        missing: list[dict[str, object]] = []
        for spec in SYNOPTIC_FIELD_SPECS:
            matches = select_noaa_index_records(
                records,
                variable=spec.noaa_variable,
                level=spec.noaa_level,
            )
            if len(matches) != 1:
                missing.append(
                    _missing(
                        spec,
                        lead_hour,
                        "source_message_missing",
                        f"GFS encontrou {len(matches)} mensagens",
                    )
                )
                continue
            record = matches[0]
            selection = NOAADownloadSelection(
                grib_url=grib_url,
                index_url=index_url,
                record=record,
                byte_length=record.length,
            )
            path = work_dir / f"gfs-f{lead_hour:03d}-{spec.field_id}.grib2"
            try:
                self.client.download_selection(selection, path)
                normalized = self.field_reader(path, spec)
                fields.append(
                    _evidence(
                        spec,
                        normalized,
                        {
                            "index_url": index_url,
                            "grib_url": grib_url,
                            "message_number": record.message_number,
                            "byte_offset": record.offset,
                            "byte_length": record.length,
                        },
                    )
                )
            except Exception as exc:
                missing.append(_missing(spec, lead_hour, "field_materialization_failed", str(exc)))
            finally:
                path.unlink(missing_ok=True)
        return _with_derived_wind(lead_hour, fields, missing)


class ECSynopticStepFetcher:
    def __init__(
        self,
        *,
        client=None,
        base_url: str | None = None,
        field_reader: FieldReader = _read_field,
    ):
        if client is not None and base_url is not None:
            raise ValueError("Informe client ou base_url para EC, nao ambos")
        self.client = client or ECMWFOpenDataPrecipitationClient(base_url=base_url)
        self.field_reader = field_reader

    def __call__(self, reference_date: date, cycle: str, lead_hour: int, work_dir: Path) -> FetchedSynopticStep:
        cycle = require_editorial_cycle(cycle)
        records = self.client.fetch_index(
            EC_PRODUCT_SLUG,
            reference_date,
            cycle=cycle,
            forecast_hour=lead_hour,
        )
        grib_url = self.client.grib_url(
            EC_PRODUCT_SLUG,
            reference_date,
            cycle=cycle,
            forecast_hour=lead_hour,
        )
        index_url = self.client.index_url(
            EC_PRODUCT_SLUG,
            reference_date,
            cycle=cycle,
            forecast_hour=lead_hour,
        )
        fields: list[SynopticFieldEvidence] = []
        missing: list[dict[str, object]] = []
        for spec in SYNOPTIC_FIELD_SPECS:
            matches = select_ecmwf_index_records(
                records,
                param=spec.ecmwf_param,
                level=spec.level_hpa,
                level_type=spec.ecmwf_levtype,
                data_type=("fc", "an"),
                stream="oper",
            )
            if len(matches) != 1:
                missing.append(
                    _missing(
                        spec,
                        lead_hour,
                        "source_message_missing",
                        f"EC encontrou {len(matches)} mensagens",
                    )
                )
                continue
            record = matches[0]
            path = work_dir / f"ec-f{lead_hour:03d}-{spec.field_id}.grib2"
            try:
                self.client.download_messages((record,), grib_url=grib_url, output_path=path)
                normalized = self.field_reader(path, spec)
                fields.append(
                    _evidence(
                        spec,
                        normalized,
                        {
                            "index_url": index_url,
                            "grib_url": grib_url,
                            "byte_offset": record.offset,
                            "byte_length": record.length,
                        },
                    )
                )
            except Exception as exc:
                missing.append(_missing(spec, lead_hour, "field_materialization_failed", str(exc)))
            finally:
                path.unlink(missing_ok=True)
        return _with_derived_wind(lead_hour, fields, missing)


def _with_derived_wind(lead_hour, fields, missing) -> FetchedSynopticStep:
    # Component summaries cannot be combined into speed. The actual derived
    # grids are intentionally computed in the reader/fetcher integration in a
    # future version; this v1 does not fabricate speed from component means.
    return FetchedSynopticStep(
        lead_hour=lead_hour,
        fields=tuple(fields),
        missing_fields=tuple(missing),
    )


def _window_id(lead_hour: int) -> str | None:
    if 0 <= lead_hour < 120:
        return "p1"
    if 120 <= lead_hour < 240:
        return "p2"
    if 240 <= lead_hour < 336:
        return "p3"
    return None


def ingest_synoptic_diagnostics(
    *,
    reference_date: date,
    model: str | ForecastModel,
    cycle: str = "00",
    lead_hours: Sequence[int] | None = None,
    step_fetcher: StepFetcher | None = None,
    ecmwf_base_url: str | None = None,
    temporary_root: str | Path | None = None,
    generated_at_utc: datetime | None = None,
) -> SynopticIngestionResult:
    if isinstance(reference_date, datetime) or not isinstance(reference_date, date):
        raise TypeError("reference_date deve ser date")
    canonical = normalize_model_id(model)
    if canonical not in MODEL_MAX_LEAD_HOURS:
        raise ValueError("Diagnostico sinotico aceita somente GFS e EC")
    cycle = require_editorial_cycle(cycle)
    max_lead = MODEL_MAX_LEAD_HOURS[canonical]
    requested_leads = tuple(
        lead_hours
        if lead_hours is not None
        else range(0, max_lead, DEFAULT_STEP_HOURS)
    )
    validated = tuple(int(value) for value in validate_forecast_leads(canonical, requested_leads))
    if not validated or validated[0] < 0 or validated[-1] >= max_lead:
        raise ValueError(
            f"Passos sinoticos de {canonical.value} devem ficar dentro de "
            f"0 <= lead < {max_lead} h; o limite final pertence a proxima janela"
        )
    fetcher = step_fetcher or (
        GFSSynopticStepFetcher()
        if canonical == ForecastModel.GFS
        else ECSynopticStepFetcher(base_url=ecmwf_base_url)
    )
    timeline: list[dict[str, object]] = []
    missing_fields: list[dict[str, object]] = []
    failed_steps: list[dict[str, object]] = []
    temp_parent = Path(temporary_root).resolve() if temporary_root else None
    if temp_parent:
        temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="weather-summary-synoptic-", dir=temp_parent) as work:
        work_root = Path(work)
        for lead_hour in validated:
            step_dir = work_root / f"f{lead_hour:03d}"
            step_dir.mkdir(parents=True, exist_ok=True)
            try:
                step = fetcher(reference_date, cycle, lead_hour, step_dir)
                field_dicts = [field.to_dict() for field in step.fields]
                missing_fields.extend(step.missing_fields)
                timeline.append(
                    {
                        "lead_hour": lead_hour,
                        "valid_time_utc": (
                            datetime.combine(reference_date, datetime.min.time(), tzinfo=UTC)
                            + timedelta(hours=lead_hour)
                        ).isoformat().replace("+00:00", "Z"),
                        "window_id": _window_id(lead_hour),
                        "fields": field_dicts,
                        "missing_field_ids": [
                            str(item["field_id"]) for item in step.missing_fields
                        ],
                    }
                )
            except Exception as exc:
                failed_steps.append(
                    {
                        "lead_hour": lead_hour,
                        "code": getattr(exc, "code", "synoptic_step_failed"),
                        "message": str(exc),
                        "error_type": exc.__class__.__name__,
                    }
                )
            finally:
                shutil.rmtree(step_dir, ignore_errors=True)
    issues = tuple((*missing_fields, *failed_steps))
    required_field_ids = {
        spec.field_id for spec in SYNOPTIC_FIELD_SPECS if spec.required
    }
    blocking_missing_fields = tuple(
        item
        for item in missing_fields
        if str(item.get("field_id") or "") in required_field_ids
    )
    blocking_issues = tuple((*blocking_missing_fields, *failed_steps))
    status = "complete"
    if blocking_issues:
        status = "partial"
    if not timeline or not any(step["fields"] for step in timeline):
        status = "failed"
    generated = generated_at_utc or datetime.now(UTC)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("generated_at_utc deve ter timezone explicito")
    return SynopticIngestionResult(
        model=canonical.value,
        family=model_family(canonical).value,
        source_id="noaa_gfs" if canonical == ForecastModel.GFS else "ecmwf_ec",
        status=status,
        reference_date=reference_date,
        cycle=cycle,
        max_lead_h=max(validated),
        step_hours=DEFAULT_STEP_HOURS,
        generated_at_utc=generated.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        configured_fields=SYNOPTIC_FIELD_SPECS,
        timeline=tuple(timeline),
        missing_fields=tuple(missing_fields),
        failed_steps=tuple(failed_steps),
        quality_issues=issues,
    )


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _entry(path: Path, root: Path, *, final_path: Path | None = None) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "name": path.name,
        "path": _relative(final_path or path, root),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _csv_bytes(result: SynopticIngestionResult) -> bytes:
    columns = (
        "model", "family", "lead_hour", "valid_time_utc", "window_id", "field_id",
        "group", "parameter", "level_type", "level_hpa", "units", "point_count",
        "min", "max", "mean", "p10", "p50", "p90", "min_location", "max_location",
    )
    binary = io.BytesIO()
    with gzip.GzipFile(fileobj=binary, mode="wb", mtime=0) as compressed:
        with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
            writer = csv.DictWriter(text, fieldnames=columns)
            writer.writeheader()
            for step in result.timeline:
                for field in step["fields"]:
                    writer.writerow(
                        {
                            **{key: field.get(key) for key in columns if key in field},
                            "model": result.model,
                            "family": result.family,
                            "lead_hour": step["lead_hour"],
                            "valid_time_utc": step["valid_time_utc"],
                            "window_id": step["window_id"],
                            "min_location": json.dumps(field.get("min_location"), separators=(",", ":")),
                            "max_location": json.dumps(field.get("max_location"), separators=(",", ":")),
                        }
                    )
    return binary.getvalue()


def persist_synoptic_ingestion(
    result: SynopticIngestionResult,
    *,
    media_root: str | Path,
    run_id: str,
    output_subdir: str = DEFAULT_OUTPUT_SUBDIR,
) -> PersistedSynopticArtifacts:
    if not _SAFE_RUN_ID.fullmatch(str(run_id or "")):
        raise ValueError("run_id deve ser identificador seguro")
    root = Path(media_root).resolve()
    subdir = Path(output_subdir)
    if subdir.is_absolute() or not subdir.parts or ".." in subdir.parts:
        raise ValueError("output_subdir deve ser relativo e seguro")
    parent = root / subdir / result.reference_date.strftime("%Y/%m/%d") / f"{result.cycle}z"
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / run_id
    if target.exists():
        raise FileExistsError(f"Diagnostico sinotico ja existe: {target}")
    staging_root = root / ".tmp"
    staging_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.", suffix=".tmp", dir=staging_root))
    try:
        json_path = temporary / "synoptic_diagnostics.json"
        json_path.write_bytes(
            json.dumps(
                result.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        csv_path = temporary / "synoptic_fields.csv.gz"
        csv_path.write_bytes(_csv_bytes(result))
        entries = (
            _entry(json_path, root, final_path=target / json_path.name),
            _entry(csv_path, root, final_path=target / csv_path.name),
        )
        manifest_path = temporary / "manifest.json"
        manifest_path.write_bytes(
            json.dumps(
                {
                    "schema_version": "weather-summary-synoptic-artifacts.v3",
                    "synoptic_schema_version": SCHEMA_VERSION,
                    "model": result.model,
                    "reference_date": result.reference_date.isoformat(),
                    "cycle": result.cycle,
                    "run_id": run_id,
                    "raw_grib_persisted": False,
                    "files": list(entries),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        )
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    json_final = target / json_path.name
    csv_final = target / csv_path.name
    manifest_final = target / manifest_path.name
    files = (_entry(json_final, root), _entry(csv_final, root), _entry(manifest_final, root))
    return PersistedSynopticArtifacts(
        directory=_relative(target, root),
        json_path=_relative(json_final, root),
        csv_path=_relative(csv_final, root),
        manifest_path=_relative(manifest_final, root),
        manifest_sha256=str(files[-1]["sha256"]),
        files=files,
    )


def build_weather_summary_synoptic_metadata(
    result: SynopticIngestionResult,
    artifacts: PersistedSynopticArtifacts,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "model": result.model,
        "family": result.family,
        "source_id": result.source_id,
        "status": result.status,
        "cycle": result.cycle,
        "reference_date": result.reference_date.isoformat(),
        "max_lead_h": result.max_lead_h,
        "step_hours": result.step_hours,
        "field_count": result.field_count,
        "available_field_ids": list(result.available_field_ids),
        "missing_fields": list(result.missing_fields),
        "failed_steps": list(result.failed_steps),
        "json_path": artifacts.json_path,
        "csv_path": artifacts.csv_path,
        "manifest_path": artifacts.manifest_path,
        "manifest_sha256": artifacts.manifest_sha256,
    }
