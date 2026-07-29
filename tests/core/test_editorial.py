from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from editorial_samples import VALID_SUMMARY as VALID_OUTPUT
from pydantic import ValidationError

import resumo_meteo.core as weather_summary_core
from resumo_meteo.core.openai_client import (
    PROMPT_BUNDLE,
    PROMPT_VERSION,
    MissingOpenAIAPIKey,
    WeatherSummaryOpenAIClient,
    WeatherSummaryOpenAIError,
    build_execution_message,
    compact_editorial_packet,
    expected_heading,
    load_window_system_prompt,
)
from resumo_meteo.core.output_validator import (
    MM_SUPPORT_TOLERANCE,
    SummaryValidationError,
    validate_summary_output,
)
from resumo_meteo.core.schema import (
    AnalysisPacket,
    WindowId,
    analysis_packet_json_schema,
)
from resumo_meteo.core.summary_scope import DEFAULT_SUMMARY_BASIN_SCOPE

WEATHER_SUMMARY_DIR = Path(weather_summary_core.__file__).resolve().parent
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE_PATH = FIXTURE_ROOT / "analysis_packet_v2.json"
FULL_FIXTURE_PATH = FIXTURE_ROOT / "analysis_packet_v2_full.json"
JSON_SCHEMA_PATH = WEATHER_SUMMARY_DIR / "schemas" / "analysis_packet_v2.schema.json"


def load_fixture_dict() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def load_packet() -> AnalysisPacket:
    return AnalysisPacket.model_validate(load_fixture_dict())


class AnalysisPacketSchemaTests(TestCase):
    def test_degraded_fixture_is_valid_and_json_serializable(self):
        packet = load_packet()

        self.assertEqual(packet.schema_version, 2)
        self.assertEqual(len(packet.basins), 12)
        self.assertEqual(
            tuple(basin.basin_id for basin in packet.basins),
            DEFAULT_SUMMARY_BASIN_SCOPE.basin_ids,
        )
        self.assertEqual(AnalysisPacket.model_validate_json(packet.to_json()), packet)

    def test_full_fixture_covers_all_basins_observations_and_daily_timing(self):
        payload = json.loads(FULL_FIXTURE_PATH.read_text(encoding="utf-8"))
        packet = AnalysisPacket.model_validate(payload)

        self.assertEqual(len(packet.basins), 12)
        self.assertTrue(all(basin.area_km2 is not None for basin in packet.basins))
        self.assertTrue(all(basin.observed is not None for basin in packet.basins))
        self.assertTrue(all(basin.windows is not None for basin in packet.basins))
        self.assertTrue(
            all(
                len(window.daily_timing) == expected_days
                for basin in packet.basins
                for window, expected_days in (
                    (basin.windows.p1, 5),
                    (basin.windows.p2, 5),
                    (basin.windows.p3, 4),
                )
            )
        )
        self.assertTrue(
            all(
                basin.observed.rain_latest_partial_mm is not None
                and basin.observed.recent_daily
                for basin in packet.basins
            )
        )
        partial_source = next(
            source
            for source in packet.source_manifest
            if source.source_id == "gpm-imerg-early-30min-latest"
        )
        self.assertEqual(partial_source.status.value, "complete")

    def test_versioned_json_schema_matches_pydantic_contract(self):
        stored_schema = json.loads(JSON_SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertEqual(stored_schema, analysis_packet_json_schema())
        self.assertEqual(
            stored_schema["$id"],
            "urn:open-resumo-meteo:schema:analysis-packet:v2",
        )

    def test_packet_accepts_only_the_common_00_utc_cycle(self):
        fixture = load_fixture_dict()
        fixture["model_runs"]["gfs"]["cycle"] = "12"

        with self.assertRaises(ValidationError):
            AnalysisPacket.model_validate(fixture)

        fixture = load_fixture_dict()
        fixture["model_runs"]["ecens"]["init_time"] = "2026-07-09T00:00:00Z"
        with self.assertRaisesRegex(ValidationError, "mix different 00 UTC initializations"):
            AnalysisPacket.model_validate(fixture)

        fixture = load_fixture_dict()
        fixture["model_runs"]["gefs"]["init_time"] = "2026-07-10T06:00:00Z"
        with self.assertRaisesRegex(ValidationError, "00 UTC cycle"):
            AnalysisPacket.model_validate(fixture)

    def test_packet_requires_an_explicit_summary_basin_scope(self):
        fixture = load_fixture_dict()
        del fixture["quality"]["summary_basin_scope_id"]

        with self.assertRaisesRegex(ValidationError, "Field required"):
            AnalysisPacket.model_validate(fixture)

    def test_obsolete_packet_schema_is_rejected(self):
        fixture = load_fixture_dict()
        fixture["schema_version"] = 1

        with self.assertRaises(ValidationError):
            AnalysisPacket.model_validate(fixture)

    def test_forecast_windows_are_exactly_5_5_4_and_contiguous(self):
        fixture = load_fixture_dict()
        fixture["forecast_windows"][2]["duration_days"] = 5
        fixture["forecast_windows"][2]["end_utc"] = "2026-07-25T00:00:00Z"

        with self.assertRaises(ValidationError):
            AnalysisPacket.model_validate(fixture)

        fixture = load_fixture_dict()
        fixture["forecast_windows"][1]["start_utc"] = "2026-07-16T12:00:00Z"
        fixture["forecast_windows"][1]["end_utc"] = "2026-07-21T12:00:00Z"
        fixture["forecast_windows"][2]["start_utc"] = "2026-07-21T12:00:00Z"
        fixture["forecast_windows"][2]["end_utc"] = "2026-07-25T12:00:00Z"
        with self.assertRaisesRegex(ValidationError, "contiguous"):
            AnalysisPacket.model_validate(fixture)

    def test_synoptic_timeline_must_stay_inside_its_window(self):
        fixture = load_fixture_dict()
        fixture["synoptic_diagnostics"]["p1"]["timeline"][0]["end_utc"] = (
            "2026-07-16T00:00:00Z"
        )

        with self.assertRaisesRegex(ValidationError, "timeline must stay inside"):
            AnalysisPacket.model_validate(fixture)

    def test_ec_cannot_extend_past_240_hours_or_enter_p3(self):
        fixture = load_fixture_dict()
        fixture["model_runs"]["ec"]["max_lead_h"] = 241
        with self.assertRaisesRegex(ValidationError, "EC must never be extrapolated"):
            AnalysisPacket.model_validate(fixture)

        fixture = load_fixture_dict()
        available_basin = next(basin for basin in fixture["basins"] if basin["windows"])
        available_basin["windows"]["p3"]["ec"] = copy.deepcopy(
            available_basin["windows"]["p2"]["ec"]
        )
        with self.assertRaisesRegex(ValidationError, "must be null in p3"):
            AnalysisPacket.model_validate(fixture)

    def test_canonical_basin_id_and_name_are_paired(self):
        fixture = load_fixture_dict()
        fixture["basins"][0]["name"] = "Jacuí Total"

        with self.assertRaisesRegex(ValidationError, "canonical name"):
            AnalysisPacket.model_validate(fixture)

        fixture = load_fixture_dict()
        fixture["basins"][1]["editorial_reference"] = "em Uruguai"
        with self.assertRaisesRegex(ValidationError, "editorial reference"):
            AnalysisPacket.model_validate(fixture)

    def test_every_canonical_basin_has_complete_operational_geometry(self):
        packet = load_packet()
        unavailable = [basin for basin in packet.basins if basin.windows is None]

        self.assertEqual(unavailable, [])
        self.assertTrue(all(basin.area_km2 is not None for basin in packet.basins))
        self.assertTrue(all(not basin.geometry_limitations for basin in packet.basins))


    def test_only_authorized_source_providers_are_accepted(self):
        packet = load_packet()
        self.assertEqual(
            {source.provider for source in packet.source_manifest},
            {"NOAA", "ECMWF", "GPM_IMERG", "MERGE_INPE"},
        )

        fixture = load_fixture_dict()
        fixture["source_manifest"][0]["provider"] = "ANEEL"
        with self.assertRaises(ValidationError):
            AnalysisPacket.model_validate(fixture)


class OpenAIEditorialClientTests(TestCase):
    class FakeResponses:
        def __init__(self):
            self.request = None

        def create(self, **request):
            self.request = request
            usage = SimpleNamespace(
                model_dump=lambda: {
                    "input_tokens": 1234,
                    "output_tokens": 456,
                    "total_tokens": 1690,
                    "input_tokens_details": {"cached_tokens": 100},
                    "output_tokens_details": {"reasoning_tokens": 50},
                }
            )
            return SimpleNamespace(
                id="resp_fixture_123",
                model="gpt-5.6-terra",
                status="completed",
                output_text=VALID_OUTPUT,
                usage=usage,
            )

    def test_execution_message_is_versioned_and_contains_exact_headings(self):
        packet = load_packet()
        message = build_execution_message(packet, WindowId.P1)

        self.assertEqual(
            expected_heading(packet, WindowId.P1),
            "1ª pêntada (10/07 a 15/07):",
        )
        self.assertIn(packet.audit.packet_id, message)
        self.assertIn("Versão do schema: 2", message)
        self.assertIn("VALORES_AUTORIZADOS", message)
        self.assertIn("BRIEF_JSON", message)
        self.assertIn('"Grande"', message)
        self.assertIn('"ref":"no Grande"', message)
        self.assertNotIn("{{analysis_packet_json}}", message)
        self.assertNotIn('"source_manifest"', message)
        self.assertLess(len(message), 15000)
        self.assertNotIn("{{authorized_basin_accumulations}}", message)
        self.assertNotIn("{{prior_sections}}", message)
        self.assertIn("TRECHO_DE_ACUMULADOS_VALIDADO", message)
        self.assertIn("cenário mais seco", load_window_system_prompt())
        self.assertIn("aumento da temperatura", load_window_system_prompt())
        self.assertIn("narrativa espacial e temporal", load_window_system_prompt())
        self.assertIn("três a cinco frases", load_window_system_prompt())
        self.assertEqual(PROMPT_VERSION, "weather-summary-editorial-v27")

    def test_only_current_prompt_bundle_is_distributed(self):
        prompt_directory = WEATHER_SUMMARY_DIR / "prompts"
        prompt_files = {path.name for path in prompt_directory.glob("*.txt")}
        self.assertEqual(prompt_files, set(PROMPT_BUNDLE.values()))

    def test_compact_editorial_packet_excludes_raw_audit_sources(self):
        packet = load_packet()
        compact = compact_editorial_packet(packet, WindowId.P1)
        compact_json = json.dumps(compact, ensure_ascii=False)

        self.assertIn("basin_highlights", compact)
        self.assertIn("synoptic_brief", compact)
        self.assertIn("energy_signals", compact)
        self.assertNotIn("basins", compact)
        self.assertNotIn("synoptic_diagnostics", compact)
        self.assertNotIn("source_manifest", compact_json)
        self.assertNotIn("audit", compact_json)
        self.assertLess(len(compact_json), 12000)

    def test_compact_brief_carries_structured_weather_and_model_contrasts(self):
        payload = load_fixture_dict()
        for basin in payload["basins"]:
            if basin.get("windows") is None:
                continue
            basin["windows"]["p1"]["gfs"]["raw_basin_mean_mm"] = 100.0
            basin["windows"]["p1"]["gfs"]["corrected_basin_mean_mm"] = 100.0
        compact = compact_editorial_packet(
            AnalysisPacket.model_validate(payload),
            WindowId.P1,
        )
        synoptic = compact["synoptic_brief"]["p1"]

        self.assertEqual(
            synoptic["structured_features"][0]["name"],
            "frente fria",
        )
        self.assertEqual(
            synoptic["temperature_evolution"][0]["trend"],
            "warming",
        )
        self.assertIn(
            "model_views",
            compact["basin_highlights"]["p1"][0],
        )
        confidence = compact["confidence_summary"]["p1"]
        self.assertIn(confidence["overall"], {"alta", "moderada", "baixa"})
        if confidence.get("reason") == "model_dispersion":
            self.assertIn("model_contrasts", confidence)

    def test_model_contrast_requires_both_independent_families(self):
        payload = load_fixture_dict()
        for basin in payload["basins"]:
            if basin.get("windows") is None:
                continue
            basin["windows"]["p1"]["ec"] = None
            basin["windows"]["p1"]["ecens"] = None

        compact = compact_editorial_packet(
            AnalysisPacket.model_validate(payload),
            WindowId.P1,
        )

        for highlight in compact["basin_highlights"]["p1"]:
            self.assertEqual(highlight["model_contrast"], "not_available")
            self.assertNotIn("model_views", highlight)

    def test_window_brief_carries_observed_and_cross_window_timing_compactly(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        for basin in payload["basins"]:
            if basin.get("observed") is None or basin.get("windows") is None:
                continue
            basin["observed"].update(
                {
                    "latest_partial_start_utc": "2026-07-10T00:00:00Z",
                    "latest_partial_end_utc": "2026-07-10T07:00:00Z",
                    "rain_latest_partial_mm": 8.0,
                    "latest_partial_coverage_pct": 100.0,
                    "recent_daily": [
                        {
                            "date": "2026-07-09",
                            "start_utc": "2026-07-09T00:00:00Z",
                            "end_utc": "2026-07-10T00:00:00Z",
                            "basin_mean_mm": 12.0,
                            "coverage_pct": 100.0,
                            "complete": True,
                        }
                    ],
                }
            )
            for window_id, dates in {
                "p1": range(11, 16),
                "p2": range(16, 21),
                "p3": range(21, 25),
            }.items():
                basin["windows"][window_id]["daily_timing"] = [
                    {
                        "date": f"2026-07-{day:02d}",
                        "gfs_basin_mean_mm": 10.0,
                        "ec_basin_mean_mm": (10.0 if window_id != "p3" else None),
                        "deterministic_center_mm": 10.0,
                        "family_difference_mm": (0.0 if window_id != "p3" else None),
                        "confidence": ("high" if window_id != "p3" else "low"),
                    }
                    for day in dates
                ]
        packet = AnalysisPacket.model_validate(payload)

        compact = compact_editorial_packet(packet, WindowId.P1)
        message = build_execution_message(packet, WindowId.P1)

        observed = compact["observed_context"]["basins"][0]
        evolution = compact["forecast_evolution"]["rows"][0]
        self.assertEqual(observed["latest_partial_day_regime"], "chuva_moderada")
        self.assertEqual(observed["recent_trend"], "episódio_isolado_ou_série_curta")
        self.assertNotIn("recent_daily_mm", observed)
        self.assertEqual(
            len(evolution["requested_daily_timing_regimes"]["p1"]),
            5,
        )
        self.assertEqual(
            set(evolution["timing_pattern_by_window"]),
            {"p1", "p2", "p3"},
        )
        self.assertNotIn("window_opinion_mm", evolution)
        self.assertNotIn("deterministic_center_mm", message)
        self.assertNotIn('"basin_mean_mm":12.0', message)
        self.assertIn("synoptic_sequence", compact)
        self.assertLess(len(message), 16500)

    def test_responses_api_request_disables_storage_and_returns_audit_fields(self):
        fake_responses = self.FakeResponses()
        fake_client = SimpleNamespace(responses=fake_responses)
        client = WeatherSummaryOpenAIClient(
            client=fake_client,
            model="gpt-5.6-terra",
            api_mode="responses",
            timeout_seconds=90,
            max_attempts=2,
            max_output_tokens=3000,
        )

        result = client.generate(
            load_packet(),
            window_id=WindowId.P1,
            audit_metadata={"job_run_id": "job-42"},
        )

        self.assertFalse(fake_responses.request["store"])
        self.assertEqual(fake_responses.request["model"], "gpt-5.6-terra")
        self.assertEqual(fake_responses.request["metadata"]["prompt_version"], PROMPT_VERSION)
        self.assertEqual(fake_responses.request["metadata"]["job_run_id"], "job-42")
        self.assertEqual(result.text, VALID_OUTPUT)
        self.assertEqual(result.response_id, "resp_fixture_123")
        self.assertEqual(result.usage.total_tokens, 1690)
        self.assertEqual(result.usage.cached_input_tokens, 100)

    def test_default_client_uses_sabia_chat_completions_configuration(self):
        client = WeatherSummaryOpenAIClient(client=SimpleNamespace())

        self.assertEqual(client.model, "sabia-4")
        self.assertEqual(client.base_url, "https://chat.maritaca.ai/api")
        self.assertEqual(client.api_mode, "chat_completions")

    def test_api_key_is_required_without_an_injected_client(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingOpenAIAPIKey):
                WeatherSummaryOpenAIClient()

    def test_sensitive_values_cannot_be_smuggled_into_audit_metadata(self):
        fake_client = SimpleNamespace(responses=self.FakeResponses())
        client = WeatherSummaryOpenAIClient(client=fake_client)

        with self.assertRaisesRegex(WeatherSummaryOpenAIError, "sensitive metadata key"):
            client.generate(
                load_packet(),
                window_id=WindowId.P1,
                audit_metadata={"api_key_copy": "never"},
            )


class SummaryOutputValidatorTests(TestCase):
    def setUp(self):
        self.packet = load_packet()

    def assert_invalid(self, text: str, code: str):
        with self.assertRaises(SummaryValidationError) as raised:
            validate_summary_output(text, self.packet)
        self.assertEqual(raised.exception.code, code)

    def test_accepts_exactly_three_sections_with_one_paragraph_each(self):
        validated = validate_summary_output(VALID_OUTPUT, self.packet)

        self.assertEqual(len(validated.sections), 3)
        self.assertEqual(validated.sections[2].window_id, "p3")
        self.assertEqual(validated.text, VALID_OUTPUT)
        self.assertEqual(validated.mm_support_tolerance, MM_SUPPORT_TOLERANCE)
        self.assertIn("Uruguai", validated.sections[0].canonical_basin_names)
        self.assertEqual(validated.sections[0].mm_values, [20.0, 25.0])

    def test_rejects_preamble_footer_wrong_dates_and_extra_paragraph(self):
        variants = (
            "Resumo meteorológico\n" + VALID_OUTPUT,
            VALID_OUTPUT + "\n\nConclusão adicional.",
            VALID_OUTPUT.replace("1ª pêntada (10/07 a 15/07):", "1ª pêntada (11/07 a 15/07):"),
            VALID_OUTPUT.replace("O vento permanece", "Um parágrafo extra.\n\nO vento permanece"),
        )
        for variant in variants:
            with self.subTest(variant=variant[:45]):
                self.assert_invalid(variant, "invalid_structure")

    def test_rejects_bullets_tables_and_markdown(self):
        paragraph = VALID_OUTPUT.splitlines()[1]
        variants = (
            VALID_OUTPUT.replace(paragraph, "- " + paragraph, 1),
            VALID_OUTPUT.replace(paragraph, "Bacia | Chuva", 1),
            VALID_OUTPUT.replace(paragraph, "**" + paragraph + "**", 1),
        )
        expected_codes = ("list_not_allowed", "table_not_allowed", "markdown_not_allowed")
        for variant, code in zip(variants, expected_codes):
            with self.subTest(code=code):
                self.assert_invalid(variant, code)

    def test_rejects_malformed_consecutive_punctuation(self):
        self.assert_invalid(
            VALID_OUTPUT.replace("25 mm no Iguaçu.", "25 mm no Iguaçu.,", 1),
            "malformed_punctuation",
        )

    def test_rejects_sentence_starting_with_lowercase_letter(self):
        self.assert_invalid(
            VALID_OUTPUT.replace(
                "A confiança é moderada.",
                "A transição termina. a confiança é moderada.",
                1,
            ),
            "lowercase_sentence_start",
        )

    def test_rejects_internal_operational_quality_terms(self):
        paragraph = VALID_OUTPUT.splitlines()[1]
        with_quality_metadata = VALID_OUTPUT.replace(
            paragraph,
            paragraph + " A qualidade operacional tem ressalva nesta janela.",
            1,
        )

        self.assert_invalid(
            with_quality_metadata,
            "reader_quality_metadata_not_allowed",
        )

    def test_normalizes_internal_family_agreement_to_reader_language(self):
        paragraph = VALID_OUTPUT.splitlines()[4]
        internal = VALID_OUTPUT.replace(
            paragraph,
            paragraph + " A confiança decorre da boa concordância entre as famílias.",
            1,
        )

        validated = validate_summary_output(internal, self.packet)

        self.assertIn("consistência do sinal", validated.text)
        self.assertNotIn("famílias", validated.text)

    def test_normalizes_invalid_locative_after_atuar_sobre(self):
        malformed = VALID_OUTPUT.replace(
            "Um cavado organiza a chuva no Sul,",
            "Um cavado atua sobre no Uruguai e organiza a chuva no Sul,",
            1,
        )

        validated = validate_summary_output(malformed, self.packet)

        self.assertIn("atua no Uruguai", validated.text)
        self.assertNotIn("atua sobre no Uruguai", validated.text)

    def test_normalizes_generic_ungrammatical_sobre_no_locative(self):
        malformed = VALID_OUTPUT.replace(
            "organiza a chuva no Sul",
            "organiza a chuva sobre no Uruguai",
            1,
        )

        validated = validate_summary_output(malformed, self.packet)

        self.assertIn("organiza a chuva no Uruguai", validated.text)
        self.assertNotIn("sobre no Uruguai", validated.text)

    def test_normalizes_entre_before_a_basin_locative(self):
        malformed = VALID_OUTPUT.replace(
            "sinal amplo nas bacias disponíveis",
            "sinal amplo entre no Uruguai e no Iguaçu",
            1,
        )

        validated = validate_summary_output(malformed, self.packet)

        self.assertIn("sinal amplo no Uruguai e no Iguaçu", validated.text)
        self.assertNotIn("entre no Uruguai", validated.text)

    def test_normalizes_mechanism_absence_hallucination_to_weather_description(self):
        malformed = VALID_OUTPUT.replace(
            "O vento permanece favorável no Nordeste.",
            "Essa evolução decorre da ausência de sistemas organizados na janela atual e da persistência apenas de distribuição e persistência de umidade. O vento permanece favorável no Nordeste.",
            1,
        )

        validated = validate_summary_output(malformed, self.packet)

        self.assertIn("A umidade permanece restrita e distribuída de forma irregular.", validated.text)
        self.assertNotIn("ausência de sistemas organizados", validated.text)

    def test_normalizes_ungrammatical_dry_condition_locative(self):
        malformed = VALID_OUTPUT.replace(
            "O vento permanece favorável no Nordeste.",
            "O padrão mantém no Iguaçu sob condição seca, enquanto o vento permanece favorável no Nordeste.",
            1,
        )

        validated = validate_summary_output(malformed, self.packet)

        self.assertIn("mantém condição seca no Iguaçu", validated.text)
        self.assertNotIn("mantém no Iguaçu sob condição seca", validated.text)

    def test_rejects_hydrological_outcome_without_hydrological_input(self):
        paragraph = VALID_OUTPUT.splitlines()[1]
        with_hydrology = VALID_OUTPUT.replace(
            paragraph,
            paragraph + " O volume previsto eleva a vazão e a recarga da bacia.",
            1,
        )

        self.assert_invalid(with_hydrology, "unsupported_hydrological_claim")

        with_vague_hydrology = VALID_OUTPUT.replace(
            "precipitação irregular",
            "atividade hídrica irregular",
            1,
        )
        self.assert_invalid(with_vague_hydrology, "unsupported_hydrological_claim")

    def test_normalizes_common_soil_state_overclaim_to_observed_rain(self):
        overclaimed = VALID_OUTPUT.replace(
            "A tendência é de enfraquecimento do sistema,",
            "O antecedente de encharcamento recente é seguido por enfraquecimento do sistema,",
            1,
        )

        validated = validate_summary_output(overclaimed, self.packet)

        self.assertIn("período chuvoso recente", validated.text)
        self.assertNotIn("encharcamento", validated.text)

    def test_requires_uncertainty_treatment_in_every_section(self):
        without_confidence = VALID_OUTPUT.replace(
            "com baixa confiança no posicionamento diário e precipitação irregular",
            "com posicionamento diário variável e precipitação irregular",
            1,
        )

        self.assert_invalid(
            without_confidence,
            "confidence_language_missing",
        )

    def test_rejects_noncanonical_basin_names(self):
        self.assert_invalid(VALID_OUTPUT.replace("Iguaçu", "Iguacu", 1), "noncanonical_basin_alias")

    def test_normalizes_sector_locative_reference_for_every_named_basin(self):
        variants = (
            VALID_OUTPUT.replace("no Uruguai", "em Uruguai", 1),
            VALID_OUTPUT.replace("no Uruguai", "na Uruguai", 1),
        )
        for variant in variants:
            with self.subTest(variant=variant.splitlines()[1][-70:]):
                validated = validate_summary_output(variant, self.packet)
                self.assertIn("no Uruguai", validated.text)

        sentence_initial = VALID_OUTPUT.replace(
            "Um cavado organiza a chuva no Sul,",
            "No Uruguai, um cavado organiza a chuva no Sul,",
            1,
        )
        validate_summary_output(sentence_initial, self.packet)

    def test_normalizes_article_before_complete_locative_reference(self):
        validated = validate_summary_output(
            VALID_OUTPUT.replace("no Uruguai", "o no Uruguai", 1),
            self.packet,
        )

        self.assertIn("no Uruguai", validated.text)
        self.assertNotIn("o no Uruguai", validated.text)

        contracted = validate_summary_output(
            VALID_OUTPUT.replace("no Uruguai", "ao no Uruguai", 1),
            self.packet,
        )
        self.assertIn("no Uruguai", contracted.text)
        self.assertNotIn("ao no Uruguai", contracted.text)

        para_contracted = validate_summary_output(
            VALID_OUTPUT.replace("no Uruguai", "para no Uruguai", 1),
            self.packet,
        )
        self.assertIn("no Uruguai", para_contracted.text)
        self.assertNotIn("para no Uruguai", para_contracted.text)

        mismatched_contraction = validate_summary_output(
            VALID_OUTPUT.replace("no Uruguai", "às no Uruguai", 1),
            self.packet,
        )
        self.assertIn("no Uruguai", mismatched_contraction.text)
        self.assertNotIn("às no Uruguai", mismatched_contraction.text)

    def test_normalizes_bacia_do_construction_without_double_locative(self):
        validated = validate_summary_output(
            VALID_OUTPUT.replace("no Uruguai", "nas bacias do Uruguai", 1),
            self.packet,
        )

        self.assertIn("no Uruguai", validated.text)
        self.assertNotIn("bacias no Uruguai", validated.text)

    def test_normalizes_locative_reference_used_as_sentence_subject(self):
        paragraph = VALID_OUTPUT.splitlines()[1]
        malformed = VALID_OUTPUT.replace(
            paragraph,
            paragraph.replace(
                "A confiança é moderada.",
                "No Uruguai mantém chuva persistente. A confiança é moderada.",
            ),
            1,
        )

        validated = validate_summary_output(malformed, self.packet)

        self.assertIn("Mantém-se chuva persistente no Uruguai", validated.text)
        self.assertNotIn("No Uruguai mantém", validated.text)

    def test_rejects_locative_subject_with_lists_and_forecast_verbs(self):
        variants = (
            VALID_OUTPUT.replace(
                "Os ensembles sustentam apenas um regime de umidade tropical,",
                "No Jacuí, no Iguaçu e no Uruguai seguem com chuva irregular,",
                1,
            ),
            VALID_OUTPUT.replace(
                "A tendência é de enfraquecimento do sistema,",
                "Na metade final, no Uruguai tende à secagem,",
                1,
            ),
            VALID_OUTPUT.replace(
                "precipitação irregular nas áreas avaliadas.",
                "precipitação irregular nas áreas avaliadas, enquanto no Paraná concentra chuva.",
                1,
            ),
            VALID_OUTPUT.replace(
                "precipitação irregular nas áreas avaliadas.",
                "precipitação irregular nas áreas avaliadas, com no Paraná finalizando o período seco.",
                1,
            ),
        )

        for variant in variants:
            with self.subTest(paragraph=variant.splitlines()[1][:80]):
                self.assert_invalid(variant, "locative_reference_used_as_subject")

    def test_rejects_adjacent_basin_locatives_without_connector(self):
        self.assert_invalid(
            VALID_OUTPUT.replace("no Uruguai e 25 mm", "no Uruguai no Iguaçu e 25 mm", 1),
            "adjacent_basin_references",
        )

    def test_rejects_invalid_number_agreement(self):
        self.assert_invalid(
            VALID_OUTPUT.replace(
                "A confiança é moderada.",
                "As bacias permanece secas. A confiança é moderada.",
                1,
            ),
            "invalid_number_agreement",
        )

    def test_normalizes_bare_basin_name_to_sector_editorial_reference(self):
        bare_name = VALID_OUTPUT.replace("no Iguaçu", "Iguaçu", 1)

        validated = validate_summary_output(bare_name, self.packet)

        self.assertIn("no Iguaçu", validated.text)
        self.assertNotIn("  ", validated.text)

    def test_basin_reference_normalization_converges_before_publication(self):
        malformed = VALID_OUTPUT.replace(
            "Um cavado organiza a chuva",
            "Nas soluções para a bacia do São Francisco, um cavado organiza a chuva",
            1,
        )

        validated = validate_summary_output(malformed, self.packet)

        self.assertIn("Nas soluções no São Francisco", validated.text)
        self.assertNotIn("para no São Francisco", validated.text)
        self.assertEqual(
            validate_summary_output(validated.text, self.packet).text,
            validated.text,
        )

    def test_point_maximum_is_never_an_authorized_editorial_accumulation(self):
        original = VALID_OUTPUT.splitlines()[1]
        point_paragraph = (
            "Um cavado organiza chuva ampla e persistente no Sul durante a maior parte "
            "da janela, com deslocamento gradual para leste e confiança moderada no timing. "
            "O máximo pontual no Uruguai chega a 50 mm, enquanto o acumulado médio por bacia "
            "fica em 20 mm no Uruguai, sem confundir o pico localizado com a média espacial."
        )
        self.assert_invalid(
            VALID_OUTPUT.replace(original, point_paragraph, 1),
            "unsupported_mm_value",
        )

        no_point_label = (
            "Um cavado organiza chuva ampla no Sul. No Uruguai, o máximo chega a 50 mm, "
            "enquanto o acumulado médio por bacia fica em 20 mm no Uruguai."
        )
        self.assert_invalid(
            VALID_OUTPUT.replace(original, no_point_label, 1),
            "point_max_not_labeled",
        )

        no_mean_label = (
            "Um cavado organiza chuva ampla no Sul. O máximo pontual no Uruguai chega a "
            "50 mm e o acumulado chega a 20 mm no Uruguai."
        )
        self.assert_invalid(
            VALID_OUTPUT.replace(original, no_mean_label, 1),
            "basin_mean_not_labeled",
        )

    def test_rejects_value_supported_only_by_another_basin_point_maximum(self):
        original = VALID_OUTPUT.splitlines()[1]
        misattributed = (
            "A chuva prevista fica concentrada no Sul ao longo da janela, com distribuição "
            "irregular e baixa confiança no detalhamento diário por ausência de evidência "
            "sinótica objetiva. Considerando somente a área com contorno disponível, o "
            "acumulado médio por bacia chegaria a 75 mm no Uruguai, mantendo sinal persistente."
        )

        self.assert_invalid(
            VALID_OUTPUT.replace(original, misattributed, 1),
            "unsupported_mm_value",
        )

    def test_mm_values_are_bound_to_their_own_basin_in_the_clause(self):
        original = VALID_OUTPUT.splitlines()[1]
        supported = (
            "Um cavado mantém chuva ampla no Sul durante a janela, com persistência maior "
            "na metade final do período e confiança moderada na distribuição espacial. "
            "Considerando os contornos operacionais, os acumulados médios por bacia ficam "
            "em 15 mm no Uruguai e 30 mm no Iguaçu. Na metade inicial, a distribuição "
            "mantém maior continuidade, antes de perder regularidade no fim do período, "
            "sem alterar o sinal espacial nas áreas citadas e mantendo o detalhamento "
            "diário sob maior incerteza espacial."
        )
        validate_summary_output(VALID_OUTPUT.replace(original, supported, 1), self.packet)

        swapped = supported.replace(
            "15 mm no Uruguai e 30 mm no Iguaçu",
            "30 mm no Uruguai e 15 mm no Iguaçu",
        )
        self.assert_invalid(
            VALID_OUTPUT.replace(original, swapped, 1),
            "unsupported_mm_value",
        )

    def test_prior_basin_mentions_do_not_capture_explicit_later_values(self):
        original = VALID_OUTPUT.splitlines()[1]
        explicit_values = (
            "Um cavado mantém chuva ampla no Sul durante a janela, com o Uruguai e o "
            "Iguaçu como áreas de maior atenção e confiança moderada na distribuição "
            "espacial. Considerando os contornos operacionais, os acumulados médios por "
            "bacia ficam em 20 mm no Uruguai e 25 mm no Iguaçu. Na metade inicial, a "
            "distribuição mantém maior continuidade, antes de perder regularidade no fim "
            "do período, sem alterar o sinal espacial nas áreas citadas e mantendo o "
            "detalhamento diário sob maior incerteza espacial."
        )

        validate_summary_output(
            VALID_OUTPUT.replace(original, explicit_values, 1), self.packet
        )

    def test_one_supported_mm_value_can_govern_a_short_basin_list(self):
        shared_value = VALID_OUTPUT.replace(
            "20 mm no Uruguai e 25 mm no Iguaçu",
            "20 mm no Uruguai e no Iguaçu",
            1,
        )

        validated = validate_summary_output(shared_value, self.packet)

        self.assertIn("20 mm no Uruguai e no Iguaçu", validated.text)

    def test_confidence_basin_does_not_capture_preceding_mm_value(self):
        same_clause = VALID_OUTPUT.replace(
            "25 mm no Iguaçu.",
            "25 mm no Iguaçu, com confiança moderada no sinal do Uruguai.",
            1,
        )

        validate_summary_output(same_clause, self.packet)

    def test_basin_first_value_predicate_binds_to_the_following_mm(self):
        paragraph = VALID_OUTPUT.splitlines()[4]
        basin_first = (
            "A tendência é de enfraquecimento do sistema, com maior incerteza "
            "espacial e chuva irregular no Centro-Sul. O calor aumenta no Sudeste. "
            "Nesta configuração, há 25 mm no Uruguai, enquanto a chuva no Iguaçu "
            "apresenta regime intermitente, chegando a 30 mm; também há 35 mm "
            "no Paranapanema. A confiança permanece moderada na janela. Na metade inicial, "
            "a distribuição mantém maior continuidade, antes de perder regularidade no "
            "fim do período, sem alterar o sinal espacial nas áreas citadas e mantendo o "
            "detalhamento diário sob maior incerteza espacial."
        )

        validated = validate_summary_output(
            VALID_OUTPUT.replace(paragraph, basin_first, 1),
            self.packet,
        )

        self.assertIn("chegando a 30 mm", validated.text)

    def test_rejects_ambiguous_mixed_confidence_language(self):
        self.assert_invalid(
            VALID_OUTPUT.replace("A confiança é moderada.", "A confiança é mista.", 1),
            "ambiguous_confidence_language",
        )

    def test_rejects_vague_model_group(self):
        self.assert_invalid(
            VALID_OUTPUT.replace("Os ensembles", "Os sistemas europeus", 1),
            "vague_model_group",
        )

    def test_rejects_raw_model_value_even_when_close_to_editorial_value(self):
        self.assert_invalid(
            VALID_OUTPUT.replace("20 mm no Uruguai", "21 mm no Uruguai", 1),
            "unsupported_mm_value",
        )

    def test_rejects_redundant_decimal_for_integer_mm(self):
        self.assert_invalid(
            VALID_OUTPUT.replace("20 mm no Uruguai", "20.0 mm no Uruguai", 1),
            "integer_mm_has_decimal",
        )

    def test_rejects_truncated_final_clause_even_with_period(self):
        truncated = VALID_OUTPUT.replace(
            "35 mm no Iguaçu.",
            "35 mm no Iguaçu, dado qu.",
            1,
        )

        self.assert_invalid(truncated, "incomplete_sentence_ending")

    def test_rejects_named_mechanism_when_synoptic_fields_are_unavailable(self):
        packet = copy.deepcopy(load_fixture_dict())
        packet["synoptic_diagnostics"]["p1"] = {
            "dominant_pattern": "indisponível",
            "timeline": [],
            "surface": {"summary": "indisponível", "signals": [], "supported_by_families": [], "confidence": "low", "source_fields": []},
            "low_levels_850_hpa": {"summary": "indisponível", "signals": [], "supported_by_families": [], "confidence": "low", "source_fields": []},
            "mid_levels_500_hpa": {"summary": "indisponível", "signals": [], "supported_by_families": [], "confidence": "low", "source_fields": []},
            "upper_levels_200_250_hpa": {"summary": "indisponível", "signals": [], "supported_by_families": [], "confidence": "low", "source_fields": []},
            "supported_features": [],
            "family_agreement": "not_available",
            "uncertainty": "indisponível",
        }

        with self.assertRaises(SummaryValidationError) as raised:
            validate_summary_output(VALID_OUTPUT, AnalysisPacket.model_validate(packet))
        self.assertEqual(raised.exception.code, "unsupported_synoptic_mechanism")

        convective_output = VALID_OUTPUT.replace(
            "Um cavado organiza a chuva",
            "A atividade convectiva organiza a chuva",
            1,
        )
        with self.assertRaises(SummaryValidationError) as raised:
            validate_summary_output(
                convective_output,
                AnalysisPacket.model_validate(packet),
            )
        self.assertEqual(raised.exception.code, "unsupported_synoptic_mechanism")

    def test_removes_announcement_that_no_synoptic_mechanism_exists(self):
        paragraph = VALID_OUTPUT.splitlines()[1]
        announced_absence = VALID_OUTPUT.replace(
            paragraph,
            paragraph + " Não há mecanismo sinótico nomeado nesta janela.",
            1,
        )

        validated = validate_summary_output(
            announced_absence,
            self.packet,
        )
        self.assertNotIn("Não há mecanismo", validated.text)

    def test_rejects_generic_announcement_of_no_active_structure(self):
        invalid = VALID_OUTPUT.replace(
            "Os ensembles sustentam apenas um regime de umidade tropical",
            "O padrão sinótico mantém apenas distribuição e persistência, sem estruturas ativas",
            1,
        )

        self.assert_invalid(invalid, "mechanism_absence_not_allowed")

    def test_removing_mechanism_absence_does_not_truncate_preceding_word(self):
        paragraph = VALID_OUTPUT.splitlines()[7]
        announced_absence = VALID_OUTPUT.replace(
            paragraph,
            paragraph.replace(
                "Os ensembles sustentam",
                "O período transcorre sem sistemas sinóticos estruturados. "
                "Os ensembles sustentam",
                1,
            ),
            1,
        )

        validated = validate_summary_output(announced_absence, self.packet)

        self.assertIn("O período transcorre.", validated.text)
        self.assertNotIn("transcorr.", validated.text)
        self.assertNotIn("sem sistemas sinóticos estruturados", validated.text)

    def test_removes_negative_model_contrast_disclaimer_without_evidence(self):
        packet = copy.deepcopy(load_fixture_dict())
        for basin in packet["basins"]:
            if basin.get("windows") is None:
                continue
            basin["windows"]["p1"]["ec"] = None
            basin["windows"]["p1"]["ecens"] = None
        paragraph = VALID_OUTPUT.splitlines()[1]
        with_disclaimer = VALID_OUTPUT.replace(
            paragraph,
            paragraph[:-1] + ", sem contraste relevante entre modelos.",
            1,
        )

        validated = validate_summary_output(
            with_disclaimer,
            AnalysisPacket.model_validate(packet),
        )

        self.assertNotIn("contraste relevante entre modelos", validated.text)

    def test_generic_field_ids_do_not_authorize_a_named_synoptic_mechanism(self):
        packet = copy.deepcopy(load_fixture_dict())
        packet["synoptic_diagnostics"]["p1"]["supported_features"] = [
            "mean_sea_level_pressure",
            "wind_u_850",
            "geopotential_500",
        ]

        with self.assertRaises(SummaryValidationError) as raised:
            validate_summary_output(VALID_OUTPUT, AnalysisPacket.model_validate(packet))
        self.assertEqual(raised.exception.code, "unsupported_synoptic_mechanism")

    def test_objective_pressure_classifier_authorizes_only_its_named_feature(self):
        packet = copy.deepcopy(load_fixture_dict())
        packet["synoptic_diagnostics"]["p1"]["supported_features"] = ["baixa pressão"]
        packet["synoptic_diagnostics"]["p1"]["dominant_pattern"] = (
            "Extremo objetivo de baixa pressão ao nível do mar."
        )
        pressure_output = VALID_OUTPUT.replace("Um cavado", "Uma baixa pressão", 1)

        validate_summary_output(pressure_output, AnalysisPacket.model_validate(packet))

        with self.assertRaises(SummaryValidationError) as raised:
            validate_summary_output(VALID_OUTPUT, AnalysisPacket.model_validate(packet))
        self.assertEqual(raised.exception.code, "unsupported_synoptic_mechanism")

    def test_material_energy_claim_requires_signal_in_the_same_window(self):
        cases = []

        without_wind = copy.deepcopy(load_fixture_dict())
        without_wind["energy_signals"]["wind"] = []
        cases.append((without_wind, VALID_OUTPUT))

        without_temperature = copy.deepcopy(load_fixture_dict())
        without_temperature["energy_signals"]["temperature"] = []
        cases.append((without_temperature, VALID_OUTPUT))

        solar_claim = VALID_OUTPUT.replace(
            "Os ensembles sustentam",
            "A geração solar permanece favorecida nas áreas avaliadas. Os ensembles sustentam",
            1,
        )
        cases.append((copy.deepcopy(load_fixture_dict()), solar_claim))

        without_severe = copy.deepcopy(load_fixture_dict())
        without_severe["energy_signals"]["severe_weather"] = []
        severe_claim = VALID_OUTPUT.replace(
            "O vento permanece favorável no Nordeste.",
            "Há risco de tempestades severas no Sul. O vento permanece favorável no Nordeste.",
            1,
        )
        cases.append((without_severe, severe_claim))

        wrong_window = VALID_OUTPUT.replace(
            "O calor aumenta no Sudeste.",
            "O calor aumenta no Sudeste e o vento permanece favorável no Nordeste.",
            1,
        )
        cases.append((copy.deepcopy(load_fixture_dict()), wrong_window))

        for packet, text in cases:
            with self.subTest(text=text.splitlines()[1][:80]):
                with self.assertRaises(SummaryValidationError) as raised:
                    validate_summary_output(text, AnalysisPacket.model_validate(packet))
                self.assertEqual(raised.exception.code, "unsupported_energy_claim")

    def test_structured_severe_signal_authorizes_conservative_severe_wording(self):
        severe_output = VALID_OUTPUT.replace(
            "O vento permanece favorável no Nordeste.",
            (
                "Há potencial de tempestades severas no Sul, ainda condicionado aos "
                "demais ingredientes convectivos. O vento permanece favorável no Nordeste."
            ),
            1,
        )

        validate_summary_output(severe_output, self.packet)

    def test_wind_in_rain_dynamics_is_not_a_false_sector_claim(self):
        packet = copy.deepcopy(load_fixture_dict())
        packet["energy_signals"]["wind"] = []
        rain_dynamics = VALID_OUTPUT.replace(
            "O vento permanece favorável no Nordeste.",
            "O vento em baixos níveis transporta umidade e ajuda a organizar a chuva no Sul.",
            1,
        )

        validate_summary_output(rain_dynamics, AnalysisPacket.model_validate(packet))

    def test_requires_available_basin_mm_value_and_minimum_content(self):
        original = VALID_OUTPUT.splitlines()[1]
        without_available_basin = original.replace("Uruguai", "setor sul").replace(
            "Iguaçu", "setor vizinho"
        )
        self.assert_invalid(
            VALID_OUTPUT.replace(original, without_available_basin, 1),
            "available_basin_missing",
        )

        without_mm = original.replace("20 mm", "vinte milímetros").replace(
            "25 mm", "vinte e cinco milímetros"
        )
        self.assert_invalid(
            VALID_OUTPUT.replace(original, without_mm, 1),
            "basin_accumulation_missing",
        )

        too_short = (
            "No Uruguai, o acumulado médio da bacia fica em 20 mm, "
            "com confiança alta."
        )
        self.assert_invalid(
            VALID_OUTPUT.replace(original, too_short, 1),
            "paragraph_too_short",
        )

    def test_rejects_mm_value_without_numeric_support_in_packet_window(self):
        self.assert_invalid(
            VALID_OUTPUT.replace("20 mm", "999 mm", 1),
            "unsupported_mm_value",
        )

    def test_rejects_paragraphs_longer_than_170_words(self):
        original = VALID_OUTPUT.splitlines()[1]
        too_long = ("chuva " * 100) + original

        self.assert_invalid(
            VALID_OUTPUT.replace(original, too_long, 1),
            "paragraph_too_long",
        )

    def test_third_window_quantification_does_not_repeat_its_duration(self):
        validate_summary_output(VALID_OUTPUT, self.packet)
        self.assert_invalid(
            VALID_OUTPUT.replace("acumulados médios por bacia ficam", "acumulados médios por bacia em quatro dias ficam"),
            "third_window_is_five_days",
        )

    def test_allows_authorized_models_and_rejects_unauthorized_models(self):
        payload = load_fixture_dict()
        for basin in payload["basins"]:
            if basin.get("windows") is None:
                continue
            basin["windows"]["p1"]["gfs"]["raw_basin_mean_mm"] = 100.0
            basin["windows"]["p1"]["gfs"]["corrected_basin_mean_mm"] = 100.0
        packet_with_contrast = AnalysisPacket.model_validate(payload)
        with_gfs = VALID_OUTPUT.replace(
            "Um cavado",
            "Segundo o GFS, um cavado",
            1,
        )
        self.assertIn(
            "GFS",
            validate_summary_output(with_gfs, packet_with_contrast).text,
        )
        self.assert_invalid(
            VALID_OUTPUT.replace("Os ensembles", "O ICON e os ensembles"),
            "model_name_not_allowed",
        )

    def test_comparative_frente_a_is_not_a_synoptic_front(self):
        payload = load_fixture_dict()
        for basin in payload["basins"]:
            if basin.get("windows") is None:
                continue
            basin["windows"]["p2"]["gfs"]["raw_basin_mean_mm"] = 5.0
            basin["windows"]["p2"]["gfs"]["corrected_basin_mean_mm"] = 5.0
            basin["windows"]["p2"]["gefs"].update(
                p10_mm=1.0,
                p25_mm=3.0,
                p50_mm=5.0,
                p75_mm=7.0,
                p90_mm=10.0,
            )
            basin["windows"]["p2"]["ec"]["raw_basin_mean_mm"] = 200.0
            basin["windows"]["p2"]["ec"]["corrected_basin_mean_mm"] = 200.0
        packet_with_contrast = AnalysisPacket.model_validate(payload)
        paragraph = VALID_OUTPUT.splitlines()[4]
        comparison = VALID_OUTPUT.replace(
            paragraph,
            paragraph
            + " O EC indica sinal mais úmido frente à solução mais seca do GFS.",
            1,
        )

        validated = validate_summary_output(comparison, packet_with_contrast)

        self.assertIn("frente à solução", validated.text)

    def test_developing_cross_window_system_requires_authorized_start_date(self):
        payload = load_fixture_dict()
        payload["synoptic_diagnostics"]["p1"]["supported_features"].append(
            "baixa pressão"
        )
        feature = copy.deepcopy(
            payload["synoptic_diagnostics"]["p1"]["structured_features"][0]
        )
        feature.update(
            {
                "feature_id": "p1-deepening-low-2026071400",
                "feature_type": "deepening_low",
                "canonical_name": "baixa pressão",
                "lifecycle": "developing",
                "start_utc": "2026-07-14T00:00:00Z",
                "end_utc": "2026-07-15T00:00:00Z",
                "peak_utc": "2026-07-15T00:00:00Z",
                "continues_into": "p2",
                "effects": ["retorno_gradual_da_instabilidade_no_sul"],
                "description": "Uma baixa pressão se aprofunda no Prata.",
            }
        )
        payload["synoptic_diagnostics"]["p1"]["structured_features"].append(feature)
        packet = AnalysisPacket.model_validate(payload)

        with self.assertRaises(SummaryValidationError) as raised:
            validate_summary_output(VALID_OUTPUT, packet)
        self.assertEqual(
            raised.exception.code,
            "synoptic_transition_timing_missing",
        )

        paragraph = VALID_OUTPUT.splitlines()[1]
        timed = VALID_OUTPUT.replace(
            paragraph,
            paragraph
            + " Na terça-feira, 14/07, uma baixa pressão começa a se aprofundar "
            "no Prata e o sinal prossegue para a pêntada seguinte.",
            1,
        )
        validate_summary_output(timed, packet)

    def test_rejects_missing_unit_spacing(self):
        self.assert_invalid(VALID_OUTPUT.replace("20 mm", "20mm", 1), "unit_spacing")

    def test_structured_weather_authorizes_detailed_prose(self):
        paragraph = VALID_OUTPUT.splitlines()[1]
        detailed = VALID_OUTPUT.replace(
            paragraph,
            paragraph
            + " A frente fria se afasta pelo oceano, enquanto as temperaturas ficam "
            "abaixo da média no início e apresentam aquecimento gradual.",
            1,
        )

        validated = validate_summary_output(detailed, self.packet)
        self.assertIn("frente fria", validated.text)
        self.assertIn("abaixo da média", validated.text)

    def test_vertical_thermal_inversion_is_not_a_regional_pattern_change(self):
        paragraph = VALID_OUTPUT.splitlines()[1]
        invalid = VALID_OUTPUT.replace(
            paragraph,
            paragraph + " Também ocorre inversão térmica no Sul.",
            1,
        )

        self.assert_invalid(invalid, "unsupported_vertical_thermal_inversion")
