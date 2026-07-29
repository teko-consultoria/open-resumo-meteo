"""Versioned editorial LLM adapter for the daily weather summary.

Configuration may be injected by the daily job or read from environment
variables. The adapter never logs or returns the API key. OpenAI Responses
requests explicitly disable response storage.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .schema import AnalysisPacket, StrictModel, WindowId

PROMPT_VERSION = "weather-summary-editorial-v27"
PROMPT_BUNDLE = {
    "window_system": "system_window_v15.txt",
    "window_execution": "execution_window_v9.txt",
    "window_repair_system": "system_repair_window_v1.txt",
    "window_repair_execution": "execution_repair_window_v1.txt",
}
DEFAULT_MODEL = "sabia-4"
DEFAULT_BASE_URL = "https://chat.maritaca.ai/api"
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_MAX_OUTPUT_TOKENS = 1200
DEFAULT_API_MODE = "chat_completions"
MAX_EDITORIAL_BASINS_PER_WINDOW = 10

PROMPT_DIRECTORY = Path(__file__).with_name("prompts")
_SENSITIVE_METADATA_KEY = re.compile(r"(?:api.?key|secret|token|password|credential)", re.IGNORECASE)
_METADATA_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class WeatherSummaryOpenAIError(RuntimeError):
    """Base error raised at the editorial API boundary."""


class MissingOpenAIAPIKey(WeatherSummaryOpenAIError):
    pass


class WeatherSummaryGenerationError(WeatherSummaryOpenAIError):
    pass


class OpenAIUsage(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    raw: dict[str, Any] = Field(default_factory=dict)


class OpenAIEditorialResult(StrictModel):
    text: str
    response_id: str
    model: str
    usage: OpenAIUsage
    prompt_version: str
    request_metadata: dict[str, str]


def _read_prompt(filename: str) -> str:
    return (PROMPT_DIRECTORY / filename).read_text(encoding="utf-8").strip()


def load_window_system_prompt() -> str:
    return _read_prompt(PROMPT_BUNDLE["window_system"])


def load_window_execution_template() -> str:
    return _read_prompt(PROMPT_BUNDLE["window_execution"])


def load_window_repair_system_prompt() -> str:
    return _read_prompt(PROMPT_BUNDLE["window_repair_system"])


def load_window_repair_execution_template() -> str:
    return _read_prompt(PROMPT_BUNDLE["window_repair_execution"])


def _heading(window_index: int, start: str, end: str) -> str:
    return f"{window_index}ª pêntada ({start} a {end}):"


def expected_heading(packet: AnalysisPacket, window_id: WindowId | str) -> str:
    selected = WindowId(window_id)
    window_indexes = {WindowId.P1: 1, WindowId.P2: 2, WindowId.P3: 3}
    window = packet.window(selected)
    return _heading(
        window_indexes[selected],
        window.start_utc.strftime("%d/%m"),
        window.end_utc.strftime("%d/%m"),
    )


def authorized_basin_accumulations(
    packet: AnalysisPacket,
    window_id: WindowId | str,
) -> dict[str, list[dict[str, object]]]:
    """Small editorial whitelist, excluding point maxima and climatology.

    The full packet still authorizes validation locally.  The LLM receives only
    the basin/window values selected for prose to minimize tokens and reduce
    the chance of mentioning marginal basins.
    """

    selected = WindowId(window_id)
    window_values: list[dict[str, object]] = []
    for rank, basin, basin_window in _selected_basin_windows(packet, selected):
        opinion = basin_window.opinion
        item: dict[str, object] = {
            "rank": rank,
            "name": basin.name,
            "ref": basin.editorial_reference,
            "rounded_mm": _rounded(opinion.rounded_mm),
            "confidence": opinion.confidence.value,
        }
        if opinion.rounded_mm is None:
            item["range_mm"] = [_rounded(item) for item in opinion.range_mm]
        window_values.append(item)
    return {selected.value: window_values}


def authorized_rain_literal(packet: AnalysisPacket, window_id: WindowId | str) -> str:
    """Render a copy-safe accumulation fragment for one editorial section."""

    selected = WindowId(window_id)
    values = authorized_basin_accumulations(packet, selected)[selected.value]
    rendered = [
        f"{item['rounded_mm']} mm {item['ref']}"
        for item in values
        if item.get("rounded_mm") is not None
    ]
    return "; ".join(rendered) if rendered else "não há acumulados quantificados autorizados"


def _section_role(window_id: WindowId) -> str:
    return {
        WindowId.P1: "apresente a configuração inicial e o principal gatilho do período",
        WindowId.P2: "destaque a evolução ou transição em relação à primeira pêntada",
        WindowId.P3: "feche com a tendência de persistência, retorno ou mudança do padrão",
    }[window_id]


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _rounded(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        rounded = round(value, 3)
        return int(rounded) if rounded.is_integer() else rounded
    return value


def _opinion_mm(window: Any) -> float | None:
    if window.opinion.rounded_mm is not None:
        return float(window.opinion.rounded_mm)
    if window.opinion.range_mm:
        return float(sum(window.opinion.range_mm) / len(window.opinion.range_mm))
    return None


def _spread_label(value: Any) -> str:
    if value is None:
        return "not_available"
    spread = abs(float(value))
    if spread <= 10:
        return "low"
    if spread <= 25:
        return "medium"
    return "high"


def _rain_regime(value_mm: Any, *, days: int = 1) -> str:
    if value_mm is None:
        return "indisponível"
    daily_mean = max(float(value_mm), 0.0) / max(int(days), 1)
    if daily_mean < 0.5:
        return "seco"
    if daily_mean < 3.0:
        return "chuva_fraca"
    if daily_mean < 10.0:
        return "chuva_moderada"
    return "chuvoso"


def _recent_rain_trend(values: list[float]) -> str:
    if not values:
        return "indisponível"
    if max(values) < 1.0:
        return "persistentemente_seco"
    if len(values) < 3:
        return "episódio_isolado_ou_série_curta"
    split = max(len(values) // 2, 1)
    earlier = sum(values[:split]) / split
    later_values = values[split:]
    later = sum(later_values) / max(len(later_values), 1)
    if later >= earlier + 5.0:
        return "umedecimento_recente"
    if earlier >= later + 5.0:
        return "secagem_recente"
    if sum(value >= 5.0 for value in values) >= 2:
        return "chuva_intermitente"
    return "predomínio_seco_com_chuva_isolada"


def _daily_timing_pattern(values: list[float | None]) -> str:
    wet_indexes = [
        index
        for index, value in enumerate(values)
        if value is not None and value >= 5.0
    ]
    if not wet_indexes:
        return "sem_sinal_diário_material"
    if len(wet_indexes) >= max(len(values) - 1, 1):
        return "distribuído_ao_longo_da_janela"
    midpoint = max((len(values) - 1) / 2.0, 0.0)
    mean_index = sum(wet_indexes) / len(wet_indexes)
    if mean_index < midpoint - 0.5:
        return "concentrado_na_metade_inicial"
    if mean_index > midpoint + 0.5:
        return "concentrado_na_metade_final"
    return "intermitente_no_meio_da_janela"


def _selected_basin_windows(
    packet: AnalysisPacket,
    window_id: WindowId,
) -> list[tuple[int, Any, Any]]:
    """Rank basin/window rows for prose and keep only the editorial shortlist."""

    affected_ids = {
        str(basin_id)
        for feature in getattr(
            packet.synoptic_diagnostics,
            window_id.value,
        ).structured_features
        for basin_id in feature.affected_basin_ids
    }
    candidates: list[tuple[float, float, float, str, bool, Any, Any]] = []
    for basin in packet.basins:
        if basin.windows is None:
            continue
        basin_window = getattr(basin.windows, window_id.value)
        mm = _opinion_mm(basin_window)
        if mm is None:
            continue
        difference = abs(float(basin_window.families.difference_mm or 0))
        antecedent_5d = (
            float(basin.observed.rain_5d_mm)
            if basin.observed is not None
            and basin.observed.rain_5d_mm is not None
            else 0.0
        )
        candidates.append(
            (
                mm,
                antecedent_5d,
                difference,
                basin.name,
                basin.basin_id in affected_ids,
                basin,
                basin_window,
            )
        )
    positive = sorted(
        (item for item in candidates if item[0] > 0),
        key=lambda item: (-item[0], -item[2], item[3]),
    )
    wet_to_dry = sorted(
        (
            item
            for item in candidates
            if item[0] <= 0 and item[1] >= 25.0
        ),
        key=lambda item: (-item[1], -item[2], item[3]),
    )
    # Keep the wettest basins, then reserve up to three slots for basins tied
    # to an objectively classified system. This prevents a widespread but
    # low-accumulation frontal signal in the Southeast/South from disappearing
    # behind convective totals in the North.
    ranked = positive[: min(6, MAX_EDITORIAL_BASINS_PER_WINDOW)]
    ranked_basin_ids = {item[5].basin_id for item in ranked}
    for item in positive:
        if not item[4] or item[5].basin_id in ranked_basin_ids:
            continue
        ranked.append(item)
        ranked_basin_ids.add(item[5].basin_id)
        if sum(1 for candidate in ranked if candidate[4]) >= 3:
            break
        if len(ranked) >= MAX_EDITORIAL_BASINS_PER_WINDOW:
            break
    for item in positive:
        if len(ranked) >= MAX_EDITORIAL_BASINS_PER_WINDOW:
            break
        if item[5].basin_id not in ranked_basin_ids:
            ranked.append(item)
            ranked_basin_ids.add(item[5].basin_id)
    remaining = MAX_EDITORIAL_BASINS_PER_WINDOW - len(ranked)
    if remaining > 0:
        ranked.extend(
            item
            for item in wet_to_dry
            if item[5].basin_id not in ranked_basin_ids
        )
        ranked = ranked[:MAX_EDITORIAL_BASINS_PER_WINDOW]
    if not ranked:
        ranked = sorted(
            candidates,
            key=lambda item: (-item[0], -item[2], item[3]),
        )[: min(4, MAX_EDITORIAL_BASINS_PER_WINDOW)]
    return [
        (rank, basin, basin_window)
        for rank, (_, _, _, _, _, basin, basin_window) in enumerate(
            ranked,
            start=1,
        )
    ]


def _compact_basin_highlight(rank: int, basin: Any, basin_window: Any) -> dict[str, object]:
    flags = []
    if basin_window.families.agreement.value in {"low", "not_available"}:
        flags.append("maior_incerteza_entre_familias")
    climatology_regime = "not_available"
    if basin_window.climatology is not None:
        percentile = float(basin_window.climatology.percentile)
        climatology_regime = (
            "well_above"
            if percentile >= 75
            else "above"
            if percentile >= 60
            else "well_below"
            if percentile <= 25
            else "below"
            if percentile <= 40
            else "near"
        )
    model_values = {
        "GFS": (
            basin_window.gfs.corrected_basin_mean_mm
            if basin_window.gfs is not None
            and basin_window.gfs.corrected_basin_mean_mm is not None
            else basin_window.gfs.raw_basin_mean_mm
            if basin_window.gfs is not None
            else None
        ),
        "GEFS": basin_window.gefs.p50_mm if basin_window.gefs is not None else None,
        "EC": (
            basin_window.ec.corrected_basin_mean_mm
            if basin_window.ec is not None
            and basin_window.ec.corrected_basin_mean_mm is not None
            else basin_window.ec.raw_basin_mean_mm
            if basin_window.ec is not None
            else None
        ),
        "ECENS": basin_window.ecens.p50_mm if basin_window.ecens is not None else None,
    }
    available_model_values = {
        model: float(value)
        for model, value in model_values.items()
        if value is not None
    }
    duration_days = len(basin_window.daily_timing) or 5
    model_regimes = {
        model: _rain_regime(value, days=duration_days)
        for model, value in model_values.items()
        if value is not None
    }
    family_values = {
        family: sum(values) / len(values)
        for family, values in {
            "GFS_GEFS": [
                available_model_values[model]
                for model in ("GFS", "GEFS")
                if model in available_model_values
            ],
            "EC_ECENS": [
                available_model_values[model]
                for model in ("EC", "ECENS")
                if model in available_model_values
            ],
        }.items()
        if values
    }
    contrast = "not_available"
    wettest_model = driest_model = None
    if len(family_values) == 2:
        wettest_model = max(available_model_values, key=available_model_values.get)
        driest_model = min(available_model_values, key=available_model_values.get)
        regime_order = {
            "seco": 0,
            "chuva_fraca": 1,
            "chuva_moderada": 2,
            "chuvoso": 3,
        }
        family_regimes = {
            family: _rain_regime(value, days=duration_days)
            for family, value in family_values.items()
        }
        regime_span = max(regime_order[value] for value in family_regimes.values()) - min(
            regime_order[value] for value in family_regimes.values()
        )
        contrast = (
            "strong"
            if regime_span >= 2
            else "moderate"
            if regime_span == 1
            else "low"
        )
    compact = {
        "rank": rank,
        "basin_id": basin.basin_id,
        "name": basin.name,
        "ref": basin.editorial_reference,
        "subsystem": _enum_value(basin.subsystem),
        "rounded_mm": _rounded(basin_window.opinion.rounded_mm),
        "confidence": basin_window.opinion.confidence.value,
        "family_agreement": basin_window.families.agreement.value,
        "family_spread": _spread_label(basin_window.families.difference_mm),
        "basis": basin_window.opinion.basis,
        "climatology_regime": climatology_regime,
        "selection_reason": (
            "wet_to_drier_transition"
            if _opinion_mm(basin_window) == 0
            and basin.observed is not None
            and basin.observed.rain_5d_mm is not None
            and basin.observed.rain_5d_mm >= 25
            else "forecast_accumulation"
        ),
        "flags": flags,
        "model_contrast": contrast,
    }
    if contrast in {"moderate", "strong"}:
        compact["model_views"] = model_regimes
        compact["wettest_model"] = wettest_model
        compact["driest_model"] = driest_model
    return compact


def _compact_synoptic_window(value: Any) -> dict[str, object]:
    supported_features = list(value.supported_features)[:6]
    dominant_pattern = (
        value.dominant_pattern
        if supported_features
        else "distribution_and_persistence_only"
    )
    return {
        "dominant_pattern": dominant_pattern,
        "supported_features": supported_features,
        "structured_features": [
            {
                "type": feature.feature_type,
                "name": feature.canonical_name,
                "lifecycle": feature.lifecycle,
                "start": _editorial_event_time(feature.start_utc),
                "end": _editorial_event_time(feature.end_utc),
                "regions": feature.regions,
                "affected_basin_ids": feature.affected_basin_ids,
                "effects": feature.effects,
                "continues_into": (
                    feature.continues_into.value
                    if feature.continues_into is not None
                    else None
                ),
                "description": feature.description,
                "confidence": feature.confidence.value,
            }
            for feature in value.structured_features
        ],
        "temperature_evolution": [
            {
                "region": diagnostic.region_label,
                "start_regime": diagnostic.start_regime,
                "end_regime": diagnostic.end_regime,
                "trend": diagnostic.trend,
                "pattern_change": diagnostic.pattern_change,
                "confidence": diagnostic.confidence.value,
            }
            for diagnostic in value.regional_temperature
        ],
        "family_agreement": value.family_agreement.value,
    }


_PORTUGUESE_WEEKDAYS = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)


def _editorial_event_time(value: Any) -> str:
    return (
        f"{_PORTUGUESE_WEEKDAYS[value.weekday()]}, "
        f"{value.strftime('%d/%m às %H')} UTC"
    )


def _compact_energy_signals(
    packet: AnalysisPacket,
    window_ids: tuple[WindowId, ...],
) -> dict[str, list[dict[str, object]]]:
    selected = {window.value for window in window_ids}
    compact: dict[str, list[dict[str, object]]] = {}
    for category in ("wind", "temperature", "solar", "severe_weather"):
        compact[category] = [
            {
                "window_id": signal.window_id.value,
                "areas_or_assets": signal.areas_or_assets,
                "interpretation": signal.operational_interpretation,
                "confidence": signal.confidence.value,
            }
            for signal in getattr(packet.energy_signals, category)
            if signal.window_id.value in selected
        ]
    return compact


def _editorial_basins(packet: AnalysisPacket, window_ids: tuple[WindowId, ...]) -> list[Any]:
    wanted_order = []
    for selected in window_ids:
        for _rank, basin, _basin_window in _selected_basin_windows(packet, selected):
            if basin.basin_id not in wanted_order:
                wanted_order.append(basin.basin_id)
    limit = MAX_EDITORIAL_BASINS_PER_WINDOW
    wanted = set(wanted_order[:limit])
    by_id = {basin.basin_id: basin for basin in packet.basins}
    return [
        by_id[basin_id]
        for basin_id in wanted_order[:limit]
        if basin_id in wanted and basin_id in by_id
    ]


def _compact_observed_context(packet: AnalysisPacket, window_ids: tuple[WindowId, ...]) -> dict[str, object]:
    basins = _editorial_basins(packet, window_ids)
    rows = []
    for basin in basins:
        observed = basin.observed
        if observed is None:
            continue
        recent_values = [float(item.basin_mean_mm) for item in observed.recent_daily]
        rows.append(
            {
                "basin_id": basin.basin_id,
                "ref": basin.editorial_reference,
                "last_complete_day_regime": _rain_regime(observed.rain_24h_mm),
                "antecedent_5d_regime": _rain_regime(
                    observed.rain_5d_mm,
                    days=5,
                ),
                "antecedent_10d_regime": _rain_regime(
                    observed.rain_10d_mm,
                    days=10,
                ),
                "recent_trend": _recent_rain_trend(recent_values),
                "latest_partial_day_regime": _rain_regime(
                    observed.rain_latest_partial_mm
                ),
                "status": observed.status.value,
            }
        )
    ranked = sorted(
        rows,
        key=lambda item: (
            item.get("latest_partial_day_regime") != "chuvoso",
            item.get("last_complete_day_regime") != "chuvoso",
        ),
    )
    return {
        "basins": rows,
        "wettest_recent_refs": [item["ref"] for item in ranked[:5]],
    }


def _compact_forecast_evolution(packet: AnalysisPacket, window_ids: tuple[WindowId, ...]) -> dict[str, object]:
    rows = []
    for basin in _editorial_basins(packet, window_ids):
        if basin.windows is None:
            continue
        window_values = {
            selected.value: _opinion_mm(getattr(basin.windows, selected.value))
            for selected in (WindowId.P1, WindowId.P2, WindowId.P3)
        }
        daily_by_window: dict[str, list[list[object]]] = {}
        daily_values_by_window: dict[str, list[float | None]] = {}
        for selected in (WindowId.P1, WindowId.P2, WindowId.P3):
            daily_by_window[selected.value] = []
            daily_values_by_window[selected.value] = []
            for item in getattr(basin.windows, selected.value).daily_timing:
                daily_by_window[selected.value].append(
                    [
                        item.date.strftime("%d/%m"),
                        _rain_regime(item.deterministic_center_mm),
                        item.confidence.value,
                    ]
                )
                daily_values_by_window[selected.value].append(
                    float(item.deterministic_center_mm)
                    if item.deterministic_center_mm is not None
                    else None
                )
        observed = basin.observed
        forecast_p1 = window_values.get("p1")
        antecedent = float(observed.rain_5d_mm) if observed and observed.rain_5d_mm is not None else None
        transition = (
            "padrão_úmido_persiste"
            if antecedent is not None and antecedent >= 25 and forecast_p1 is not None and forecast_p1 >= 25
            else "úmido_para_mais_seco"
            if antecedent is not None and antecedent >= 25 and forecast_p1 is not None and forecast_p1 < 15
            else "seco_para_mais_úmido"
            if antecedent is not None and antecedent < 15 and forecast_p1 is not None and forecast_p1 >= 25
            else "sem_transição_material"
        )
        rows.append(
            {
                "basin_id": basin.basin_id,
                "ref": basin.editorial_reference,
                "window_regimes": {
                    key: _rain_regime(value, days=(4 if key == "p3" else 5))
                    for key, value in window_values.items()
                },
                "requested_daily_timing_regimes": {
                    selected.value: daily_by_window[selected.value]
                    for selected in window_ids
                },
                "timing_pattern_by_window": {
                    selected.value: _daily_timing_pattern(
                        daily_values_by_window[selected.value]
                    )
                    for selected in (WindowId.P1, WindowId.P2, WindowId.P3)
                },
                "antecedent_to_forecast": transition,
            }
        )
    return {
        "rows": rows,
    }


def _compact_synoptic_sequence(packet: AnalysisPacket) -> dict[str, object]:
    return {
        selected.value: {
            "features": [
                {
                    "name": feature.canonical_name,
                    "lifecycle": feature.lifecycle,
                    "continues_into": (
                        feature.continues_into.value
                        if feature.continues_into is not None
                        else None
                    ),
                }
                for feature in getattr(
                    packet.synoptic_diagnostics,
                    selected.value,
                ).structured_features
            ],
        }
        for selected in (WindowId.P1, WindowId.P2, WindowId.P3)
    }


def _compact_confidence_summary(
    packet: AnalysisPacket,
    window_ids: tuple[WindowId, ...],
) -> dict[str, object]:
    summary: dict[str, object] = {}
    for selected in window_ids:
        selected_rows = _selected_basin_windows(packet, selected)
        confidences = [
            basin_window.opinion.confidence.value
            for _rank, _basin, basin_window in selected_rows
        ]
        lower_refs = [
            basin.editorial_reference
            for _rank, basin, basin_window in selected_rows
            if basin_window.opinion.confidence.value in {"low", "medium"}
        ]
        model_contrasts = []
        for rank, basin, basin_window in selected_rows:
            highlight = _compact_basin_highlight(rank, basin, basin_window)
            if highlight["model_contrast"] not in {"moderate", "strong"}:
                continue
            model_contrasts.append(
                {
                    "ref": basin.editorial_reference,
                    "strength": highlight["model_contrast"],
                    "driest_model": highlight["driest_model"],
                    "wettest_model": highlight["wettest_model"],
                }
            )
        overall = (
            "indisponível"
            if not confidences
            else "alta"
            if all(value == "high" for value in confidences)
            else "baixa"
            if any(value == "low" for value in confidences)
            else "moderada"
        )
        payload: dict[str, object] = {
            "overall": overall,
            "lower_confidence_refs": lower_refs,
        }
        if model_contrasts:
            payload["reason"] = "model_dispersion"
            payload["model_contrasts"] = model_contrasts
        summary[selected.value] = payload
    return summary


def compact_editorial_packet(
    packet: AnalysisPacket,
    window_id: WindowId | str,
) -> dict[str, object]:
    """Small, source-derived payload for the language-model boundary.

    The full ``AnalysisPacket`` remains the audit object and validation source.
    This compact view carries only the already-derived fields the editor needs.
    """

    selected_window_id = WindowId(window_id)
    window_ids = (selected_window_id,)
    synoptic_sequence = _compact_synoptic_sequence(packet)
    return {
        "schema_version": "weather-summary-editorial-brief.v7",
        "issue": {
            "date": packet.issue.date.isoformat(),
            "time": packet.issue.time.isoformat(timespec="minutes"),
            "timezone": packet.issue.timezone,
        },
        "forecast_windows": [
            {
                "id": window.id.value,
                "label": window.label,
                "start": window.start_utc.strftime("%d/%m"),
                "end": window.end_utc.strftime("%d/%m"),
            }
            for window in packet.forecast_windows
            if window.id in window_ids
        ],
        "basin_highlights": {
            selected.value: [
                _compact_basin_highlight(rank, basin, basin_window)
                for rank, basin, basin_window in _selected_basin_windows(packet, selected)[
                    :MAX_EDITORIAL_BASINS_PER_WINDOW
                ]
            ]
            for selected in window_ids
        },
        "synoptic_brief": {
            selected.value: _compact_synoptic_window(
                getattr(packet.synoptic_diagnostics, selected.value)
            )
            for selected in window_ids
        },
        "synoptic_sequence": synoptic_sequence,
        "confidence_summary": _compact_confidence_summary(packet, window_ids),
        "observed_context": _compact_observed_context(packet, window_ids),
        "forecast_evolution": _compact_forecast_evolution(packet, window_ids),
        "episode_context": {
            "zcas_named_authorized_by_window": {
                selected.value: any(
                    str(feature).strip().casefold() == "zcas"
                    for feature in getattr(
                        packet.synoptic_diagnostics,
                        selected.value,
                    ).supported_features
                )
                for selected in (WindowId.P1, WindowId.P2, WindowId.P3)
            },
        },
        "energy_signals": _compact_energy_signals(packet, window_ids),
        "material_change": None
        if packet.material_change is None
        else packet.material_change.model_dump(mode="json", exclude_none=False),
    }


def build_execution_message(
    packet: AnalysisPacket,
    window_id: WindowId | str,
    *,
    prior_sections: tuple[str, ...] = (),
) -> str:
    """Render the versioned user message without interpreting packet text."""

    selected_window_id = WindowId(window_id)
    compact_packet_json = json.dumps(
        compact_editorial_packet(packet, selected_window_id),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    replacements = {
        "{{issue_date}}": packet.issue.date.isoformat(),
        "{{issue_time}}": packet.issue.time.isoformat(timespec="minutes"),
        "{{timezone}}": packet.issue.timezone,
        "{{packet_id}}": packet.audit.packet_id,
        "{{schema_version}}": str(packet.schema_version),
        "{{expected_heading}}": expected_heading(packet, selected_window_id),
        "{{window_id}}": selected_window_id.value,
        "{{authorized_basin_accumulations}}": json.dumps(
            authorized_basin_accumulations(packet, selected_window_id),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "{{authorized_rain_literal}}": authorized_rain_literal(packet, selected_window_id),
        "{{section_role}}": _section_role(selected_window_id),
        "{{prior_sections}}": "\n\n".join(prior_sections) or "(nenhuma seção anterior)",
        "{{editorial_packet_json}}": compact_packet_json,
    }
    message = load_window_execution_template()
    for marker, value in replacements.items():
        message = message.replace(marker, value)
    unresolved = re.findall(r"\{\{[^{}]+\}\}", message)
    if unresolved:
        raise WeatherSummaryGenerationError(f"unresolved execution template markers: {unresolved}")
    return message


def build_window_repair_message(
    packet: AnalysisPacket,
    *,
    window_id: WindowId | str,
    invalid_text: str,
    validation_error_code: str,
    validation_error_message: str,
    prior_sections: tuple[str, ...] = (),
) -> str:
    """Render a repair request scoped to the rejected pentad only."""

    selected = WindowId(window_id)
    replacements = {
        "{{expected_heading}}": expected_heading(packet, selected),
        "{{window_id}}": selected.value,
        "{{validation_error_code}}": validation_error_code,
        "{{validation_error_message}}": validation_error_message,
        "{{invalid_text}}": str(invalid_text or "").strip(),
        "{{authorized_basin_accumulations}}": json.dumps(
            authorized_basin_accumulations(packet, selected),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "{{authorized_rain_literal}}": authorized_rain_literal(packet, selected),
        "{{section_role}}": _section_role(selected),
        "{{prior_sections}}": "\n\n".join(prior_sections) or "(nenhuma seção anterior)",
        "{{editorial_packet_json}}": json.dumps(
            compact_editorial_packet(packet, selected),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    }
    message = load_window_repair_execution_template()
    for marker, value in replacements.items():
        message = message.replace(marker, value)
    unresolved = re.findall(r"\{\{[^{}]+\}\}", message)
    if unresolved:
        raise WeatherSummaryGenerationError(f"unresolved window repair template markers: {unresolved}")
    return message


def _environment_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise WeatherSummaryOpenAIError(f"{name} must be an integer") from exc
    if value < minimum:
        raise WeatherSummaryOpenAIError(f"{name} must be at least {minimum}")
    return value


def _environment_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise WeatherSummaryOpenAIError(f"{name} must be numeric") from exc
    if value < minimum:
        raise WeatherSummaryOpenAIError(f"{name} must be at least {minimum}")
    return value


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict())
    return str(value)


def _attribute(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _usage_from_response(response: Any) -> OpenAIUsage:
    usage_object = _attribute(response, "usage")
    raw = _json_safe(usage_object) if usage_object is not None else {}
    if not isinstance(raw, dict):
        raw = {}
    input_details = raw.get("input_tokens_details") or {}
    output_details = raw.get("output_tokens_details") or {}
    return OpenAIUsage(
        input_tokens=raw.get("input_tokens") or raw.get("prompt_tokens"),
        output_tokens=raw.get("output_tokens") or raw.get("completion_tokens"),
        total_tokens=raw.get("total_tokens"),
        cached_input_tokens=input_details.get("cached_tokens"),
        reasoning_output_tokens=output_details.get("reasoning_tokens"),
        raw=raw,
    )


def _audit_metadata(
    packet: AnalysisPacket,
    *,
    packet_json: str,
    extra: Mapping[str, Any] | None,
) -> dict[str, str]:
    metadata: dict[str, str] = {
        "pipeline": "weather_summary",
        "prompt_version": PROMPT_VERSION,
        "schema_version": str(packet.schema_version),
        "packet_id": packet.audit.packet_id,
        "issue_date": packet.issue.date.isoformat(),
        "packet_sha256": hashlib.sha256(packet_json.encode("utf-8")).hexdigest(),
    }
    for key, value in (extra or {}).items():
        key = str(key)
        if _SENSITIVE_METADATA_KEY.search(key):
            raise WeatherSummaryOpenAIError(f"sensitive metadata key is not allowed: {key!r}")
        if not _METADATA_KEY.fullmatch(key):
            raise WeatherSummaryOpenAIError(f"invalid metadata key: {key!r}")
        serialized = str(value)
        if len(serialized) > 512:
            raise WeatherSummaryOpenAIError(f"metadata value is too long for key {key!r}")
        metadata[key] = serialized
    if len(metadata) > 16:
        raise WeatherSummaryOpenAIError("OpenAI request metadata cannot exceed 16 entries")
    return metadata


def _generic_openai_key_allowed_for_base_url(base_url: str | None) -> bool:
    cleaned = str(base_url or "").strip().lower()
    return not cleaned or "api.openai.com" in cleaned


class WeatherSummaryOpenAIClient:
    """Small synchronous adapter around OpenAI or OpenAI-compatible APIs."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        max_attempts: int | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: LiteralReasoningEffort | None = None,
        base_url: str | None = None,
        api_mode: Literal["responses", "chat_completions"] | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model or os.environ.get("WEATHER_SUMMARY_LLM_MODEL") or DEFAULT_MODEL
        configured_base_url = (
            base_url
            if base_url is not None
            else os.environ.get("WEATHER_SUMMARY_LLM_BASE_URL", DEFAULT_BASE_URL)
        )
        self.base_url = str(configured_base_url or "").strip() or None
        configured_api_mode = (
            api_mode
            or os.environ.get("WEATHER_SUMMARY_LLM_API_MODE")
            or DEFAULT_API_MODE
        )
        if configured_api_mode not in {"responses", "chat_completions"}:
            raise WeatherSummaryOpenAIError(
                "api_mode must be 'responses' or 'chat_completions'"
            )
        self.api_mode = configured_api_mode
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else _environment_float(
                "WEATHER_SUMMARY_LLM_TIMEOUT_SECONDS",
                DEFAULT_TIMEOUT_SECONDS,
                minimum=1,
            )
        )
        self.max_attempts = (
            max_attempts
            if max_attempts is not None
            else _environment_int(
                "WEATHER_SUMMARY_LLM_MAX_ATTEMPTS",
                DEFAULT_MAX_ATTEMPTS,
                minimum=1,
            )
        )
        self.max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else _environment_int(
                "WEATHER_SUMMARY_LLM_MAX_OUTPUT_TOKENS",
                DEFAULT_MAX_OUTPUT_TOKENS,
                minimum=256,
            )
        )
        if self.timeout_seconds < 1:
            raise WeatherSummaryOpenAIError("timeout_seconds must be at least 1")
        if self.max_attempts < 1:
            raise WeatherSummaryOpenAIError("max_attempts must be at least 1")
        if self.max_output_tokens < 256:
            raise WeatherSummaryOpenAIError("max_output_tokens must be at least 256")
        self.reasoning_effort = reasoning_effort

        if client is not None:
            self._client = client
            return

        resolved_key = (
            api_key
            or os.environ.get("WEATHER_SUMMARY_LLM_API_KEY")
            or (
                os.environ.get("OPENAI_API_KEY")
                if _generic_openai_key_allowed_for_base_url(self.base_url)
                else None
            )
        )
        if not resolved_key:
            raise MissingOpenAIAPIKey(
                "WEATHER_SUMMARY_LLM_API_KEY or OPENAI_API_KEY is required "
                "for weather summary generation"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - exercised only in a broken runtime image.
            raise WeatherSummaryOpenAIError("the openai package is not installed") from exc

        # The SDK performs bounded exponential-backoff retries. max_attempts is
        # the operator-facing total, hence SDK max_retries = attempts - 1.
        client_kwargs: dict[str, Any] = {
            "api_key": resolved_key,
            "timeout": self.timeout_seconds,
            "max_retries": self.max_attempts - 1,
        }
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        self._client = OpenAI(
            **client_kwargs,
        )

    def generate(
        self,
        packet: AnalysisPacket | Mapping[str, Any],
        *,
        window_id: WindowId | str,
        audit_metadata: Mapping[str, Any] | None = None,
        prior_sections: tuple[str, ...] = (),
    ) -> OpenAIEditorialResult:
        validated_packet = (
            packet if isinstance(packet, AnalysisPacket) else AnalysisPacket.model_validate(packet)
        )
        if not validated_packet.quality.generation_allowed:
            raise WeatherSummaryGenerationError(
                "analysis packet has quality blockers; the OpenAI request was not sent"
            )

        packet_json = json.dumps(
            validated_packet.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        metadata = _audit_metadata(
            validated_packet,
            packet_json=packet_json,
            extra={
                **dict(audit_metadata or {}),
                "window_id": WindowId(window_id).value,
            },
        )
        selected_window_id = WindowId(window_id)
        instructions = load_window_system_prompt()
        execution_message = build_execution_message(
            validated_packet,
            selected_window_id,
            prior_sections=prior_sections,
        )
        if self.api_mode == "chat_completions":
            return self._generate_chat_completion(
                instructions=instructions,
                execution_message=execution_message,
                metadata=metadata,
            )
        return self._generate_response(
            instructions=instructions,
            execution_message=execution_message,
            metadata=metadata,
        )

    def repair_window(
        self,
        packet: AnalysisPacket | Mapping[str, Any],
        *,
        window_id: WindowId | str,
        invalid_text: str,
        validation_error_code: str,
        validation_error_message: str,
        audit_metadata: Mapping[str, Any] | None = None,
        prior_sections: tuple[str, ...] = (),
    ) -> OpenAIEditorialResult:
        """Repair only the rejected section, preserving approved pentads."""

        validated_packet = (
            packet if isinstance(packet, AnalysisPacket) else AnalysisPacket.model_validate(packet)
        )
        selected = WindowId(window_id)
        packet_json = json.dumps(
            validated_packet.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        metadata = _audit_metadata(
            validated_packet,
            packet_json=packet_json,
            extra={
                **dict(audit_metadata or {}),
                "repair": "window",
                "window_id": selected.value,
                "validation_code": validation_error_code,
            },
        )
        instructions = load_window_repair_system_prompt()
        execution_message = build_window_repair_message(
            validated_packet,
            window_id=selected,
            invalid_text=invalid_text,
            validation_error_code=validation_error_code,
            validation_error_message=validation_error_message,
            prior_sections=prior_sections,
        )
        if self.api_mode == "chat_completions":
            return self._generate_chat_completion(
                instructions=instructions,
                execution_message=execution_message,
                metadata=metadata,
            )
        return self._generate_response(
            instructions=instructions,
            execution_message=execution_message,
            metadata=metadata,
        )

    def _generate_response(
        self,
        *,
        instructions: str,
        execution_message: str,
        metadata: dict[str, str],
    ) -> OpenAIEditorialResult:
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": execution_message,
            "max_output_tokens": self.max_output_tokens,
            "metadata": metadata,
            "store": False,
        }
        if self.reasoning_effort is not None:
            request["reasoning"] = {"effort": self.reasoning_effort}

        try:
            response = self._client.responses.create(**request)
        except Exception as exc:
            # Do not interpolate the client, request, prompt or credential.
            raise WeatherSummaryGenerationError(
                f"OpenAI Responses API request failed ({type(exc).__name__})"
            ) from exc

        status = _attribute(response, "status")
        if status not in (None, "completed"):
            incomplete = _json_safe(_attribute(response, "incomplete_details"))
            raise WeatherSummaryGenerationError(
                f"OpenAI response did not complete; status={status!r}, details={incomplete!r}"
            )
        text = (_attribute(response, "output_text") or "").strip()
        if not text:
            raise WeatherSummaryGenerationError("OpenAI response contained no output_text")
        response_id = _attribute(response, "id")
        response_model = _attribute(response, "model") or self.model
        if not response_id:
            raise WeatherSummaryGenerationError("OpenAI response did not include an id")

        return OpenAIEditorialResult(
            text=text,
            response_id=str(response_id),
            model=str(response_model),
            usage=_usage_from_response(response),
            prompt_version=PROMPT_VERSION,
            request_metadata=metadata,
        )

    def _generate_chat_completion(
        self,
        *,
        instructions: str,
        execution_message: str,
        metadata: dict[str, str],
    ) -> OpenAIEditorialResult:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": execution_message},
            ],
            "max_tokens": self.max_output_tokens,
        }
        # Kimi K2.6 is a general-purpose thinking model; disable thinking for
        # this deterministic editorial task so the response budget is reserved
        # for the reader-facing text. Kimi K2.7 Code does not allow disabling
        # thinking, so it is left at provider defaults.
        if self.base_url and "moonshot.ai" in self.base_url and self.model == "kimi-k2.6":
            request["extra_body"] = {"thinking": {"type": "disabled"}}
        try:
            response = self._client.chat.completions.create(**request)
        except Exception as exc:
            raise WeatherSummaryGenerationError(
                f"OpenAI-compatible Chat Completions request failed ({type(exc).__name__})"
            ) from exc

        choices = _attribute(response, "choices") or []
        if not choices:
            raise WeatherSummaryGenerationError("chat completion response contained no choices")
        message = _attribute(choices[0], "message")
        text = (_attribute(message, "content") or "").strip()
        if not text:
            reasoning = _attribute(message, "reasoning_content") or ""
            raise WeatherSummaryGenerationError(
                "chat completion response contained no message content"
                + (f"; reasoning_chars={len(reasoning)}" if reasoning else "")
            )
        response_id = _attribute(response, "id")
        response_model = _attribute(response, "model") or self.model
        if not response_id:
            raise WeatherSummaryGenerationError("chat completion response did not include an id")

        return OpenAIEditorialResult(
            text=text,
            response_id=str(response_id),
            model=str(response_model),
            usage=_usage_from_response(response),
            prompt_version=PROMPT_VERSION,
            request_metadata=metadata,
        )


LiteralReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]


__all__ = [
    "DEFAULT_MODEL",
    "MissingOpenAIAPIKey",
    "OpenAIEditorialResult",
    "OpenAIUsage",
    "PROMPT_VERSION",
    "WeatherSummaryGenerationError",
    "WeatherSummaryOpenAIClient",
    "WeatherSummaryOpenAIError",
    "authorized_basin_accumulations",
    "authorized_rain_literal",
    "build_execution_message",
    "build_window_repair_message",
    "compact_editorial_packet",
    "expected_heading",
]
