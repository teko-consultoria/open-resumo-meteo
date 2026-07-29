"""Hard model horizons and interpolation without extrapolation."""

from __future__ import annotations

import math

from .constants import ForecastModel, model_specification, normalize_model_id


class ForecastHorizonError(ValueError):
    pass


class ForecastExtrapolationError(ForecastHorizonError):
    pass


def maximum_lead_hours(model: str | ForecastModel) -> int:
    return model_specification(model).maximum_lead_hours


def validate_lead_hour(model: str | ForecastModel, lead_hour: int | float) -> float:
    canonical = normalize_model_id(model)
    if isinstance(lead_hour, bool):
        raise TypeError("lead_hour deve ser numerico")
    lead = float(lead_hour)
    if not math.isfinite(lead) or lead < 0:
        raise ForecastHorizonError("lead_hour deve ser finito e nao negativo")
    maximum = maximum_lead_hours(canonical)
    if lead > maximum:
        raise ForecastExtrapolationError(
            f"{canonical.value} nao pode ser usado em {lead:g} h; horizonte maximo {maximum} h"
        )
    return lead


def validate_forecast_leads(
    model: str | ForecastModel,
    lead_hours: list[int | float] | tuple[int | float, ...],
) -> tuple[float, ...]:
    validated = tuple(validate_lead_hour(model, lead) for lead in lead_hours)
    if any(current <= previous for previous, current in zip(validated, validated[1:])):
        raise ForecastHorizonError("lead_hours deve estar estritamente crescente")
    return validated


def model_supports_window(
    model: str | ForecastModel,
    *,
    start_lead_hour: int | float,
    end_lead_hour: int | float,
) -> bool:
    """A window is supported only when its exclusive end boundary is available."""

    try:
        start = validate_lead_hour(model, start_lead_hour)
        end = validate_lead_hour(model, end_lead_hour)
    except ForecastHorizonError:
        return False
    return end > start
