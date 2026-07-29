"""Canonical model identifiers and independence rules for the weather summary.

This module is intentionally small and dependency free.  Model aliases are not
accepted: accepting an apparently harmless alias (for example ``ifs``) would
make it possible to introduce an unapproved forecast source without it being
visible in the analysis manifest.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class ForecastModel(StrEnum):
    GFS = "gfs"
    GEFS = "gefs"
    EC = "ec"
    ECENS = "ecens"


class ModelFamily(StrEnum):
    NCEP = "ncep"
    ECMWF = "ecmwf"


@dataclass(frozen=True, slots=True)
class ModelSpecification:
    model: ForecastModel
    family: ModelFamily
    is_ensemble: bool
    maximum_lead_hours: int
    expected_members: int | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["model"] = self.model.value
        payload["family"] = self.family.value
        return payload


MODEL_SPECIFICATIONS: dict[ForecastModel, ModelSpecification] = {
    ForecastModel.GFS: ModelSpecification(
        model=ForecastModel.GFS,
        family=ModelFamily.NCEP,
        is_ensemble=False,
        maximum_lead_hours=384,
    ),
    ForecastModel.GEFS: ModelSpecification(
        model=ForecastModel.GEFS,
        family=ModelFamily.NCEP,
        is_ensemble=True,
        maximum_lead_hours=384,
        expected_members=31,
    ),
    ForecastModel.EC: ModelSpecification(
        model=ForecastModel.EC,
        family=ModelFamily.ECMWF,
        is_ensemble=False,
        maximum_lead_hours=240,
    ),
    ForecastModel.ECENS: ModelSpecification(
        model=ForecastModel.ECENS,
        family=ModelFamily.ECMWF,
        is_ensemble=True,
        maximum_lead_hours=336,
        expected_members=50,
    ),
}

AUTHORIZED_MODEL_IDS: tuple[str, ...] = tuple(model.value for model in ForecastModel)
AUTHORIZED_MODELS = frozenset(AUTHORIZED_MODEL_IDS)

MODEL_FAMILY_BY_MODEL: dict[str, str] = {
    model.value: specification.family.value
    for model, specification in MODEL_SPECIFICATIONS.items()
}
FAMILY_MODELS: dict[str, tuple[str, str]] = {
    ModelFamily.NCEP.value: (ForecastModel.GFS.value, ForecastModel.GEFS.value),
    ModelFamily.ECMWF.value: (ForecastModel.EC.value, ForecastModel.ECENS.value),
}
# The internal daily product is intentionally pinned to the common 00 UTC
# cycle.  Other cycles may continue to exist in the generic public-weather
# ingestion pipeline, but are not eligible for this editorial product.
EDITORIAL_CYCLE_UTC = "00"


def normalize_model_id(value: str | ForecastModel) -> ForecastModel:
    """Return the canonical model enum, rejecting all non-authorized aliases."""

    if isinstance(value, ForecastModel):
        return value
    if not isinstance(value, str):
        raise TypeError("model_id deve ser texto ou ForecastModel")
    normalized = value.strip().lower()
    try:
        return ForecastModel(normalized)
    except ValueError as exc:
        allowed = ", ".join(AUTHORIZED_MODEL_IDS)
        raise ValueError(f"Modelo nao autorizado: {value!r}. Permitidos: {allowed}") from exc


def model_specification(value: str | ForecastModel) -> ModelSpecification:
    return MODEL_SPECIFICATIONS[normalize_model_id(value)]


def model_family(value: str | ForecastModel) -> ModelFamily:
    return model_specification(value).family


def require_editorial_cycle(value: str | int) -> str:
    if isinstance(value, bool):
        raise TypeError("cycle deve ser string ou inteiro")
    if isinstance(value, int):
        normalized = f"{value:02d}"
    elif isinstance(value, str):
        normalized = value.strip().lower().removesuffix("z").zfill(2)
    else:
        raise TypeError("cycle deve ser string ou inteiro")
    if normalized != EDITORIAL_CYCLE_UTC:
        raise ValueError("O resumo meteorologico diario aceita exclusivamente a rodada 00 UTC")
    return EDITORIAL_CYCLE_UTC
