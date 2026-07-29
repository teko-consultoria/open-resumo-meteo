from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import AUTHORIZED_MODEL_IDS, EDITORIAL_CYCLE_UTC, MODEL_FAMILY_BY_MODEL

DEFAULT_MODEL_FIELD_CONFIG_PATH = Path(__file__).with_name("config") / "model_fields.v1.json"
VALID_ROLES = frozenset(
    {
        "precipitation",
        "surface",
        "850_hpa",
        "500_hpa",
        "200_250_hpa",
        "wind",
        "temperature",
        "solar",
        "severe_weather",
    }
)


@dataclass(frozen=True, slots=True)
class ModelFieldConfig:
    config_id: str
    config_version: str
    cycle_utc: str
    domain: Mapping[str, Any]
    models: Mapping[str, Mapping[str, Any]]

    def model(self, model_id: str) -> Mapping[str, Any]:
        try:
            return self.models[str(model_id)]
        except KeyError as exc:
            raise ValueError(f"Modelo nao autorizado no cadastro de campos: {model_id!r}") from exc

    def fields(self, model_id: str, *, role: str | None = None) -> tuple[Mapping[str, Any], ...]:
        fields = tuple(self.model(model_id)["fields"])
        if role is None:
            return fields
        if role not in VALID_ROLES:
            raise ValueError(f"Papel de campo desconhecido: {role!r}")
        return tuple(field for field in fields if field["role"] == role)

    def required_fields(self, model_id: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(field for field in self.fields(model_id) if field["required"])


def _validate_field(model_id: str, field: object) -> None:
    if not isinstance(field, dict):
        raise ValueError(f"Campo de {model_id} deve ser objeto")
    required = {"key", "role", "required", "units_out"}
    if not required.issubset(field):
        raise ValueError(f"Campo de {model_id} sem metadados obrigatorios: {sorted(required - set(field))}")
    if field["role"] not in VALID_ROLES:
        raise ValueError(f"Role invalido em {model_id}: {field['role']!r}")
    if not isinstance(field["required"], bool):
        raise ValueError(f"required deve ser booleano em {model_id}.{field['key']}")
    selector_key = "variable" if model_id in {"gfs", "gefs"} else "param"
    if not field.get(selector_key):
        raise ValueError(f"Campo {model_id}.{field['key']} sem seletor {selector_key}")


def load_model_field_config(path: str | Path = DEFAULT_MODEL_FIELD_CONFIG_PATH) -> ModelFieldConfig:
    resolved = Path(path)
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Nao foi possivel ler a configuracao de campos: {resolved}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("Configuracao de campos deve usar schema_version=1")
    if raw.get("cycle_utc") != EDITORIAL_CYCLE_UTC:
        raise ValueError("Configuracao editorial deve usar exclusivamente o ciclo 00 UTC")
    models = raw.get("models")
    if not isinstance(models, dict) or tuple(models) != AUTHORIZED_MODEL_IDS:
        raise ValueError("Configuracao deve conter somente GFS, GEFS, EC e ECENS na ordem canonica")
    for model_id, model in models.items():
        if not isinstance(model, dict):
            raise ValueError(f"Configuracao do modelo {model_id} deve ser objeto")
        if model.get("family") != MODEL_FAMILY_BY_MODEL[model_id]:
            raise ValueError(f"Familia divergente em {model_id}")
        fields = model.get("fields")
        if not isinstance(fields, list) or not fields:
            raise ValueError(f"Modelo {model_id} sem campos")
        for field in fields:
            _validate_field(model_id, field)
        keys = [str(field["key"]) for field in fields]
        if len(keys) != len(set(keys)):
            raise ValueError(f"Chaves de campo duplicadas em {model_id}")
        precipitation = [field for field in fields if field["role"] == "precipitation"]
        if len(precipitation) != 1 or not precipitation[0]["required"]:
            raise ValueError(f"Modelo {model_id} deve ter uma precipitacao obrigatoria")
    return ModelFieldConfig(
        config_id=str(raw.get("config_id") or ""),
        config_version=str(raw.get("config_version") or ""),
        cycle_utc=str(raw["cycle_utc"]),
        domain=dict(raw.get("domain") or {}),
        models=models,
    )


DEFAULT_MODEL_FIELD_CONFIG = load_model_field_config()


__all__ = [
    "DEFAULT_MODEL_FIELD_CONFIG",
    "DEFAULT_MODEL_FIELD_CONFIG_PATH",
    "ModelFieldConfig",
    "load_model_field_config",
]
