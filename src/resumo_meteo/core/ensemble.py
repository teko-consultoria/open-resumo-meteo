"""Pure ensemble precipitation statistics."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

DEFAULT_THRESHOLDS_MM: tuple[float, ...] = (5.0, 10.0, 25.0, 50.0, 100.0)


def _finite_members(values: Iterable[float | None]) -> tuple[tuple[float, ...], int]:
    valid: list[float] = []
    invalid_count = 0
    for value in values:
        if value is None:
            invalid_count += 1
            continue
        if isinstance(value, bool):
            invalid_count += 1
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            invalid_count += 1
            continue
        if not math.isfinite(numeric):
            invalid_count += 1
            continue
        valid.append(numeric)
    return tuple(valid), invalid_count


def quantile(values: Iterable[float], probability: float) -> float:
    """R-7/NumPy-linear quantile without requiring NumPy."""

    if not 0 <= probability <= 1:
        raise ValueError("probability deve estar entre 0 e 1")
    valid, invalid_count = _finite_members(values)
    if invalid_count:
        raise ValueError("quantile recebeu valor ausente ou nao finito")
    if not valid:
        raise ValueError("quantile requer pelo menos um valor")
    ordered = sorted(valid)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def probability_exceeding(values: Iterable[float], threshold_mm: float) -> float:
    valid, invalid_count = _finite_members(values)
    if invalid_count:
        raise ValueError("probability_exceeding recebeu valor ausente ou nao finito")
    if not valid:
        raise ValueError("probability_exceeding requer pelo menos um membro")
    return sum(value > threshold_mm for value in valid) / len(valid)


@dataclass(frozen=True, slots=True)
class EnsembleStatistics:
    valid_member_count: int
    expected_member_count: int | None
    invalid_member_count: int
    completeness_fraction: float | None
    mean_mm: float
    p10_mm: float
    p25_mm: float
    p50_mm: float
    p75_mm: float
    p90_mm: float
    standard_deviation_mm: float
    spread_p90_p10_mm: float
    interquartile_range_mm: float
    coefficient_of_variation: float | None
    probabilities_gt_mm: Mapping[str, float]
    quality_flags: tuple[str, ...] = ()

    @property
    def spread_mm(self) -> float:
        return self.spread_p90_p10_mm

    def probability_gt(self, threshold_mm: float) -> float:
        key = _threshold_key(threshold_mm)
        try:
            return self.probabilities_gt_mm[key]
        except KeyError as exc:
            raise KeyError(f"Limiar nao calculado: {threshold_mm:g} mm") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "valid_member_count": self.valid_member_count,
            "expected_member_count": self.expected_member_count,
            "invalid_member_count": self.invalid_member_count,
            "completeness_fraction": self.completeness_fraction,
            "mean_mm": self.mean_mm,
            "p10_mm": self.p10_mm,
            "p25_mm": self.p25_mm,
            "p50_mm": self.p50_mm,
            "p75_mm": self.p75_mm,
            "p90_mm": self.p90_mm,
            "standard_deviation_mm": self.standard_deviation_mm,
            "spread_p90_p10_mm": self.spread_p90_p10_mm,
            "interquartile_range_mm": self.interquartile_range_mm,
            "coefficient_of_variation": self.coefficient_of_variation,
            "probabilities_gt_mm": dict(self.probabilities_gt_mm),
            "quality_flags": list(self.quality_flags),
        }


def _threshold_key(value: float) -> str:
    numeric = float(value)
    token = str(int(numeric)) if numeric.is_integer() else format(numeric, "g")
    return f"gt_{token}_mm"


def ensemble_statistics(
    member_values_mm: Iterable[float | None],
    *,
    expected_member_count: int | None = None,
    thresholds_mm: Iterable[float] = DEFAULT_THRESHOLDS_MM,
) -> EnsembleStatistics:
    """Calculate member-wise distribution, probabilities and completeness."""

    raw_values = tuple(member_values_mm)
    valid, invalid_count = _finite_members(raw_values)
    if not valid:
        raise ValueError("Estatisticas ensemble requerem pelo menos um membro valido")
    if any(value < 0 for value in valid):
        raise ValueError("Acumulados dos membros nao podem ser negativos")
    if expected_member_count is not None and expected_member_count <= 0:
        raise ValueError("expected_member_count deve ser positivo")
    if expected_member_count is not None and len(raw_values) > expected_member_count:
        raise ValueError("Foram recebidos mais membros que o total esperado")

    thresholds = tuple(float(value) for value in thresholds_mm)
    if len(set(thresholds)) != len(thresholds):
        raise ValueError("thresholds_mm contem valores duplicados")
    if any(not math.isfinite(value) or value < 0 for value in thresholds):
        raise ValueError("thresholds_mm deve conter valores finitos e nao negativos")

    p10 = quantile(valid, 0.10)
    p25 = quantile(valid, 0.25)
    p50 = quantile(valid, 0.50)
    p75 = quantile(valid, 0.75)
    p90 = quantile(valid, 0.90)
    mean = statistics.fmean(valid)
    stddev = statistics.pstdev(valid)
    completeness = (
        len(valid) / expected_member_count
        if expected_member_count is not None
        else len(valid) / len(raw_values)
    )
    flags: list[str] = []
    if invalid_count:
        flags.append("invalid_or_missing_members")
    if expected_member_count is not None and len(valid) < expected_member_count:
        flags.append("incomplete_ensemble")

    return EnsembleStatistics(
        valid_member_count=len(valid),
        expected_member_count=expected_member_count,
        invalid_member_count=invalid_count,
        completeness_fraction=completeness,
        mean_mm=mean,
        p10_mm=p10,
        p25_mm=p25,
        p50_mm=p50,
        p75_mm=p75,
        p90_mm=p90,
        standard_deviation_mm=stddev,
        spread_p90_p10_mm=p90 - p10,
        interquartile_range_mm=p75 - p25,
        coefficient_of_variation=(stddev / mean if mean > 0 else None),
        probabilities_gt_mm={
            _threshold_key(threshold): probability_exceeding(valid, threshold)
            for threshold in thresholds
        },
        quality_flags=tuple(flags),
    )
