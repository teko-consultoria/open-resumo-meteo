"""Conservative, structured interpretation of the objective synoptic series.

The language model never receives raw grids.  This module turns independent
GFS and EC regional time series into a small set of auditable events.  A named
mechanism is promoted with high confidence only when both families satisfy the
same physical rule in a compatible place and period.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from .schema import WindowId
from .synoptic_regions import (
    SYNOPTIC_REGION_IDS,
    load_temperature_climatology,
    region_basin_ids,
    region_label,
    temperature_normal_degc,
)

TEMPERATURE_CLIMATOLOGY_SOURCE_ID = "noaa-ncep-temperature-climatology-1991-2020"
MODEL_FAMILY = {"gfs": "ncep", "ec": "ecmwf"}


def _field(step: Mapping[str, Any], field_id: str) -> Mapping[str, Any] | None:
    return next(
        (
            item
            for item in step.get("fields", ())
            if isinstance(item, Mapping) and item.get("field_id") == field_id
        ),
        None,
    )


def _regional_value(
    step: Mapping[str, Any],
    field_id: str,
    region_id: str,
    statistic: str = "p50",
) -> float | None:
    field = _field(step, field_id)
    if field is None:
        return None
    regions = field.get("regional_summaries")
    region = regions.get(region_id) if isinstance(regions, Mapping) else None
    if not isinstance(region, Mapping):
        return None
    try:
        value = float(region.get(statistic))
    except (TypeError, ValueError):
        return None
    units = str(field.get("units") or "").strip().casefold()
    if field_id == "mean_sea_level_pressure" and units == "pa":
        value /= 100.0
    return value


def _steps(inputs: Mapping[str, dict], model: str, window_id: str) -> list[dict]:
    value = inputs.get(model)
    if not isinstance(value, Mapping):
        return []
    return sorted(
        (
            step
            for step in value.get("timeline", ())
            if isinstance(step, dict) and step.get("window_id") == window_id
        ),
        key=lambda item: int(item["lead_hour"]),
    )


def _series(
    inputs: Mapping[str, dict],
    model: str,
    window_id: str,
    field_id: str,
    region_id: str,
    statistic: str = "p50",
) -> list[tuple[dict, float]]:
    values = []
    for step in _steps(inputs, model, window_id):
        value = _regional_value(step, field_id, region_id, statistic)
        if value is not None:
            values.append((step, value))
    return values


def _compatible_support(
    candidates: Mapping[str, dict | None],
    inputs: Mapping[str, dict],
) -> tuple[list[str], str] | None:
    models = [model for model in ("gfs", "ec") if candidates.get(model)]
    if not models:
        return None
    families = [MODEL_FAMILY[model] for model in models]
    complete = all(str(inputs[model].get("status")) == "complete" for model in models)
    confidence = "high" if len(models) == 2 and complete else "medium" if complete else "low"
    return families, confidence


def _feature_bounds(candidates: Mapping[str, dict | None], window) -> tuple[datetime, datetime, datetime | None]:
    present = [item for item in candidates.values() if item]
    start = max(window.start_utc, max(item["start"] for item in present))
    end = min(window.end_utc, min(item["end"] for item in present))
    if end <= start:
        start = max(window.start_utc, min(item["start"] for item in present))
        end = min(window.end_utc, max(item["end"] for item in present))
    peaks = [item.get("peak") for item in present if item.get("peak") is not None]
    peak = min(max(peaks), end) if peaks else None
    if peak is not None and peak < start:
        peak = start
    return start, end, peak


def _source_ids(models: list[str], model_source_ids: Mapping[str, str]) -> list[str]:
    return [model_source_ids[model] for model in ("gfs", "ec") if model in models]


def _affected(regions: tuple[str, ...]) -> list[str]:
    result = []
    for region_id in regions:
        for basin_id in region_basin_ids(region_id):
            if basin_id not in result:
                result.append(basin_id)
    return result


def _event(
    *,
    window,
    feature_type: str,
    canonical_name: str,
    lifecycle: str,
    regions: tuple[str, ...],
    effects: tuple[str, ...],
    evidence_fields: tuple[str, ...],
    candidates: Mapping[str, dict | None],
    inputs: Mapping[str, dict],
    model_source_ids: Mapping[str, str],
    description: str,
    continues_into: str | None = None,
) -> dict | None:
    support = _compatible_support(candidates, inputs)
    if support is None:
        return None
    families, confidence = support
    models = [model for model in ("gfs", "ec") if candidates.get(model)]
    start, end, peak = _feature_bounds(candidates, window)
    return {
        "feature_id": f"{window.window_id}-{feature_type}-{start:%Y%m%d%H}",
        "feature_type": feature_type,
        "canonical_name": canonical_name,
        "lifecycle": lifecycle,
        "start_utc": start,
        "end_utc": end,
        "peak_utc": peak,
        "regions": list(regions),
        "affected_basin_ids": _affected(regions),
        "effects": list(effects),
        "evidence_fields": list(evidence_fields),
        "supported_by_families": families,
        "confidence": confidence,
        "continues_into": continues_into,
        "description": description,
        "source_ids": _source_ids(models, model_source_ids),
    }


def _post_frontal_high_candidate(inputs: Mapping[str, dict], model: str, window_id: str) -> dict | None:
    steps = _steps(inputs, model, window_id)
    if not steps:
        return None
    early = steps[: min(4, len(steps))]
    pressure_south = [
        (step, value)
        for step in early
        if (value := _regional_value(step, "mean_sea_level_pressure", "south_brazil")) is not None
    ]
    pressure_southeast = [
        (step, value)
        for step in steps[: min(7, len(steps))]
        if (value := _regional_value(step, "mean_sea_level_pressure", "southeast_brazil")) is not None
    ]
    temperature_850 = [
        value
        for step in early
        if (value := _regional_value(step, "temperature_850", "south_brazil")) is not None
    ]
    meridional_wind = [
        value
        for step in early[:2]
        if (value := _regional_value(step, "wind_v_850", "south_brazil")) is not None
    ]
    temperature_2m = _series(
        inputs, model, window_id, "temperature_2m", "south_brazil", "mean"
    )
    if (
        not pressure_south
        or not pressure_southeast
        or not temperature_850
        or max(value for _step, value in pressure_south) < 1020.0
        or max(value for _step, value in pressure_southeast) < 1021.0
        or min(temperature_850) > 11.0
        or not meridional_wind
        or max(meridional_wind) < 1.5
        or len(temperature_2m) < 2
        or temperature_2m[-1][1] - temperature_2m[0][1] < 4.0
    ):
        return None
    combined = pressure_south + pressure_southeast
    peak_step, peak_value = max(combined, key=lambda item: item[1])
    end = steps[-1]["valid_time"] + timedelta(hours=12)
    for step in steps:
        if step["valid_time"] <= peak_step["valid_time"]:
            continue
        south = _regional_value(step, "mean_sea_level_pressure", "south_brazil")
        southeast = _regional_value(step, "mean_sea_level_pressure", "southeast_brazil")
        if south is not None and southeast is not None and max(south, southeast) <= peak_value - 2.5:
            end = step["valid_time"]
            break
    return {
        "start": steps[0]["valid_time"],
        "end": end,
        "peak": peak_step["valid_time"],
    }


def _departing_front_candidate(inputs: Mapping[str, dict], model: str, window_id: str) -> dict | None:
    steps = _steps(inputs, model, window_id)
    if len(steps) < 3:
        return None
    early = steps[:4]
    t850 = [
        value
        for step in early[:2]
        if (value := _regional_value(step, "temperature_850", "south_brazil")) is not None
    ]
    v850 = [
        value
        for step in early[:2]
        if (value := _regional_value(step, "wind_v_850", "south_brazil")) is not None
    ]
    rh_land = [
        value
        for step in early
        if (value := _regional_value(step, "relative_humidity_850", "south_brazil")) is not None
    ]
    rh_ocean = [
        value
        for step in early
        if (value := _regional_value(step, "relative_humidity_850", "south_atlantic")) is not None
    ]
    if (
        not t850
        or min(t850) > 11.0
        or not v850
        or max(v850) < 1.5
        or len(rh_land) < 2
        or rh_land[0] - min(rh_land[1:]) < 12.0
    ):
        return None
    offshore = rh_ocean and max(rh_ocean[1:]) >= max(min(rh_land[1:]) + 10.0, 55.0)
    return {
        "start": steps[0]["valid_time"],
        "end": min(steps[min(3, len(steps) - 1)]["valid_time"] + timedelta(hours=12), steps[-1]["valid_time"] + timedelta(hours=12)),
        "peak": steps[0]["valid_time"],
        "offshore": bool(offshore),
    }


def _deepening_low_candidate(inputs: Mapping[str, dict], model: str, window_id: str) -> dict | None:
    series = _series(
        inputs, model, window_id, "mean_sea_level_pressure", "la_plata", "p50"
    )
    if len(series) < 4:
        return None
    for end_index, (end_step, end_value) in enumerate(series[2:], start=2):
        prior = [
            (start_step, start_value)
            for start_step, start_value in series[:end_index]
            if 24
            <= int(end_step["lead_hour"]) - int(start_step["lead_hour"])
            <= 36
        ]
        if not prior:
            continue
        fall = max(start_value for _start_step, start_value in prior) - end_value
        # The lifecycle begins at the first time the low becomes materially
        # deep, not at an earlier weak fall or only at its eventual minimum.
        if fall >= 5.0 and end_value <= 1005.0:
            return {
                "start": end_step["valid_time"],
                "end": end_step["valid_time"] + timedelta(hours=12),
                "peak": end_step["valid_time"],
            }
    return None


def _active_low_candidate(
    inputs: Mapping[str, dict], model: str, window_id: str
) -> dict | None:
    steps = _steps(inputs, model, window_id)
    if len(steps) < 5:
        return None
    early = steps[: min(6, len(steps))]
    pressure = [
        (step, value)
        for step in early
        if (value := _regional_value(
            step,
            "mean_sea_level_pressure",
            "south_brazil",
        ))
        is not None
    ]
    meridional_wind = [
        value
        for step in early
        if (value := _regional_value(step, "wind_v_850", "south_brazil"))
        is not None
    ]
    humidity = [
        value
        for step in early
        if (value := _regional_value(
            step,
            "relative_humidity_850",
            "south_brazil",
        ))
        is not None
    ]
    if (
        not pressure
        or min(value for _step, value in pressure) > 1009.0
        or not meridional_wind
        or sum(meridional_wind) / len(meridional_wind) > -4.0
        or not humidity
        or max(humidity) < 60.0
    ):
        return None
    peak_step, peak_value = min(pressure, key=lambda item: item[1])
    end_step = steps[-1]
    lifecycle = "dominant"
    for step in steps:
        if step["valid_time"] <= peak_step["valid_time"]:
            continue
        value = _regional_value(
            step,
            "mean_sea_level_pressure",
            "south_brazil",
        )
        if value is not None and value >= peak_value + 6.0:
            end_step = step
            lifecycle = "weakening"
            break
    return {
        "start": steps[0]["valid_time"],
        "end": end_step["valid_time"] + timedelta(hours=12),
        "peak": peak_step["valid_time"],
        "lifecycle": lifecycle,
    }


def _frontal_crossing_candidate(inputs: Mapping[str, dict], model: str, window_id: str) -> dict | None:
    steps = _steps(inputs, model, window_id)
    candidates = []
    for region_id in ("south_brazil", "southeast_brazil"):
        for index, start in enumerate(steps):
            end = next(
                (
                    item
                    for item in steps[index + 1 :]
                    if int(item["lead_hour"]) - int(start["lead_hour"]) == 24
                ),
                None,
            )
            if end is None:
                continue
            t0 = _regional_value(start, "temperature_850", region_id)
            t1 = _regional_value(end, "temperature_850", region_id)
            rh0 = _regional_value(start, "relative_humidity_850", region_id)
            rh1 = _regional_value(end, "relative_humidity_850", region_id)
            v0 = _regional_value(start, "wind_v_850", region_id)
            v1 = _regional_value(end, "wind_v_850", region_id)
            if None in (t0, t1, rh0, rh1, v0, v1):
                continue
            if t1 - t0 <= -3.0 and rh1 - rh0 >= 10.0 and v1 - v0 >= 3.0:
                candidates.append((start, end, region_id))
    if not candidates:
        return None
    start, end, region_id = min(candidates, key=lambda item: item[0]["valid_time"])
    return {
        "start": start["valid_time"],
        "end": end["valid_time"] + timedelta(hours=12),
        "peak": end["valid_time"],
        "region": region_id,
    }


def _height_wave_candidate(
    inputs: Mapping[str, dict], model: str, window_id: str, *, direction: str
) -> dict | None:
    heights = _series(
        inputs, model, window_id, "geopotential_500", "south_brazil", "p50"
    )
    pressures = {
        int(step["lead_hour"]): value
        for step, value in _series(
            inputs,
            model,
            window_id,
            "mean_sea_level_pressure",
            "south_brazil",
            "p50",
        )
    }
    if len(heights) < 3:
        return None
    threshold = 55.0 if direction == "rising" else -40.0
    best = None
    for index, (start, start_value) in enumerate(heights[:-2]):
        for end, end_value in heights[index + 2 :]:
            hours = int(end["lead_hour"]) - int(start["lead_hour"])
            change = end_value - start_value
            pressure_change = pressures.get(int(end["lead_hour"]), 0) - pressures.get(
                int(start["lead_hour"]), 0
            )
            accepted = (
                direction == "rising" and change >= threshold and pressure_change >= -1.0
            ) or (
                direction == "falling" and change <= threshold and pressure_change <= -2.0
            )
            if accepted and 24 <= hours <= 60:
                score = abs(change)
                if best is None or score > best[0]:
                    best = (score, start, end)
    if best is None:
        return None
    _score, start, end = best
    return {
        "start": start["valid_time"],
        "end": end["valid_time"] + timedelta(hours=12),
        "peak": end["valid_time"],
    }


def _temperature_diagnostics(
    inputs: Mapping[str, dict],
    *,
    window,
    model_source_ids: Mapping[str, str],
) -> list[dict]:
    climatology = load_temperature_climatology()
    result = []
    for region_id in SYNOPTIC_REGION_IDS:
        by_model = {
            model: _series(
                inputs,
                model,
                window.window_id,
                "temperature_2m",
                region_id,
                "mean",
            )
            for model in ("gfs", "ec")
        }
        models = [model for model, rows in by_model.items() if len(rows) >= 2]
        if not models:
            continue
        early_values = []
        late_values = []
        normal_values = []
        for model in models:
            rows = by_model[model]
            segment = max(1, min(3, len(rows) // 3 or 1))
            early_values.extend(value for _step, value in rows[:segment])
            late_values.extend(value for _step, value in rows[-segment:])
            normal_values.extend(
                temperature_normal_degc(step["valid_time"].month, region_id)
                for step, _value in (*rows[:segment], *rows[-segment:])
            )
        start_value = sum(early_values) / len(early_values)
        end_value = sum(late_values) / len(late_values)
        normal = sum(normal_values) / len(normal_values)
        start_anomaly = start_value - normal
        end_anomaly = end_value - normal
        change = end_value - start_value

        def regime(anomaly: float) -> str:
            if anomaly <= -2.0:
                return "below_average"
            if anomaly >= 2.0:
                return "above_average"
            return "near_average"

        trend = "warming" if change >= 2.0 else "cooling" if change <= -2.0 else "stable"
        start_regime = regime(start_anomaly)
        end_regime = regime(end_anomaly)
        if trend == "stable" and start_regime == end_regime == "near_average":
            continue
        families = [MODEL_FAMILY[model] for model in models]
        result.append(
            {
                "region_id": region_id,
                "region_label": region_label(region_id),
                "start_regime": start_regime,
                "end_regime": end_regime,
                "trend": trend,
                "pattern_change": start_regime != end_regime,
                "start_anomaly_degc": round(start_anomaly, 1),
                "end_anomaly_degc": round(end_anomaly, 1),
                "change_degc": round(change, 1),
                "climatology_id": str(climatology["climatology_id"]),
                "supported_by_families": families,
                "confidence": "high" if len(models) == 2 else "medium",
                "source_ids": [
                    *_source_ids(models, model_source_ids),
                    TEMPERATURE_CLIMATOLOGY_SOURCE_ID,
                ],
            }
        )
    return result


def detect_structured_synoptic_features(
    inputs: Mapping[str, dict],
    *,
    forecast_windows,
    model_source_ids: Mapping[str, str],
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], list[str]]:
    """Return structured features, regional temperature and audit warnings."""

    features: dict[str, list[dict]] = {"p1": [], "p2": [], "p3": []}
    temperature: dict[str, list[dict]] = {"p1": [], "p2": [], "p3": []}
    warnings = []
    for window in forecast_windows:
        window_id = window.window_id
        temperature[window_id] = _temperature_diagnostics(
            inputs,
            window=window,
            model_source_ids=model_source_ids,
        )

        post_high = {
            model: _post_frontal_high_candidate(inputs, model, window_id)
            for model in ("gfs", "ec")
        }
        if window_id == "p1" and all(post_high.values()):
            value = _event(
                window=window,
                feature_type="post_frontal_high",
                canonical_name="alta pós-frontal",
                lifecycle="dominant",
                regions=("south_brazil", "southeast_brazil"),
                effects=(
                    "tempo_firme_no_centro_sul",
                    "temperaturas_abaixo_da_media_no_inicio",
                    "aquecimento_gradual_com_o_enfraquecimento_da_alta",
                ),
                evidence_fields=(
                    "mean_sea_level_pressure",
                    "temperature_850",
                    "wind_v_850",
                    "temperature_2m",
                ),
                candidates=post_high,
                inputs=inputs,
                model_source_ids=model_source_ids,
                description=(
                    "Alta pós-frontal domina o Centro-Sul no início, sustenta tempo "
                    "firme e ar mais frio, e perde força gradualmente na metade final."
                ),
            )
            if value:
                features[window_id].append(value)

        departing = {
            model: _departing_front_candidate(inputs, model, window_id)
            for model in ("gfs", "ec")
        }
        if window_id == "p1" and all(departing.values()):
            offshore = all(item and item.get("offshore") for item in departing.values())
            lifecycle = "moving_offshore" if offshore else "antecedent"
            description = (
                "A frente fria antecedente se afasta pelo Atlântico, com influência "
                "continental residual e precipitação mais restrita."
                if offshore
                else "A assinatura pós-frontal indica passagem recente de frente fria pelo Sul."
            )
            value = _event(
                window=window,
                feature_type="cold_front",
                canonical_name="frente fria",
                lifecycle=lifecycle,
                regions=("south_brazil", "southeast_brazil", "south_atlantic"),
                effects=("chuva_fraca_e_irregular_no_centro_sul", "frente_mais_oceanica" if offshore else "efeito_residual_no_continente"),
                evidence_fields=(
                    "temperature_850",
                    "relative_humidity_850",
                    "wind_v_850",
                    "mean_sea_level_pressure",
                ),
                candidates=departing,
                inputs=inputs,
                model_source_ids=model_source_ids,
                description=description,
            )
            if value:
                features[window_id].append(value)

        deepening = {
            model: _deepening_low_candidate(inputs, model, window_id)
            for model in ("gfs", "ec")
        }
        if all(deepening.values()):
            continues = (
                WindowId.P2.value
                if window_id == "p1"
                else WindowId.P3.value
                if window_id == "p2"
                else None
            )
            value = _event(
                window=window,
                feature_type="deepening_low",
                canonical_name="baixa pressão",
                lifecycle="developing",
                regions=("la_plata", "chaco", "south_brazil"),
                effects=(
                    "retorno_gradual_da_instabilidade_no_sul",
                    "fluxo_de_norte_e_aquecimento_pre_frontal",
                    "sinal_se_estende_para_a_janela_seguinte" if continues else "instabilidade_na_janela",
                ),
                evidence_fields=(
                    "mean_sea_level_pressure",
                    "temperature_850",
                    "wind_v_850",
                    "relative_humidity_850",
                ),
                candidates=deepening,
                inputs=inputs,
                model_source_ids=model_source_ids,
                description=(
                    "Uma baixa pressão se aprofunda na região do Prata no fim da "
                    "janela, iniciando novo ciclo de instabilidade no Sul."
                ),
                continues_into=continues,
            )
            if value:
                value["end_utc"] = window.end_utc
                features[window_id].append(value)

        active_low = {
            model: _active_low_candidate(inputs, model, window_id)
            for model in ("gfs", "ec")
        }
        if window_id == "p2" and all(active_low.values()):
            lifecycle = (
                "weakening"
                if all(
                    item and item.get("lifecycle") == "weakening"
                    for item in active_low.values()
                )
                else "dominant"
            )
            value = _event(
                window=window,
                feature_type="low_pressure",
                canonical_name="baixa pressão",
                lifecycle=lifecycle,
                regions=("south_brazil", "la_plata", "chaco"),
                effects=(
                    "instabilidade_persistente_no_sul",
                    "fluxo_de_norte_no_inicio",
                    "estabilizacao_gradual_com_o_afastamento_da_baixa"
                    if lifecycle == "weakening"
                    else "manutencao_da_instabilidade",
                ),
                evidence_fields=(
                    "mean_sea_level_pressure",
                    "relative_humidity_850",
                    "wind_v_850",
                ),
                candidates=active_low,
                inputs=inputs,
                model_source_ids=model_source_ids,
                description=(
                    "A baixa pressão formada na janela anterior atua sobre o Sul "
                    "no início e perde força gradualmente na metade final."
                    if lifecycle == "weakening"
                    else "A baixa pressão permanece ativa sobre o Sul durante a janela."
                ),
            )
            if value:
                features[window_id].append(value)

        crossing = {
            model: _frontal_crossing_candidate(inputs, model, window_id)
            for model in ("gfs", "ec")
        }
        if all(crossing.values()):
            regions = tuple(
                dict.fromkeys(
                    str(item["region"])
                    for item in crossing.values()
                    if item and item.get("region")
                )
            ) or ("south_brazil",)
            value = _event(
                window=window,
                feature_type="cold_front",
                canonical_name="frente fria",
                lifecycle="crossing",
                regions=regions,
                effects=("queda_de_temperatura", "aumento_da_umidade", "mudanca_do_vento"),
                evidence_fields=("temperature_850", "relative_humidity_850", "wind_v_850"),
                candidates=crossing,
                inputs=inputs,
                model_source_ids=model_source_ids,
                description="Frente fria cruza a região com queda térmica, aumento de umidade e mudança do vento.",
            )
            if value and not any(
                item["feature_type"] == "cold_front" for item in features[window_id]
            ):
                features[window_id].append(value)

        if not any(item["feature_type"] == "post_frontal_high" for item in features[window_id]):
            ridge = {
                model: _height_wave_candidate(
                    inputs, model, window_id, direction="rising"
                )
                for model in ("gfs", "ec")
            }
            if all(ridge.values()):
                value = _event(
                    window=window,
                    feature_type="ridge",
                    canonical_name="crista",
                    lifecycle="establishing",
                    regions=("south_brazil", "southeast_brazil"),
                    effects=("estabilizacao_da_atmosfera", "reducao_da_chuva"),
                    evidence_fields=("geopotential_500", "mean_sea_level_pressure"),
                    candidates=ridge,
                    inputs=inputs,
                    model_source_ids=model_source_ids,
                    description="Crista em 500 hPa reforça a estabilização sobre o Centro-Sul.",
                )
                if value:
                    features[window_id].append(value)

        trough = {
            model: _height_wave_candidate(
                inputs, model, window_id, direction="falling"
            )
            for model in ("gfs", "ec")
        }
        if all(trough.values()):
            value = _event(
                window=window,
                feature_type="trough",
                canonical_name="cavado",
                lifecycle="developing",
                regions=("south_brazil", "la_plata"),
                effects=("aumento_da_instabilidade",),
                evidence_fields=("geopotential_500", "mean_sea_level_pressure"),
                candidates=trough,
                inputs=inputs,
                model_source_ids=model_source_ids,
                description="Cavado em 500 hPa avança sobre o Sul e reforça a instabilidade.",
            )
            if value:
                features[window_id].append(value)

    if not any(features.values()):
        warnings.append(
            "Nenhum mecanismo sinótico passou pelos classificadores estruturados de duas famílias."
        )
    return features, temperature, warnings


__all__ = [
    "TEMPERATURE_CLIMATOLOGY_SOURCE_ID",
    "detect_structured_synoptic_features",
]
