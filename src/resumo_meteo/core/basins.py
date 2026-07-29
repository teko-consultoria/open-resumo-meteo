"""Strict, versioned registry of the canonical SMAP energy-basin catalog."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

EXPECTED_CANONICAL_BASINS: tuple[tuple[str, str], ...] = (
    ("amazonas_norte", "Amazonas Norte"),
    ("amazonas_sudeste", "Amazonas Sudeste"),
    ("araguari", "Araguari"),
    ("capivari", "Capivari"),
    ("doce", "Doce"),
    ("grande", "Grande"),
    ("iguacu", "Iguaçu"),
    ("itabapoana", "Itabapoana"),
    ("itajai_acu", "Itajaí-Açu"),
    ("jacui", "Jacuí"),
    ("jequitinhonha", "Jequitinhonha"),
    ("madeira", "Madeira"),
    ("mucuri", "Mucuri"),
    ("paraguacu", "Paraguaçu"),
    ("paraguai", "Paraguai"),
    ("paraiba_do_sul", "Paraíba do Sul"),
    ("parana", "Paraná"),
    ("paranaiba", "Paranaíba"),
    ("paranapanema", "Paranapanema"),
    ("parnaiba", "Parnaíba"),
    ("sao_francisco", "São Francisco"),
    ("santa_maria_vitoria", "Santa Maria da Vitória"),
    ("teles_pires", "Teles Pires"),
    ("tiete", "Tietê"),
    ("tocantins", "Tocantins"),
    ("uruguai", "Uruguai"),
    ("xingu", "Xingu"),
)

CANONICAL_BASIN_IDS: tuple[str, ...] = tuple(item[0] for item in EXPECTED_CANONICAL_BASINS)
CANONICAL_BASIN_NAMES: tuple[str, ...] = tuple(item[1] for item in EXPECTED_CANONICAL_BASINS)
CANONICAL_BASIN_NAME_BY_ID: dict[str, str] = dict(EXPECTED_CANONICAL_BASINS)
DEFAULT_BASIN_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "basins.json"
PACKAGE_ROOT = Path(__file__).resolve().parent
_BASIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_VALID_SUBSYSTEMS = frozenset(("S", "SE_CO", "NE", "N"))
_VALID_EDITORIAL_ARTICLES = frozenset(("o", "a"))
_EDITORIAL_CONTRACTIONS: dict[tuple[str, str], str] = {
    ("em", "o"): "no",
    ("em", "a"): "na",
    ("de", "o"): "do",
    ("de", "a"): "da",
    ("a", "o"): "ao",
    ("a", "a"): "à",
}


def _lookup_token(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(ascii_text.casefold().strip().split())


@dataclass(frozen=True, slots=True)
class BasinDefinition:
    basin_id: str
    canonical_name: str
    editorial_article: str
    aliases: tuple[str, ...]
    subsystem: str
    hydrographic_region: str
    geometry_id: str
    geometry_path: str
    geometry_source_reference: str

    def editorial_reference(self, *, preposition: str = "em") -> str:
        """Return the sector-style reference while preserving the isolated name.

        The article is catalog data instead of being inferred from the spelling
        of the river name.  This keeps every canonical basin reusable with the
        Portuguese contractions used in prose (for example, ``no Uruguai`` and
        ``do Uruguai``), including future basins with a different article.
        """

        try:
            contraction = _EDITORIAL_CONTRACTIONS[(preposition, self.editorial_article)]
        except KeyError as exc:
            raise ValueError(
                f"Preposicao editorial nao suportada: {preposition!r}"
            ) from exc
        return f"{contraction} {self.canonical_name}"

    @property
    def locative_reference(self) -> str:
        """Canonical ``em`` + article form used by the weather summary."""

        return self.editorial_reference(preposition="em")

    def to_dict(self) -> dict[str, object]:
        return {
            "basin_id": self.basin_id,
            "canonical_name": self.canonical_name,
            "editorial_article": self.editorial_article,
            "aliases": list(self.aliases),
            "subsystem": self.subsystem,
            "hydrographic_region": self.hydrographic_region,
            "geometry_id": self.geometry_id,
            "geometry_path": self.geometry_path,
            "geometry_source_reference": self.geometry_source_reference,
        }


@dataclass(frozen=True, slots=True)
class BasinRegistry:
    schema_version: int
    catalog_id: str
    catalog_version: str
    crs: str
    source_note: str
    basins: tuple[BasinDefinition, ...]

    def __post_init__(self) -> None:
        _validate_registry(self)

    @property
    def by_id(self) -> dict[str, BasinDefinition]:
        return {basin.basin_id: basin for basin in self.basins}

    def require_id(self, basin_id: str) -> BasinDefinition:
        if not isinstance(basin_id, str):
            raise TypeError("basin_id deve ser texto")
        try:
            return self.by_id[basin_id]
        except KeyError as exc:
            raise ValueError(f"ID de bacia nao canonico: {basin_id!r}") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "catalog_id": self.catalog_id,
            "catalog_version": self.catalog_version,
            "source_note": self.source_note,
            "crs": self.crs,
            "basins": [basin.to_dict() for basin in self.basins],
        }


def _validate_registry(registry: BasinRegistry) -> None:
    if registry.schema_version != 4:
        raise ValueError("schema_version do cadastro de bacias deve ser 4")
    if not registry.catalog_id or not registry.catalog_version or not registry.crs:
        raise ValueError("Cadastro de bacias sem id, versao ou CRS")
    actual_pairs = tuple((basin.basin_id, basin.canonical_name) for basin in registry.basins)
    if actual_pairs != EXPECTED_CANONICAL_BASINS:
        raise ValueError("Cadastro deve conter exatamente as 27 bacias SMAP canônicas, na ordem contratual")
    seen_aliases: dict[str, str] = {}
    geometry_ids: set[str] = set()
    geometry_paths: dict[str, str] = {}
    repository_root = PACKAGE_ROOT

    for basin in registry.basins:
        if not _BASIN_ID_PATTERN.fullmatch(basin.basin_id):
            raise ValueError(f"basin_id invalido: {basin.basin_id}")
        if (
            not isinstance(basin.editorial_article, str)
            or basin.editorial_article not in _VALID_EDITORIAL_ARTICLES
        ):
            raise ValueError(
                f"Artigo editorial invalido para {basin.basin_id}: {basin.editorial_article!r}"
            )
        if basin.subsystem not in _VALID_SUBSYSTEMS:
            raise ValueError(f"Subsistema invalido para {basin.basin_id}: {basin.subsystem}")
        if not basin.hydrographic_region or not basin.geometry_id:
            raise ValueError(f"Metadados incompletos para {basin.basin_id}")
        geometry_path = Path(basin.geometry_path)
        if (
            geometry_path.is_absolute()
            or ".." in geometry_path.parts
            or geometry_path.suffix.casefold() != ".geojson"
        ):
            raise ValueError(f"Caminho de contorno inseguro para {basin.basin_id}")
        if not (repository_root / geometry_path).is_file():
            raise ValueError(f"Contorno cadastrado nao existe para {basin.basin_id}")
        if (
            not basin.geometry_source_reference.strip()
            or basin.geometry_source_reference.startswith("/")
        ):
            raise ValueError(f"Proveniencia de contorno invalida para {basin.basin_id}")
        owner = geometry_paths.get(basin.geometry_path)
        if owner is not None:
            raise ValueError(
                f"Contorno reutilizado entre {owner} e {basin.basin_id}"
            )
        geometry_paths[basin.geometry_path] = basin.basin_id
        if basin.geometry_id in geometry_ids:
            raise ValueError(f"geometry_id duplicado: {basin.geometry_id}")
        geometry_ids.add(basin.geometry_id)
        candidates = (basin.canonical_name, basin.basin_id.replace("_", " "), *basin.aliases)
        local_tokens: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                raise ValueError(f"Alias vazio em {basin.basin_id}")
            token = _lookup_token(candidate)
            if token in local_tokens:
                # Accent-less forms intentionally normalize to the same lookup
                # token and need not be repeated in the source configuration.
                continue
            local_tokens.add(token)
            owner = seen_aliases.get(token)
            if owner is not None and owner != basin.basin_id:
                raise ValueError(f"Alias ambiguo entre {owner} e {basin.basin_id}: {candidate}")
            seen_aliases[token] = basin.basin_id


def load_basin_registry(path: str | Path = DEFAULT_BASIN_CONFIG_PATH) -> BasinRegistry:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Nao foi possivel ler o cadastro de bacias: {config_path}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("basins"), list):
        raise ValueError("Cadastro de bacias deve ser um objeto JSON com lista basins")
    allowed_top_level = {
        "schema_version",
        "catalog_id",
        "catalog_version",
        "source_note",
        "crs",
        "basins",
    }
    if set(raw) != allowed_top_level:
        raise ValueError(f"Campos inesperados/ausentes no cadastro: {sorted(set(raw) ^ allowed_top_level)}")
    basin_fields = {
        "basin_id",
        "canonical_name",
        "editorial_article",
        "aliases",
        "subsystem",
        "hydrographic_region",
        "geometry_id",
        "geometry_path",
        "geometry_source_reference",
    }
    basins: list[BasinDefinition] = []
    for index, item in enumerate(raw["basins"]):
        if not isinstance(item, dict) or set(item) != basin_fields:
            actual = set(item) if isinstance(item, dict) else set()
            raise ValueError(f"Campos invalidos na bacia {index}: {sorted(actual ^ basin_fields)}")
        if not isinstance(item["aliases"], list):
            raise ValueError(f"aliases deve ser lista na bacia {index}")
        basins.append(
            BasinDefinition(
                basin_id=item["basin_id"],
                canonical_name=item["canonical_name"],
                editorial_article=item["editorial_article"],
                aliases=tuple(item["aliases"]),
                subsystem=item["subsystem"],
                hydrographic_region=item["hydrographic_region"],
                geometry_id=item["geometry_id"],
                geometry_path=item["geometry_path"],
                geometry_source_reference=item["geometry_source_reference"],
            )
        )
    return BasinRegistry(
        schema_version=raw["schema_version"],
        catalog_id=raw["catalog_id"],
        catalog_version=raw["catalog_version"],
        source_note=raw["source_note"],
        crs=raw["crs"],
        basins=tuple(basins),
    )


DEFAULT_BASIN_REGISTRY = load_basin_registry()


def require_canonical_basin_id(basin_id: str) -> str:
    return DEFAULT_BASIN_REGISTRY.require_id(basin_id).basin_id


def format_basin_editorial_reference(
    basin_id: str,
    *,
    preposition: str = "em",
) -> str:
    """Format a canonical basin reference from the versioned registry."""

    return DEFAULT_BASIN_REGISTRY.require_id(basin_id).editorial_reference(
        preposition=preposition
    )


def validate_canonical_basin_ids(values: Iterable[str], *, require_all: bool = False) -> tuple[str, ...]:
    result = tuple(require_canonical_basin_id(value) for value in values)
    if len(set(result)) != len(result):
        raise ValueError("Lista de bacias contem IDs duplicados")
    if require_all and result != CANONICAL_BASIN_IDS:
        raise ValueError(
            f"Lista deve conter as {len(CANONICAL_BASIN_IDS)} bacias na ordem canonica"
        )
    return result
