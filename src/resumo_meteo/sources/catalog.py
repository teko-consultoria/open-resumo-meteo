"""Authoritative catalogue for the only forecast sources accepted by the product."""

from __future__ import annotations

from dataclasses import dataclass

EDITORIAL_CYCLE = "00"
# The product uses 5/5/4-day windows: D0–D+5, D+5–D+10 and D+10–D+14.
# EC ends at D+10; GFS, GEFS and ECENS supply the 240–336 h final window.
WEATHER_SUMMARY_HORIZON_HOURS = 336

GFS_PRODUCT_SLUG = "gfs-precipitacao-brasil"
GEFS_PRODUCT_SLUG = "gefs-precipitacao-brasil"
EC_PRODUCT_SLUG = "ec-precipitacao-brasil"
ECENS_PRODUCT_SLUG = "ecens-precipitacao-brasil"

GEFS_CONTROL_MEMBER_ID = "gec00"
GEFS_PERTURBED_MEMBER_IDS = tuple(f"gep{number:02d}" for number in range(1, 31))
GEFS_MEMBER_IDS = (GEFS_CONTROL_MEMBER_ID, *GEFS_PERTURBED_MEMBER_IDS)
ECENS_MEMBER_IDS = tuple(str(number) for number in range(1, 51))


@dataclass(frozen=True, slots=True)
class ModelSourceSpec:
    model: str
    family: str
    source: str
    product_slug: str
    label: str
    provider_model: str
    max_forecast_hour: int
    ensemble: bool
    member_ids: tuple[str, ...] = ()
    nominal_resolution: str = "0.25 degree"

    def daily_end_hours(self, *, horizon_hours: int | None = None) -> tuple[int, ...]:
        requested = self.max_forecast_hour if horizon_hours is None else int(horizon_hours)
        if requested < 24:
            return ()
        return tuple(range(24, min(requested, self.max_forecast_hour) + 1, 24))


MODEL_SPECS = {
    "gfs": ModelSourceSpec(
        model="gfs",
        family="ncep",
        source="noaa",
        product_slug=GFS_PRODUCT_SLUG,
        label="GFS",
        provider_model="gfs",
        max_forecast_hour=384,
        ensemble=False,
    ),
    "gefs": ModelSourceSpec(
        model="gefs",
        family="ncep",
        source="noaa",
        product_slug=GEFS_PRODUCT_SLUG,
        label="GEFS",
        provider_model="geavg",
        max_forecast_hour=384,
        ensemble=True,
        member_ids=GEFS_MEMBER_IDS,
        nominal_resolution="0.25 degree through 240h; 0.5 degree thereafter",
    ),
    "ec": ModelSourceSpec(
        model="ec",
        family="ecmwf",
        source="ecmwf",
        product_slug=EC_PRODUCT_SLUG,
        label="EC",
        provider_model="ifs/oper/fc",
        max_forecast_hour=240,
        ensemble=False,
    ),
    "ecens": ModelSourceSpec(
        model="ecens",
        family="ecmwf",
        source="ecmwf",
        product_slug=ECENS_PRODUCT_SLUG,
        label="ECENS",
        provider_model="ifs/enfo/ef",
        max_forecast_hour=336,
        ensemble=True,
        member_ids=ECENS_MEMBER_IDS,
    ),
}


def get_model_spec(model: str) -> ModelSourceSpec:
    key = str(model or "").strip().lower()
    try:
        return MODEL_SPECS[key]
    except KeyError as exc:
        raise ValueError(
            f"Modelo nao autorizado: {model!r}; use GFS, GEFS, EC ou ECENS"
        ) from exc


def normalize_gefs_member_id(member_id: str | int) -> str:
    if isinstance(member_id, int) and not isinstance(member_id, bool):
        candidate = GEFS_CONTROL_MEMBER_ID if member_id == 0 else f"gep{member_id:02d}"
    else:
        candidate = str(member_id or "").strip().lower()
        if candidate in {"control", "c00"}:
            candidate = GEFS_CONTROL_MEMBER_ID
        elif len(candidate) == 3 and candidate.startswith("p") and candidate[1:].isdigit():
            candidate = f"ge{candidate}"
    if candidate not in GEFS_MEMBER_IDS:
        raise ValueError(
            f"Membro GEFS invalido: {member_id!r}; esperado gec00 ou gep01..gep30"
        )
    return candidate


__all__ = [
    "ECENS_MEMBER_IDS",
    "ECENS_PRODUCT_SLUG",
    "EC_PRODUCT_SLUG",
    "EDITORIAL_CYCLE",
    "GEFS_MEMBER_IDS",
    "GEFS_PRODUCT_SLUG",
    "GFS_PRODUCT_SLUG",
    "MODEL_SPECS",
    "ModelSourceSpec",
    "WEATHER_SUMMARY_HORIZON_HOURS",
    "get_model_spec",
    "normalize_gefs_member_id",
]
