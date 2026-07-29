from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from editorial_samples import VALID_SUMMARY

from resumo_meteo.core.openai_client import (
    PROMPT_VERSION,
    OpenAIEditorialResult,
    OpenAIUsage,
)
from resumo_meteo.core.schema import AnalysisPacket, WindowId
from resumo_meteo.pipeline.generation import (
    SUMMARY_DISCLAIMER,
    SUMMARY_DISCLAIMER_VERSION,
    EditorialGenerationError,
    generate_validated_summary,
)

PUBLISHED_SUMMARY = f"{VALID_SUMMARY}\n\n{SUMMARY_DISCLAIMER}"


def _load_packet() -> AnalysisPacket:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "analysis_packet_v2.json"
    return AnalysisPacket.model_validate_json(fixture.read_text(encoding="utf-8"))


def _sections(text: str = VALID_SUMMARY) -> dict[WindowId, str]:
    values = text.split("\n\n")
    assert len(values) == 3
    return dict(zip((WindowId.P1, WindowId.P2, WindowId.P3), values, strict=True))


class FakeEditorialClient:
    def __init__(
        self,
        *,
        window_outputs: dict[WindowId, str],
        window_repair_outputs: dict[WindowId, str] | None = None,
    ) -> None:
        self.window_outputs = window_outputs
        self.window_repair_outputs = window_repair_outputs or {}
        self.generate_calls: list[dict[str, Any]] = []
        self.repair_calls: list[dict[str, Any]] = []
        self.response_counter = 0

    def _result(self, text: str, *, window_id: str) -> OpenAIEditorialResult:
        self.response_counter += 1
        return OpenAIEditorialResult(
            text=text,
            response_id=f"response-{self.response_counter}",
            model="sabia-test",
            usage=OpenAIUsage(input_tokens=100, output_tokens=50, total_tokens=150),
            prompt_version=PROMPT_VERSION,
            request_metadata={"window_id": window_id},
        )

    def generate(self, packet, *, audit_metadata=None, window_id=None, prior_sections=()):
        selected = WindowId(window_id)
        self.generate_calls.append(
            {
                "packet": packet,
                "audit_metadata": dict(audit_metadata or {}),
                "window_id": selected,
                "prior_sections": tuple(prior_sections),
            }
        )
        return self._result(self.window_outputs[selected], window_id=selected.value)

    def repair_window(
        self,
        packet,
        *,
        window_id,
        invalid_text,
        validation_error_code,
        validation_error_message,
        audit_metadata=None,
        prior_sections=(),
    ):
        selected = WindowId(window_id)
        self.repair_calls.append(
            {
                "packet": packet,
                "window_id": selected,
                "invalid_text": invalid_text,
                "validation_error_code": validation_error_code,
                "validation_error_message": validation_error_message,
                "audit_metadata": dict(audit_metadata or {}),
                "prior_sections": tuple(prior_sections),
            }
        )
        if selected not in self.window_repair_outputs:
            raise AssertionError("repair_window não deveria ter sido chamado")
        return self._result(self.window_repair_outputs[selected], window_id=f"repair-{selected.value}")


def test_generation_uses_exactly_one_call_per_window_and_three_sections() -> None:
    packet = _load_packet()
    client = FakeEditorialClient(window_outputs=_sections())

    result = generate_validated_summary(packet, run_id="run-1", client=client)

    assert [call["window_id"] for call in client.generate_calls] == [
        WindowId.P1,
        WindowId.P2,
        WindowId.P3,
    ]
    assert client.repair_calls == []
    assert result.text == PUBLISHED_SUMMARY
    assert result.sha256 == hashlib.sha256(PUBLISHED_SUMMARY.encode("utf-8")).hexdigest()
    assert result.text.count("ª pêntada (") == 3
    assert result.text.splitlines()[0].startswith("1ª pêntada")
    assert result.text.splitlines()[-1] == SUMMARY_DISCLAIMER
    assert result.metadata["generation_mode"] == "sequential_window_validation_with_scoped_repair"
    assert result.metadata["attempts"][0]["status"] == "validated"
    assert len(result.metadata["validated_sections"]) == 3
    assert result.metadata["publication_disclaimer"] == {
        "version": SUMMARY_DISCLAIMER_VERSION,
        "text": SUMMARY_DISCLAIMER,
    }


def test_invalid_window_is_repaired_without_rewriting_approved_sections() -> None:
    packet = _load_packet()
    outputs = _sections()
    rejected = "2ª pêntada (15/07 a 20/07):\nrascunho rejeitado da p2"
    outputs[WindowId.P2] = rejected
    client = FakeEditorialClient(
        window_outputs=outputs,
        window_repair_outputs={WindowId.P2: _sections()[WindowId.P2]},
    )

    result = generate_validated_summary(
        packet,
        run_id="run-repair",
        client=client,
        max_attempts=1,
    )

    assert result.text == PUBLISHED_SUMMARY
    assert result.response_id == "response-4"
    assert len(client.generate_calls) == 3
    assert len(client.repair_calls) == 1
    assert client.repair_calls[0]["window_id"] == WindowId.P2
    assert client.repair_calls[0]["invalid_text"] == rejected
    assert client.repair_calls[0]["validation_error_code"] == "incomplete_sentence_ending"
    assert client.repair_calls[0]["validation_error_message"]
    assert client.generate_calls[2]["prior_sections"] == (
        _sections()[WindowId.P1],
        _sections()[WindowId.P2],
    )

    audit = result.metadata["attempts"][0]
    assert audit["status"] == "validated_with_window_repairs"
    assert audit["windows"][1]["repair"]["text"] == _sections()[WindowId.P2]
    assert audit["windows"][1]["repair"]["validation"]["status"] == "accepted"
    assert result.validation_errors[0]["candidate_text"] == rejected
    assert result.validation_errors[0]["code"] == "incomplete_sentence_ending"
    assert result.validation_errors[0]["message"]


def test_exhausted_repair_retains_both_rejected_texts_and_reasons_in_error_metadata() -> None:
    packet = _load_packet()
    rejected_by_window = {window_id: "saída sem a estrutura obrigatória" for window_id in WindowId}
    client = FakeEditorialClient(
        window_outputs=rejected_by_window,
        window_repair_outputs={WindowId.P1: "reparo ainda inválido"},
    )

    with pytest.raises(EditorialGenerationError) as raised:
        generate_validated_summary(
            packet,
            run_id="run-failed",
            client=client,
            max_attempts=1,
        )

    metadata = raised.value.metadata
    assert len(metadata["validation_errors"]) == 2
    initial, repaired = metadata["validation_errors"]
    assert initial["candidate_text"] == "saída sem a estrutura obrigatória"
    assert initial["code"] == "invalid_structure"
    assert initial["message"]
    assert repaired["candidate_text"] == "reparo ainda inválido"
    assert repaired["code"] == "invalid_structure"
    assert repaired["message"]
    assert metadata["attempts"][0]["windows"][0]["repair"]["text"] == "reparo ainda inválido"
    assert metadata["attempts"][0]["windows"][0]["repair"]["validation"]["status"] == "failed"
