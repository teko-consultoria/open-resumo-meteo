"""Two-family precipitation consensus without model double counting."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from .constants import FAMILY_MODELS, ModelFamily, normalize_model_id
from .ensemble import EnsembleStatistics


@dataclass(frozen=True, slots=True)
class ModelEstimate:
    central_mm: float
    lower_mm: float | None = None
    upper_mm: float | None = None

    def __post_init__(self) -> None:
        for field_name in ("central_mm", "lower_mm", "upper_mm"):
            value = getattr(self, field_name)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{field_name} deve ser finito e nao negativo")
        if self.lower_mm is not None and self.lower_mm > self.central_mm:
            raise ValueError("lower_mm nao pode superar central_mm")
        if self.upper_mm is not None and self.upper_mm < self.central_mm:
            raise ValueError("upper_mm nao pode ser inferior a central_mm")


@dataclass(frozen=True, slots=True)
class FamilyEstimate:
    family_id: str
    central_mm: float
    representative_model: str
    available_models: tuple[str, ...]
    deterministic_within_ensemble: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "family_id": self.family_id,
            "central_mm": self.central_mm,
            "representative_model": self.representative_model,
            "available_models": list(self.available_models),
            "deterministic_within_ensemble": self.deterministic_within_ensemble,
        }


@dataclass(frozen=True, slots=True)
class TwoFamilyConsensus:
    opinion_mm: float
    family_estimates: tuple[FamilyEstimate, ...]
    normalized_family_weights: Mapping[str, float]
    agreement: str
    quality_flags: tuple[str, ...] = ()

    @property
    def independent_family_count(self) -> int:
        return len(self.family_estimates)

    @property
    def contributing_models(self) -> tuple[str, ...]:
        return tuple(estimate.representative_model for estimate in self.family_estimates)

    def to_dict(self) -> dict[str, object]:
        return {
            "opinion_mm": self.opinion_mm,
            "independent_family_count": self.independent_family_count,
            "family_estimates": [estimate.to_dict() for estimate in self.family_estimates],
            "normalized_family_weights": dict(self.normalized_family_weights),
            "agreement": self.agreement,
            "quality_flags": list(self.quality_flags),
        }


EstimateInput = float | int | ModelEstimate | EnsembleStatistics | Mapping[str, object] | None


def _model_estimate(value: EstimateInput) -> ModelEstimate | None:
    if value is None:
        return None
    if isinstance(value, ModelEstimate):
        return value
    if isinstance(value, EnsembleStatistics):
        return ModelEstimate(value.p50_mm, value.p25_mm, value.p75_mm)
    if isinstance(value, bool):
        raise TypeError("Estimativa de modelo nao pode ser booleana")
    if isinstance(value, (int, float)):
        return ModelEstimate(float(value))
    if isinstance(value, Mapping):
        central = next(
            (
                value[key]
                for key in ("p50_mm", "median_mm", "central_mm", "value_mm", "raw_mm")
                if key in value and value[key] is not None
            ),
            None,
        )
        if central is None:
            return None
        lower = value.get("p25_mm", value.get("lower_mm"))
        upper = value.get("p75_mm", value.get("upper_mm"))
        return ModelEstimate(
            central_mm=float(central),
            lower_mm=float(lower) if lower is not None else None,
            upper_mm=float(upper) if upper is not None else None,
        )
    raise TypeError(f"Tipo de estimativa nao suportado: {type(value).__name__}")


def _agreement(values: tuple[float, ...]) -> str:
    if len(values) < 2:
        return "single_family"
    difference = abs(values[0] - values[1])
    typical = (values[0] + values[1]) / 2
    if difference <= max(5.0, typical * 0.20):
        return "high"
    if difference <= max(15.0, typical * 0.50):
        return "medium"
    return "low"


def compute_two_family_consensus(
    model_values: Mapping[str, EstimateInput],
    *,
    family_weights: Mapping[str, float] | None = None,
) -> TwoFamilyConsensus:
    """Consolidate one representative per independent family.

    The ensemble median represents its family whenever available.  The
    deterministic run is retained only to check whether it lies inside the
    ensemble interquartile envelope.  If the ensemble is absent, its
    deterministic sibling is an explicitly degraded fallback.
    """

    canonical_values: dict[str, ModelEstimate | None] = {}
    for raw_model, raw_value in model_values.items():
        model = normalize_model_id(raw_model).value
        if model in canonical_values:
            raise ValueError(f"Modelo duplicado apos normalizacao: {raw_model}")
        canonical_values[model] = _model_estimate(raw_value)

    supplied_weights = dict(family_weights or {})
    unknown_families = set(supplied_weights) - {family.value for family in ModelFamily}
    if unknown_families:
        raise ValueError(f"Familias desconhecidas em family_weights: {sorted(unknown_families)}")

    estimates: list[FamilyEstimate] = []
    flags: list[str] = []
    raw_weights: dict[str, float] = {}

    for family in ModelFamily:
        deterministic_model, ensemble_model = FAMILY_MODELS[family.value]
        deterministic = canonical_values.get(deterministic_model)
        ensemble = canonical_values.get(ensemble_model)
        available_models = tuple(
            model
            for model, estimate in (
                (deterministic_model, deterministic),
                (ensemble_model, ensemble),
            )
            if estimate is not None
        )
        if ensemble is not None:
            representative = ensemble
            representative_model = ensemble_model
            within: bool | None = None
            if deterministic is not None and ensemble.lower_mm is not None and ensemble.upper_mm is not None:
                within = ensemble.lower_mm <= deterministic.central_mm <= ensemble.upper_mm
                if not within:
                    flags.append(f"{deterministic_model}_outside_{ensemble_model}_iqr")
        elif deterministic is not None:
            representative = deterministic
            representative_model = deterministic_model
            within = None
            flags.append(f"{family.value}_ensemble_missing")
        else:
            continue

        weight = float(supplied_weights.get(family.value, 1.0))
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError(f"Peso da familia {family.value} deve ser finito e positivo")
        raw_weights[family.value] = weight
        estimates.append(
            FamilyEstimate(
                family_id=family.value,
                central_mm=representative.central_mm,
                representative_model=representative_model,
                available_models=available_models,
                deterministic_within_ensemble=within,
            )
        )

    if not estimates:
        raise ValueError("Consenso requer ao menos uma familia com dados")
    if len(estimates) < 2:
        flags.append("single_family_only")

    total_weight = sum(raw_weights[estimate.family_id] for estimate in estimates)
    normalized_weights = {
        estimate.family_id: raw_weights[estimate.family_id] / total_weight
        for estimate in estimates
    }
    opinion = sum(
        estimate.central_mm * normalized_weights[estimate.family_id]
        for estimate in estimates
    )
    return TwoFamilyConsensus(
        opinion_mm=opinion,
        family_estimates=tuple(estimates),
        normalized_family_weights=normalized_weights,
        agreement=_agreement(tuple(estimate.central_mm for estimate in estimates)),
        quality_flags=tuple(flags),
    )
