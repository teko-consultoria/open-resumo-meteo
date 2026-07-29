"""Current typed contract between weather processing and editorial generation.

The packet contains only derived, auditable diagnostics. Raw GRIB, rasters and
geometries stay on the processing side of the boundary. The editorial payload
uses the versioned 12-basin scope covered by the MERGE/INPE climatology.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from .basins import (
    CANONICAL_BASIN_NAME_BY_ID,
    format_basin_editorial_reference,
)

ANALYSIS_PACKET_SCHEMA_VERSION = 2
ANALYSIS_PACKET_JSON_SCHEMA_ID = "urn:open-resumo-meteo:schema:analysis-packet:v2"


class StrictModel(BaseModel):
    """Base class used by every public object in the packet."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a UTC offset")
    return value.astimezone(UTC)


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
Percentage = Annotated[float, Field(ge=0, le=100)]
Probability = Annotated[float, Field(ge=0, le=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]


class WindowId(str, Enum):
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


class ModelId(str, Enum):
    GFS = "gfs"
    GEFS = "gefs"
    EC = "ec"
    ECENS = "ecens"


class ModelFamily(str, Enum):
    NCEP = "ncep"
    ECMWF = "ecmwf"


class DataStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    DELAYED = "delayed"
    MISSING = "missing"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Agreement(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NOT_AVAILABLE = "not_available"


class Subsystem(str, Enum):
    SOUTH = "S"
    SOUTHEAST_CENTRAL_WEST = "SE_CO"
    NORTHEAST = "NE"
    NORTH = "N"


CANONICAL_BASINS: dict[str, str] = dict(CANONICAL_BASIN_NAME_BY_ID)


def _canonical_basin_id(value: str) -> str:
    if value not in CANONICAL_BASINS:
        raise ValueError(f"{value!r} is not a canonical basin_id")
    return value


CanonicalBasinId = Annotated[str, AfterValidator(_canonical_basin_id)]


class Issue(StrictModel):
    date: date
    time: time
    timezone: Literal["America/Sao_Paulo"] = "America/Sao_Paulo"
    cutoff_utc: UTCDateTime


class ForecastWindow(StrictModel):
    id: WindowId
    label: str
    start_utc: UTCDateTime
    end_utc: UTCDateTime
    duration_days: PositiveInt

    @model_validator(mode="after")
    def validate_window_contract(self) -> Self:
        expected_label = {
            WindowId.P1: "1ª pêntada",
            WindowId.P2: "2ª pêntada",
            WindowId.P3: "3ª pêntada",
        }[self.id]
        allowed_durations = (4, 5) if self.id == WindowId.P3 else (5,)
        if self.label != expected_label or self.duration_days not in allowed_durations:
            raise ValueError(
                f"{self.id.value} must use label={expected_label!r} and duration_days="
                f"{', '.join(str(value) for value in allowed_durations)}"
            )
        if self.end_utc - self.start_utc != timedelta(days=self.duration_days):
            raise ValueError("forecast window bounds must exactly match duration_days")
        return self


class ModelRun(StrictModel):
    model: ModelId
    family: ModelFamily
    init_time: UTCDateTime | None
    available_at: UTCDateTime | None
    status: DataStatus
    cycle: Literal["00"] = "00"
    version: str | None = None
    max_lead_h: NonNegativeInt
    expected_members: PositiveInt
    available_members: NonNegativeInt
    expected_steps: NonNegativeInt
    available_steps: NonNegativeInt
    available_variables: list[str] = Field(default_factory=list)
    missing_valid_times: list[UTCDateTime] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_model_contract(self) -> Self:
        expected_family = {
            ModelId.GFS: ModelFamily.NCEP,
            ModelId.GEFS: ModelFamily.NCEP,
            ModelId.EC: ModelFamily.ECMWF,
            ModelId.ECENS: ModelFamily.ECMWF,
        }[self.model]
        if self.family != expected_family:
            raise ValueError(f"{self.model.value} belongs to the {expected_family.value} family")

        max_supported_lead = {
            ModelId.GFS: 336,
            ModelId.GEFS: 336,
            ModelId.EC: 240,
            ModelId.ECENS: 336,
        }[self.model]
        if self.max_lead_h > max_supported_lead:
            raise ValueError(
                f"{self.model.value} max_lead_h cannot exceed {max_supported_lead}; "
                "EC must never be extrapolated beyond 240 h"
            )

        expected_member_count = {
            ModelId.GFS: 1,
            ModelId.GEFS: 31,
            ModelId.EC: 1,
            ModelId.ECENS: 50,
        }[self.model]
        if self.expected_members != expected_member_count:
            raise ValueError(
                f"{self.model.value} expected_members must be {expected_member_count}"
            )
        if self.available_members > self.expected_members:
            raise ValueError("available_members cannot exceed expected_members")
        if self.available_steps > self.expected_steps:
            raise ValueError("available_steps cannot exceed expected_steps")
        if self.status == DataStatus.COMPLETE and (
            self.available_members != self.expected_members
            or self.available_steps != self.expected_steps
        ):
            raise ValueError("a complete model run must contain every expected member and step")
        if self.status in {DataStatus.MISSING, DataStatus.FAILED} and self.available_steps:
            raise ValueError("missing or failed model runs cannot report available steps")
        if self.status not in {DataStatus.MISSING, DataStatus.FAILED} and self.init_time is None:
            raise ValueError("an available model run must identify init_time")
        return self


class ModelRuns(StrictModel):
    gfs: ModelRun
    gefs: ModelRun
    ec: ModelRun
    ecens: ModelRun

    @model_validator(mode="after")
    def validate_slots(self) -> Self:
        for slot, expected in (
            ("gfs", ModelId.GFS),
            ("gefs", ModelId.GEFS),
            ("ec", ModelId.EC),
            ("ecens", ModelId.ECENS),
        ):
            if getattr(self, slot).model != expected:
                raise ValueError(f"model_runs.{slot} must describe {expected.value}")
        available_init_times = {
            run.init_time
            for run in (self.gfs, self.gefs, self.ec, self.ecens)
            if run.status not in {DataStatus.MISSING, DataStatus.FAILED}
        }
        if any(
            init_time is None
            or init_time.hour != 0
            or init_time.minute != 0
            or init_time.second != 0
            for init_time in available_init_times
        ):
            raise ValueError("weather summary model runs must use the 00 UTC cycle")
        if len(available_init_times) > 1:
            raise ValueError("weather summary cannot silently mix different 00 UTC initializations")
        return self


class ObservedDailyRain(StrictModel):
    date: date
    start_utc: UTCDateTime
    end_utc: UTCDateTime
    basin_mean_mm: NonNegativeFloat
    coverage_pct: Percentage
    complete: Literal[True] = True

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if self.end_utc <= self.start_utc:
            raise ValueError("observed daily period end must be after start")
        if self.start_utc.date() != self.date or self.end_utc - self.start_utc != timedelta(days=1):
            raise ValueError("observed daily period must cover its UTC calendar day")
        return self


class ObservedRain(StrictModel):
    status: DataStatus
    data_end_utc: UTCDateTime | None
    rain_24h_mm: NonNegativeFloat | None
    rain_72h_mm: NonNegativeFloat | None
    rain_5d_mm: NonNegativeFloat | None
    rain_10d_mm: NonNegativeFloat | None
    rain_30d_mm: NonNegativeFloat | None
    rain_30d_anomaly_pct: float | None
    event_accumulation_mm: NonNegativeFloat | None
    antecedent_index: NonNegativeFloat | None
    percentile: Percentage | None
    coverage_pct: Percentage | None
    latest_partial_start_utc: UTCDateTime | None = None
    latest_partial_end_utc: UTCDateTime | None = None
    rain_latest_partial_mm: NonNegativeFloat | None = None
    latest_partial_coverage_pct: Percentage | None = None
    recent_daily: list[ObservedDailyRain] = Field(default_factory=list, max_length=10)
    source_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_missing_observation(self) -> Self:
        if self.status == DataStatus.COMPLETE and (
            self.data_end_utc is None or self.coverage_pct is None
        ):
            raise ValueError("complete observed rain requires data_end_utc and coverage_pct")
        partial_values = (
            self.latest_partial_start_utc,
            self.latest_partial_end_utc,
            self.rain_latest_partial_mm,
            self.latest_partial_coverage_pct,
        )
        if any(value is not None for value in partial_values):
            if any(value is None for value in partial_values):
                raise ValueError("latest partial observed period must be supplied atomically")
            if self.latest_partial_end_utc <= self.latest_partial_start_utc:
                raise ValueError("latest partial observed end must be after start")
        if [item.date for item in self.recent_daily] != sorted(
            {item.date for item in self.recent_daily}
        ):
            raise ValueError("recent_daily must contain unique ascending dates")
        return self


class DeterministicPrecipitation(StrictModel):
    raw_basin_mean_mm: NonNegativeFloat
    corrected_basin_mean_mm: NonNegativeFloat | None
    spatial_coverage_pct: Percentage
    valid_steps: PositiveInt
    source_ids: list[str] = Field(default_factory=list)


class EnsembleProbabilities(StrictModel):
    gt_5mm: Probability
    gt_10mm: Probability
    gt_25mm: Probability
    gt_50mm: Probability
    gt_100mm: Probability

    @model_validator(mode="after")
    def validate_monotonic_probabilities(self) -> Self:
        values = (self.gt_5mm, self.gt_10mm, self.gt_25mm, self.gt_50mm, self.gt_100mm)
        if any(left < right for left, right in zip(values, values[1:])):
            raise ValueError("exceedance probabilities must not increase with threshold")
        return self


class EnsemblePrecipitation(StrictModel):
    members_expected: PositiveInt
    members_available: PositiveInt
    p10_mm: NonNegativeFloat
    p25_mm: NonNegativeFloat
    p50_mm: NonNegativeFloat
    p75_mm: NonNegativeFloat
    p90_mm: NonNegativeFloat
    mean_mm: NonNegativeFloat
    spread_mm: NonNegativeFloat
    coefficient_of_variation: NonNegativeFloat | None
    probabilities: EnsembleProbabilities
    source_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_distribution(self) -> Self:
        quantiles = (self.p10_mm, self.p25_mm, self.p50_mm, self.p75_mm, self.p90_mm)
        if any(left > right for left, right in zip(quantiles, quantiles[1:])):
            raise ValueError("ensemble precipitation quantiles must be monotonic")
        if self.members_available > self.members_expected:
            raise ValueError("members_available cannot exceed members_expected")
        return self


class SpatialDistribution(StrictModel):
    basin_mean_mm: NonNegativeFloat
    basin_median_mm: NonNegativeFloat
    basin_p10_mm: NonNegativeFloat
    basin_p90_mm: NonNegativeFloat
    area_gt_5mm_pct: Percentage
    area_gt_10mm_pct: Percentage
    area_gt_25mm_pct: Percentage
    area_gt_50mm_pct: Percentage
    point_max_mm: NonNegativeFloat
    point_max_is_gridpoint: Literal[True] = True

    @model_validator(mode="after")
    def validate_spatial_statistics(self) -> Self:
        if not self.basin_p10_mm <= self.basin_median_mm <= self.basin_p90_mm:
            raise ValueError("spatial p10, median and p90 must be monotonic")
        area_values = (
            self.area_gt_5mm_pct,
            self.area_gt_10mm_pct,
            self.area_gt_25mm_pct,
            self.area_gt_50mm_pct,
        )
        if any(left < right for left, right in zip(area_values, area_values[1:])):
            raise ValueError("area fractions must not increase with precipitation threshold")
        if self.point_max_mm < self.basin_mean_mm:
            raise ValueError("point_max_mm cannot be smaller than basin_mean_mm")
        return self


class ClimatologyWindow(StrictModel):
    climatology_id: str
    mean_mm: NonNegativeFloat
    median_mm: NonNegativeFloat
    p10_mm: NonNegativeFloat
    p25_mm: NonNegativeFloat
    p75_mm: NonNegativeFloat
    p90_mm: NonNegativeFloat
    anomaly_mm: float
    anomaly_pct: float | None
    percentile: Percentage

    @model_validator(mode="after")
    def validate_climatology_quantiles(self) -> Self:
        quantiles = (self.p10_mm, self.p25_mm, self.median_mm, self.p75_mm, self.p90_mm)
        if any(left > right for left, right in zip(quantiles, quantiles[1:])):
            raise ValueError("climatology quantiles must be monotonic")
        return self


class FamilyPrecipitation(StrictModel):
    family: ModelFamily
    ensemble_median_mm: NonNegativeFloat
    calibrated_median_mm: NonNegativeFloat | None
    deterministic_mm: NonNegativeFloat | None
    deterministic_inside_ensemble_p10_p90: bool | None
    confidence: Confidence
    source_ids: list[str] = Field(default_factory=list)


class FamilyComparison(StrictModel):
    ncep: FamilyPrecipitation | None
    ecmwf: FamilyPrecipitation | None
    agreement: Agreement
    difference_mm: NonNegativeFloat | None
    same_signal: bool | None
    same_spatial_pattern: bool | None
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_family_slots(self) -> Self:
        if self.ncep is not None and self.ncep.family != ModelFamily.NCEP:
            raise ValueError("families.ncep must contain the NCEP family summary")
        if self.ecmwf is not None and self.ecmwf.family != ModelFamily.ECMWF:
            raise ValueError("families.ecmwf must contain the ECMWF family summary")
        if self.ncep is None and self.ecmwf is None:
            if self.agreement != Agreement.NOT_AVAILABLE:
                raise ValueError("agreement must be not_available when both families are absent")
        elif self.agreement == Agreement.NOT_AVAILABLE:
            raise ValueError("agreement cannot be not_available when at least one family is present")
        return self


class OpinionPrecipitation(StrictModel):
    range_mm: tuple[NonNegativeFloat, NonNegativeFloat]
    rounded_mm: NonNegativeFloat | None
    confidence: Confidence
    basis: Literal[
        "two_family_consensus",
        "single_family",
        "ensemble_regime",
        "insufficient_for_number",
    ]
    family_weights: dict[ModelFamily, Probability] = Field(default_factory=dict)
    uncertainty_note: str | None

    @model_validator(mode="after")
    def validate_opinion(self) -> Self:
        lower, upper = self.range_mm
        if lower > upper:
            raise ValueError("opinion range_mm must be ordered")
        if self.rounded_mm is not None and not lower <= self.rounded_mm <= upper:
            raise ValueError("rounded_mm must fall inside range_mm")
        if self.family_weights:
            if set(self.family_weights) - {ModelFamily.NCEP, ModelFamily.ECMWF}:
                raise ValueError("only the two independent families can receive weights")
            if abs(sum(self.family_weights.values()) - 1.0) > 1e-6:
                raise ValueError("family_weights must sum to 1 without double-counting models")
        return self


class WindowDataQuality(StrictModel):
    status: DataStatus
    warnings: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    coverage_pct: Percentage | None


class DailyForecastTiming(StrictModel):
    date: date
    gfs_basin_mean_mm: NonNegativeFloat | None
    ec_basin_mean_mm: NonNegativeFloat | None
    deterministic_center_mm: NonNegativeFloat | None
    family_difference_mm: NonNegativeFloat | None
    confidence: Confidence

    @model_validator(mode="after")
    def validate_center(self) -> Self:
        values = [
            value
            for value in (self.gfs_basin_mean_mm, self.ec_basin_mean_mm)
            if value is not None
        ]
        if self.deterministic_center_mm is None and values:
            raise ValueError("daily deterministic center is required when a model is present")
        if self.deterministic_center_mm is not None and not values:
            raise ValueError("daily deterministic center cannot exist without a model")
        if len(values) < 2 and self.family_difference_mm is not None:
            raise ValueError("daily family difference requires both deterministic models")
        return self


class BasinWindowAnalysis(StrictModel):
    gfs: DeterministicPrecipitation | None
    ec: DeterministicPrecipitation | None
    gefs: EnsemblePrecipitation | None
    ecens: EnsemblePrecipitation | None
    spatial: SpatialDistribution
    climatology: ClimatologyWindow | None
    families: FamilyComparison
    opinion: OpinionPrecipitation
    quality: WindowDataQuality
    daily_timing: list[DailyForecastTiming] = Field(default_factory=list, max_length=5)


class BasinWindows(StrictModel):
    p1: BasinWindowAnalysis
    p2: BasinWindowAnalysis
    p3: BasinWindowAnalysis

    @model_validator(mode="after")
    def forbid_ec_extrapolation(self) -> Self:
        if self.p3.ec is not None:
            raise ValueError(
                "EC deterministic precipitation is unavailable after 240 h and must be null in p3"
            )
        if self.p3.families.ecmwf is not None and self.p3.families.ecmwf.deterministic_mm is not None:
            raise ValueError("p3 ECMWF family cannot contain an extrapolated EC deterministic value")
        return self


class BasinAnalysis(StrictModel):
    basin_id: CanonicalBasinId
    name: str
    editorial_reference: str
    aliases: list[str] = Field(default_factory=list)
    subsystem: Subsystem
    geometry_version: str
    geometry_status: DataStatus
    geometry_source_name: str | None
    geometry_limitations: list[str] = Field(default_factory=list)
    area_km2: PositiveFloat | None
    observed: ObservedRain | None
    windows: BasinWindows | None

    @model_validator(mode="after")
    def validate_canonical_name(self) -> Self:
        expected = CANONICAL_BASINS[self.basin_id]
        if self.name != expected:
            raise ValueError(f"{self.basin_id} canonical name must be {expected!r}")
        expected_reference = format_basin_editorial_reference(self.basin_id)
        if self.editorial_reference != expected_reference:
            raise ValueError(
                f"{self.basin_id} editorial reference must be {expected_reference!r}"
            )
        if any(alias.casefold() == self.name.casefold() for alias in self.aliases):
            raise ValueError("aliases must not repeat the canonical name")
        if self.geometry_status == DataStatus.COMPLETE:
            if self.area_km2 is None or self.observed is None or self.windows is None:
                raise ValueError(
                    "a basin with complete geometry requires area, observed rain and all windows"
                )
        else:
            if not self.geometry_limitations:
                raise ValueError(
                    "a non-complete canonical basin geometry requires an explicit limitation"
                )
            if self.geometry_status in {DataStatus.MISSING, DataStatus.FAILED} and (
                self.area_km2 is not None or self.windows is not None
            ):
                raise ValueError(
                    "missing geometry cannot carry invented area-weighted basin analyses"
                )
        return self


class TimelineEvent(StrictModel):
    start_utc: UTCDateTime
    end_utc: UTCDateTime
    description: str
    timing_confidence: Confidence

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.end_utc <= self.start_utc:
            raise ValueError("timeline event end_utc must be after start_utc")
        return self


class AtmosphericLevelDiagnostic(StrictModel):
    summary: str
    signals: list[str]
    supported_by_families: list[ModelFamily]
    confidence: Confidence
    source_fields: list[str]


class StructuredSynopticFeature(StrictModel):
    feature_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{5,119}$")
    feature_type: Literal[
        "cold_front",
        "post_frontal_high",
        "high_pressure",
        "deepening_low",
        "low_pressure",
        "trough",
        "ridge",
        "moisture_convergence",
        "zcas",
    ]
    canonical_name: str
    lifecycle: Literal[
        "antecedent",
        "establishing",
        "dominant",
        "weakening",
        "developing",
        "crossing",
        "moving_offshore",
    ]
    start_utc: UTCDateTime
    end_utc: UTCDateTime
    peak_utc: UTCDateTime | None = None
    regions: list[str] = Field(min_length=1)
    affected_basin_ids: list[CanonicalBasinId] = Field(default_factory=list)
    effects: list[str] = Field(default_factory=list)
    evidence_fields: list[str] = Field(min_length=1)
    supported_by_families: list[ModelFamily] = Field(min_length=1)
    confidence: Confidence
    continues_into: WindowId | None = None
    description: str
    source_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_feature_bounds(self) -> Self:
        if self.end_utc <= self.start_utc:
            raise ValueError("structured feature end_utc must be after start_utc")
        if self.peak_utc is not None and not (
            self.start_utc <= self.peak_utc <= self.end_utc
        ):
            raise ValueError("structured feature peak_utc must stay inside its bounds")
        if len(set(self.regions)) != len(self.regions):
            raise ValueError("structured feature regions must be unique")
        if len(set(self.affected_basin_ids)) != len(self.affected_basin_ids):
            raise ValueError("structured feature basin IDs must be unique")
        if len(set(self.supported_by_families)) != len(self.supported_by_families):
            raise ValueError("structured feature families must be unique")
        return self


class RegionalTemperatureDiagnostic(StrictModel):
    region_id: str
    region_label: str
    start_regime: Literal["below_average", "near_average", "above_average"]
    end_regime: Literal["below_average", "near_average", "above_average"]
    trend: Literal["warming", "cooling", "stable"]
    pattern_change: bool
    start_anomaly_degc: float
    end_anomaly_degc: float
    change_degc: float
    climatology_id: str
    supported_by_families: list[ModelFamily] = Field(min_length=1)
    confidence: Confidence
    source_ids: list[str] = Field(default_factory=list)


class SynopticWindowDiagnostic(StrictModel):
    dominant_pattern: str
    timeline: list[TimelineEvent]
    surface: AtmosphericLevelDiagnostic
    low_levels_850_hpa: AtmosphericLevelDiagnostic
    mid_levels_500_hpa: AtmosphericLevelDiagnostic
    upper_levels_200_250_hpa: AtmosphericLevelDiagnostic
    supported_features: list[str]
    family_agreement: Agreement
    uncertainty: str
    structured_features: list[StructuredSynopticFeature] = Field(default_factory=list)
    regional_temperature: list[RegionalTemperatureDiagnostic] = Field(default_factory=list)


class SynopticDiagnostics(StrictModel):
    p1: SynopticWindowDiagnostic
    p2: SynopticWindowDiagnostic
    p3: SynopticWindowDiagnostic


class EnergySignal(StrictModel):
    signal_id: str
    window_id: WindowId
    category: Literal["wind", "temperature", "solar", "severe_weather"]
    material: Literal[True] = True
    areas_or_assets: list[str]
    meteorological_basis: str
    operational_interpretation: str
    value_min: float | None = None
    value_max: float | None = None
    unit: str | None = None
    confidence: Confidence
    source_ids: list[str]
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_numeric_range(self) -> Self:
        if (self.value_min is None) != (self.value_max is None):
            raise ValueError("value_min and value_max must be supplied together")
        if self.value_min is not None and self.value_min > self.value_max:
            raise ValueError("energy signal value_min cannot exceed value_max")
        if self.value_min is not None and not self.unit:
            raise ValueError("quantified energy signals require a unit")
        return self


class EnergySignals(StrictModel):
    wind: list[EnergySignal] = Field(default_factory=list)
    temperature: list[EnergySignal] = Field(default_factory=list)
    solar: list[EnergySignal] = Field(default_factory=list)
    severe_weather: list[EnergySignal] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_categories(self) -> Self:
        for category in ("wind", "temperature", "solar", "severe_weather"):
            if any(signal.category != category for signal in getattr(self, category)):
                raise ValueError(f"energy_signals.{category} contains a different category")
        return self


class SourceManifestEntry(StrictModel):
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,79}$")
    provider: Literal["NOAA", "ECMWF", "GPM_IMERG", "MERGE_INPE", "OpenAI"]
    category: Literal[
        "forecast",
        "observation",
        "climatology",
        "generation",
    ]
    url: str | None
    collected_at: UTCDateTime
    data_time: UTCDateTime | None
    status: DataStatus
    latency_minutes: NonNegativeFloat | None
    checksum_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    license_or_terms: str | None
    variables: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class MaterialWindowChange(StrictModel):
    window_id: WindowId
    description: str
    affected_basin_ids: list[CanonicalBasinId] = Field(default_factory=list)
    direction: Literal["wetter", "drier", "timing", "spatial_shift", "other"]
    confidence: Confidence


class MaterialChange(StrictModel):
    previous_issue_date: date
    changes: list[MaterialWindowChange] = Field(min_length=1)


class RoundingRules(StrictModel):
    up_to_20_mm_step: Literal[5] = 5
    from_20_to_100_low_spread_step: Literal[5] = 5
    from_20_to_100_high_spread_step: Literal[10] = 10
    above_100_mm_min_step: Literal[10] = 10
    dry_signal_threshold_mm: Literal[5] = 5
    prefer_iqr_if_width_at_least_mm: NonNegativeFloat = 20


class QualityManifest(StrictModel):
    generation_allowed: bool
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)
    delayed_sources: list[str] = Field(default_factory=list)
    incomplete_sources: list[str] = Field(default_factory=list)
    climatology_id: str | None
    basin_geometry_version: str
    summary_basin_scope_id: str
    summary_basin_scope_version: str
    summary_basin_ids: list[CanonicalBasinId] = Field(min_length=1)
    observed_coverage_pct: Percentage | None
    forecast_coverage_pct: Percentage
    checks_passed: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_generation_gate(self) -> Self:
        if self.blockers and self.generation_allowed:
            raise ValueError("generation_allowed must be false while blockers exist")
        if not self.blockers and not self.generation_allowed:
            raise ValueError("generation_allowed=false requires at least one explicit blocker")
        if not self.summary_basin_scope_id or not self.summary_basin_scope_version:
            raise ValueError("summary basin scope must have id and version")
        if not self.summary_basin_ids or len(set(self.summary_basin_ids)) != len(
            self.summary_basin_ids
        ):
            raise ValueError("summary basin scope must contain unique basin IDs")
        return self


class PacketAudit(StrictModel):
    packet_id: str = Field(pattern=r"^[a-zA-Z0-9._:-]{8,120}$")
    created_at: UTCDateTime
    pipeline_version: str
    field_config_version: str
    code_revision: str | None
    input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    notes: list[str] = Field(default_factory=list)


class AnalysisPacket(StrictModel):
    """Current JSON-serializable hand-off to the editorial model."""

    schema_version: Literal[2]
    issue: Issue
    forecast_windows: list[ForecastWindow] = Field(min_length=3, max_length=3)
    model_runs: ModelRuns
    # The packet may cover a product-specific subset of the canonical catalog.
    # Its exact ordered membership is enforced against quality.summary_basin_ids
    # in the model validator below.
    basins: list[BasinAnalysis] = Field(min_length=1)
    synoptic_diagnostics: SynopticDiagnostics
    energy_signals: EnergySignals
    material_change: MaterialChange | None
    rounding_mm: RoundingRules
    source_manifest: list[SourceManifestEntry] = Field(min_length=1)
    quality: QualityManifest
    audit: PacketAudit

    @model_validator(mode="after")
    def validate_packet_contract(self) -> Self:
        expected_ids = [WindowId.P1, WindowId.P2, WindowId.P3]
        if [window.id for window in self.forecast_windows] != expected_ids:
            raise ValueError("forecast_windows must be ordered exactly as p1, p2, p3")
        expected_durations = (5, 5, 4)
        actual_durations = tuple(window.duration_days for window in self.forecast_windows)
        if actual_durations != expected_durations:
            expected_label = "+".join(str(value) for value in expected_durations)
            raise ValueError(
                f"analysis packets must use {expected_label}-day forecast windows"
            )
        for left, right in zip(self.forecast_windows, self.forecast_windows[1:]):
            if left.end_utc != right.start_utc:
                raise ValueError("forecast windows must be contiguous and non-overlapping")

        basin_ids = [basin.basin_id for basin in self.basins]
        if len(set(basin_ids)) != len(basin_ids):
            raise ValueError("basin_id values must be unique")
        expected_basin_ids = [str(value) for value in self.quality.summary_basin_ids]
        if basin_ids != expected_basin_ids:
            missing = sorted(set(expected_basin_ids) - set(basin_ids))
            extra = sorted(set(basin_ids) - set(expected_basin_ids))
            raise ValueError(
                "packet basins must match the declared summary scope in order; "
                f"missing={missing}, extra={extra}"
            )

        for window_id in expected_ids:
            window = self.window(window_id)
            diagnostic = getattr(self.synoptic_diagnostics, window_id.value)
            events = sorted(diagnostic.timeline, key=lambda item: (item.start_utc, item.end_utc))
            for event in events:
                if event.start_utc < window.start_utc or event.end_utc > window.end_utc:
                    raise ValueError(
                        f"synoptic_diagnostics.{window_id.value} timeline must stay inside its forecast window"
                    )
            if any(current.start_utc < previous.end_utc for previous, current in zip(events, events[1:])):
                raise ValueError(
                    f"synoptic_diagnostics.{window_id.value} timeline events cannot overlap"
                )
            for feature in diagnostic.structured_features:
                if feature.start_utc < window.start_utc or feature.end_utc > window.end_utc:
                    raise ValueError(
                        f"synoptic_diagnostics.{window_id.value} structured features "
                        "must stay inside their forecast window"
                    )
                if feature.continues_into is not None:
                    expected_next = {
                        WindowId.P1: WindowId.P2,
                        WindowId.P2: WindowId.P3,
                        WindowId.P3: None,
                    }[window_id]
                    if feature.continues_into != expected_next:
                        raise ValueError(
                            f"synoptic_diagnostics.{window_id.value} feature cannot "
                            f"continue into {feature.continues_into.value}"
                        )

        source_ids = [source.source_id for source in self.source_manifest]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_manifest source_id values must be unique")
        known_sources = set(source_ids)
        referenced_sources: set[str] = set()
        for run in (self.model_runs.gfs, self.model_runs.gefs, self.model_runs.ec, self.model_runs.ecens):
            referenced_sources.update(run.source_ids)
        for basin in self.basins:
            if basin.observed is not None:
                referenced_sources.update(basin.observed.source_ids)
            if basin.windows is None:
                continue
            for window_id, basin_window in (
                (WindowId.P1, basin.windows.p1),
                (WindowId.P2, basin.windows.p2),
                (WindowId.P3, basin.windows.p3),
            ):
                forecast_window = self.window(window_id)
                expected_daily_dates = [
                    forecast_window.start_utc.date() + timedelta(days=offset)
                    for offset in range(1, forecast_window.duration_days + 1)
                ]
                if basin_window.daily_timing and [
                    item.date for item in basin_window.daily_timing
                ] != expected_daily_dates:
                    raise ValueError(
                        f"{basin.basin_id}/{window_id.value} daily_timing must cover the full window"
                    )
                window = basin_window
                for model_value in (window.gfs, window.gefs, window.ec, window.ecens):
                    if model_value is not None:
                        referenced_sources.update(model_value.source_ids)
                for family in (window.families.ncep, window.families.ecmwf):
                    if family is not None:
                        referenced_sources.update(family.source_ids)
        for signals in (
            self.energy_signals.wind,
            self.energy_signals.temperature,
            self.energy_signals.solar,
            self.energy_signals.severe_weather,
        ):
            for signal in signals:
                referenced_sources.update(signal.source_ids)
        for diagnostic in (
            self.synoptic_diagnostics.p1,
            self.synoptic_diagnostics.p2,
            self.synoptic_diagnostics.p3,
        ):
            for feature in diagnostic.structured_features:
                referenced_sources.update(feature.source_ids)
            for temperature in diagnostic.regional_temperature:
                referenced_sources.update(temperature.source_ids)
        unknown_sources = sorted(referenced_sources - known_sources)
        if unknown_sources:
            raise ValueError(f"packet references source IDs absent from source_manifest: {unknown_sources}")

        geometry_versions = {basin.geometry_version for basin in self.basins}
        if geometry_versions != {self.quality.basin_geometry_version}:
            raise ValueError("all basin geometries must match quality.basin_geometry_version")
        climatology_ids = {
            window.climatology.climatology_id
            for basin in self.basins
            if basin.windows is not None
            for window in (basin.windows.p1, basin.windows.p2, basin.windows.p3)
            if window.climatology is not None
        }
        expected_climatology_ids = (
            {self.quality.climatology_id} if self.quality.climatology_id is not None else set()
        )
        if climatology_ids != expected_climatology_ids:
            raise ValueError("all climatology records must match quality.climatology_id")
        return self

    def window(self, window_id: WindowId | str) -> ForecastWindow:
        wanted = WindowId(window_id)
        return next(window for window in self.forecast_windows if window.id == wanted)

    def to_json(self, *, indent: int | None = None) -> str:
        return self.model_dump_json(indent=indent, exclude_none=False)


def analysis_packet_json_schema() -> dict[str, Any]:
    """Return the canonical JSON Schema for the current packet contract."""

    schema = AnalysisPacket.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = ANALYSIS_PACKET_JSON_SCHEMA_ID
    schema["title"] = "Open Resumo Meteo weather summary analysis packet v2"
    schema["$comment"] = "Canonical standalone contract for the current open pipeline."
    return schema


__all__ = [
    "ANALYSIS_PACKET_JSON_SCHEMA_ID",
    "ANALYSIS_PACKET_SCHEMA_VERSION",
    "AnalysisPacket",
    "Agreement",
    "CANONICAL_BASINS",
    "Confidence",
    "DataStatus",
    "ModelFamily",
    "ModelId",
    "WindowId",
    "analysis_packet_json_schema",
]
