"""Strict validation for the reader-facing three-section weather summary."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import Field

from .schema import CANONICAL_BASINS, AnalysisPacket, StrictModel, WindowId


class SummaryValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class SummarySection(StrictModel):
    window_id: WindowId
    heading: str
    paragraph: str
    word_count: int = Field(ge=1)
    canonical_basin_names: list[str] = Field(min_length=1)
    mm_values: list[float] = Field(min_length=1)


class ValidatedSummary(StrictModel):
    text: str
    sections: list[SummarySection] = Field(min_length=3, max_length=3)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mm_support_tolerance: float = Field(ge=0)

    def section(self, window_id: WindowId | str) -> SummarySection:
        wanted = WindowId(window_id)
        return next(section for section in self.sections if section.window_id == wanted)


_PROHIBITED_MODEL_NAME = re.compile(
    r"\b(?:AIFS|ICON|WRF|BRAMS)\b",
    re.IGNORECASE,
)
_AUTHORIZED_MODEL_NAME = re.compile(r"\b(?:GFS|GEFS|EC|ECENS)\b", re.IGNORECASE)
_AMBIGUOUS_CONFIDENCE = re.compile(
    r"\bconfian[cç]a(?:\s+\w+){0,2}\s+(?:mista|baixa\s+ou\s+mista)\b",
    re.IGNORECASE,
)
_VAGUE_MODEL_GROUP = re.compile(
    r"\b(?:sistemas?|modelos?)\s+europeus?|\bsolu[cç][aã]o\s+europeia\b",
    re.IGNORECASE,
)
_MODEL_COMPARISON_LANGUAGE = re.compile(
    r"\b(?:contraste|divergência|discrepância)\b[^.;!?]{0,45}"
    r"\b(?:modelos?|soluções?)\b|"
    r"\b(?:GFS|GEFS|EC|ECENS)\b[^.;!?]{0,70}"
    r"\b(?:mais\s+(?:úmid[oa]s?|sec[oa]s?)|sinal\s+mais\s+(?:úmido|seco))\b"
    r"[^.;!?]{0,70}\b(?:GFS|GEFS|EC|ECENS)\b",
    re.IGNORECASE,
)
_MARKDOWN_LINK = re.compile(r"!?\[[^\]\n]+\]\([^\)\n]+\)")
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_BULLET_OR_LIST = re.compile(r"^(?:[-+*•]\s+|\d+[.)]\s+)")
_MALFORMED_PUNCTUATION = re.compile(r"[.!?][,;:]")
_LOWERCASE_SENTENCE_START = re.compile(
    r"[.!?]\s+[a-záàâãéêíóôõúüç]",
)
_INVALID_NUMBER_AGREEMENT = re.compile(
    r"\b(?:(?:permanece|mantém)\s+(?:secos|secas)|"
    r"(?:permanecem|mantêm)\s+(?:seco|seca))\b",
    re.IGNORECASE,
)
_AMBIGUOUS_DRY_TO_WET_TRANSITION = re.compile(
    r"\b(?:transi[cç][aã]o|transita\w*|passa\w*|evolu\w*|mudan[cç]a)\b"
    r"[^.;!?]{0,100}\b(?:sec[oa]s?|pouca\s+chuva)\b"
    r"[^.;!?]{0,100}\b(?:mais\s+[uú]mid[oa]s?|mais\s+chuv\w*|cen[aá]rio\s+[uú]mid\w*)\b",
    re.IGNORECASE,
)
_EXPLICIT_OBSERVED_ANTECEDENT = re.compile(
    r"\b(?:observad\w*|antecedent\w*|dias?\s+anteriores?)\b",
    re.IGNORECASE,
)
_EXPLICIT_FIRST_FORECAST_DAY = re.compile(
    r"\b(?:j[aá]\s+)?no\s+primeiro\s+dia(?:\s+(?:da|de)\s+p[eê]ntada)?\b",
    re.IGNORECASE,
)
MIN_PARAGRAPH_WORDS = 75
MAX_PARAGRAPH_WORDS = 170
# Editorial values are already rounded before the language-model boundary.
# Keep only a floating-point comparison epsilon here: a wider tolerance would
# silently authorize raw model values near an editorial endpoint.
MM_SUPPORT_TOLERANCE = 1e-6

_MM_VALUE = re.compile(
    r"(?<![\w.,])-?\d+(?:[.,]\d+)?"
    r"(?:\s+(?:a|–|—|-)\s+-?\d+(?:[.,]\d+)?)?\s+mm\b",
    re.IGNORECASE,
)
_MM_NUMBERS = re.compile(
    r"(?<![\w.,])(?P<first>-?\d+(?:[.,]\d+)?)"
    r"(?:\s+(?:a|–|—|-)\s+(?P<second>-?\d+(?:[.,]\d+)?))?\s+mm\b",
    re.IGNORECASE,
)
_POINT_CONCEPT = re.compile(r"\b(?:máxim\w*|pico\w*|pontual\w*)\b", re.IGNORECASE)
_EXPLICIT_POINT = re.compile(
    r"\b(?:máxim\w*(?:\s+\w+){0,3}\s+pontual\w*|pontual\w*(?:\s+\w+){0,3}\s+máxim\w*|máximo\s+de\s+grade)\b",
    re.IGNORECASE,
)
_EXPLICIT_BASIN_MEAN = re.compile(
    r"\b(?:média(?:\s+espacial)?\s+(?:da|das|de)\s+bacia\w*|média\s+de\s+bacia|acumulad\w*\s+m[ée]di\w*(?:\s+por\s+bacia)?)\b",
    re.IGNORECASE,
)
_NAMED_SYNOPTIC_MECHANISM = re.compile(
    r"\b(?:sistema\s+frontal|frente\s+(?:fria|quente)|"
    r"frente(?!\s+(?:a|à|ao|aos|às|de|do|da|dos|das)\b)|"
    r"alta\s+p[oó]s-frontal|ciclone|anticiclone|cavado|crista|"
    r"jato\s+de\s+baixos\s+níveis|JBN|ZCAS|ZCIT|VCAN|bloqueio|massa\s+de\s+ar|"
    r"convergência\s+de\s+umidade|(?:alta|baixa)\s+pressão|convecção|"
    r"(?:atividade|instabilidade|precipitação|chuva)\s+convectiv\w*)\b",
    re.IGNORECASE,
)
_MECHANISM_ABSENCE = re.compile(
    r"\b(?:sem|ausência\s+de|não\s+(?:há|existe|se\s+observa))\b"
    r"[^.;!?]{0,70}\b(?:mecanismos?\s+sinóticos?|sistemas?\s+sinóticos?(?:\s+estruturados?)?|sistemas?\s+forçantes?|"
    r"estruturas?\s+(?:sinóticas?|dinâmicas?|ativas?))\b",
    re.IGNORECASE,
)
_MECHANISM_ABSENCE_CLAUSE = re.compile(
    # The separator must consume whitespace before an optional conjunction.
    # Using optional whitespace followed by ``e\s+`` can start at the final
    # "e" of a preceding word (for example, ``transcorre sem``), silently
    # turning it into ``transcorr`` when this internal clause is removed.
    r"(?:,\s*|\s+e\s+|\s+)(?:a\s+)?(?:sem|ausência\s+de|não\s+(?:há|existe|se\s+observa))\b"
    r"[^,.;!?\n]{0,80}\b(?:mecanismos?\s+sinóticos?|sistemas?\s+sinóticos?(?:\s+estruturados?)?|sistemas?\s+forçantes?|"
    r"estruturas?\s+(?:sinóticas?|dinâmicas?))\b[^,.;!?\n]*"
    r"(?P<punctuation>[,.;!?])",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY = re.compile(r"(?<!\d)[.;!?]+(?!\d)")
_VALUE_TO_BASIN_CONNECTOR = re.compile(
    r"\s*,?\s*(?:no|na|nos|nas|em|do|da|de)\s+",
    re.IGNORECASE,
)
_WIND_WORD = re.compile(r"\bventos?\b", re.IGNORECASE)
_WIND_OPERATIONAL_CONTEXT = re.compile(
    r"\b(?:favor[aá]ve(?:l|is)|desfavor[aá]ve(?:l|is)|gera[cç][aã]o|produ[cç][aã]o|"
    r"recurso|potencial|parques?|polos?|e[oó]lic\w*)\b|\d+(?:[.,]\d+)?\s+m/s\b",
    re.IGNORECASE,
)
_WIND_SECTOR = re.compile(r"\be[oó]lic\w*\b", re.IGNORECASE)
_TEMPERATURE_TREND = re.compile(
    r"\b(?:calor|temperaturas?)\b[^.;!?]{0,60}\b(?:aument\w*|diminu\w*|sub\w*|"
    r"cai\w*|elev\w*|redu\w*|aquec\w*|resfri\w*|"
    r"abaixo\s+da\s+m[eé]dia|acima\s+da\s+m[eé]dia|pr[oó]xim\w*\s+da\s+m[eé]dia|"
    r"pression\w*|favore\w*|impact\w*)\b|"
    r"\b(?:aumento|queda|eleva[cç][aã]o|redu[cç][aã]o)\s+(?:do|da|de)\s+"
    r"(?:calor|temperatura)\b|\d+(?:[.,]\d+)?\s+°C\b",
    re.IGNORECASE,
)
_LOAD_SECTOR = re.compile(
    r"\b(?:carga\s+(?:el[eé]trica|do\s+SIN)|centros?\s+de\s+carga|"
    r"demanda\s+(?:el[eé]trica|de\s+energia)|consumo\s+de\s+energia|"
    r"press[aã]o\s+(?:meteorol[oó]gica\s+)?sobre\s+a\s+carga)\b",
    re.IGNORECASE,
)
_SOLAR_SECTOR = re.compile(
    r"\b(?:solar|fotovoltaic\w*|irradi[aâ]ncia|radia[cç][aã]o\s+solar)\b",
    re.IGNORECASE,
)
_SEVERE_WEATHER = re.compile(
    r"\b(?:tempo\s+severo|eventos?\s+severos?|tempestades?\s+severas?|granizo|"
    r"vendaval|tornado|microexplos[aã]o|descargas?\s+el[eé]tricas?|rajadas?)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_HYDROLOGICAL_OUTCOME = re.compile(
    r"\b(?:ENA|vaz(?:ão|oes|ões)|aflu[eê]ncias?|recarga|aportes?|armazenamento|"
    r"reservat[oó]rios?|cheias?|d[eé]ficit\s+h[ií]drico|resposta\s+hidrol[oó]gica|"
    r"condi[cç][oõ]es\s+h[ií]dricas|atividade\s+h[ií]drica)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_SOIL_STATE = re.compile(
    r"\b(?:encharc\w*|satura[cç][aã]o|solos?\s+(?:saturad\w*|encharcad\w*)|"
    r"umidade\s+do\s+solo)\b",
    re.IGNORECASE,
)
_CONFIDENCE_LANGUAGE = re.compile(
    r"\b(?:confian[cç]a|confiabilidade|incerteza|previsibilidade)\b",
    re.IGNORECASE,
)
_BELOW_AVERAGE_TEMPERATURE = re.compile(
    r"\b(?:temperaturas?|valores?\s+t[eé]rmicos?)\b[^.;!?]{0,45}"
    r"\babaixo\s+da\s+m[eé]dia\b",
    re.IGNORECASE,
)
_ABOVE_AVERAGE_TEMPERATURE = re.compile(
    r"\b(?:temperaturas?|valores?\s+t[eé]rmicos?)\b[^.;!?]{0,45}"
    r"\bacima\s+da\s+m[eé]dia\b",
    re.IGNORECASE,
)
_WARMING_TEMPERATURE = re.compile(
    r"\b(?:aquec\w*|eleva[cç][aã]o\s+(?:gradual\s+)?das?\s+temperaturas?|"
    r"temperaturas?\s+(?:em\s+)?(?:eleva[cç][aã]o|alta))\b",
    re.IGNORECASE,
)
_COOLING_TEMPERATURE = re.compile(
    r"\b(?:resfri\w*|queda\s+(?:gradual\s+)?das?\s+temperaturas?|"
    r"temperaturas?\s+(?:em\s+)?queda)\b",
    re.IGNORECASE,
)
_VERTICAL_THERMAL_INVERSION = re.compile(r"\binvers[aã]o\s+t[eé]rmica\b", re.IGNORECASE)
_INCOMPLETE_ENDING = re.compile(
    r"\b(?:dado\s+qu?|visto\s+qu?|uma\s+vez\s+qu?|embora|porque|pois|"
    r"enquanto|quando|que|qu)\s*[.!?]?$",
    re.IGNORECASE,
)

_MECHANISM_ALIASES = {
    "sistema frontal": "frente",
    "frente": "frente",
    "frente fria": "frente fria",
    "frente quente": "frente quente",
    "ciclone": "ciclone",
    "anticiclone": "anticiclone",
    "cavado": "cavado",
    "crista": "crista",
    "jato de baixos niveis": "jato de baixos niveis",
    "jbn": "jato de baixos niveis",
    "zcas": "zcas",
    "zcit": "zcit",
    "vcan": "vcan",
    "bloqueio": "bloqueio",
    "massa de ar": "massa de ar",
    "convergencia de umidade": "convergencia de umidade",
    "alta pressao": "alta pressao",
    "alta pos frontal": "alta pos frontal",
    "baixa pressao": "baixa pressao",
    "conveccao": "conveccao",
}


def _expected_heading(packet: AnalysisPacket, window_id: WindowId, index: int) -> str:
    window = packet.window(window_id)
    return (
        f"{index}ª pêntada "
        f"({window.start_utc.strftime('%d/%m')} a {window.end_utc.strftime('%d/%m')}):"
    )


def _strip_accents(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    )


def _mask_exact_canonical_names(text: str, *, basin_names: Iterable[str]) -> str:
    masked = text
    for name in sorted(set(basin_names), key=len, reverse=True):
        for match in list(re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", masked, re.IGNORECASE)):
            if match.group(0) != name:
                raise SummaryValidationError(
                    "noncanonical_basin_case",
                    f"basin name must be written exactly as {name!r}",
                )
        masked = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", " " * len(name), masked)
    return masked


def _normalize_basin_editorial_references(
    text: str,
    packet: AnalysisPacket,
) -> str:
    normalized = str(text)
    for basin in sorted(packet.basins, key=lambda item: len(item.name), reverse=True):
        expected = basin.editorial_reference
        name = basin.name
        prefix = expected[: -len(name)] if expected.endswith(name) else ""
        prefixed_pattern = re.compile(
            rf"(?<!\w)(?:(?:ao|à|aos|às|para|pelo|pela|pelos|pelas|"
            rf"o|a|os|as)\s+(?:no|na|nos|nas)|"
            rf"para\s+no|para\s+na|para\s+nos|para\s+nas|"
            rf"pelo\s+no|pela\s+na|pelos\s+nos|pelas\s+nas|"
            rf"o\s+no|a\s+na|os\s+nos|as\s+nas|"
            rf"ao|à|aos|às|para|pelo|pela|pelos|pelas|"
            rf"o|a|os|as|do|da|dos|das|de|em|no|na|nos|nas)\s+"
            rf"{re.escape(name)}(?!\w)",
            re.IGNORECASE,
        )

        def replace_prefixed(
            match: re.Match[str], expected: str = expected
        ) -> str:
            return (
                expected[:1].upper() + expected[1:]
                if match.group(0)[:1].isupper()
                else expected
            )

        normalized = prefixed_pattern.sub(replace_prefixed, normalized)
        pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")

        def replace(
            match: re.Match[str],
            prefix: str = prefix,
            expected: str = expected,
            normalized: str = normalized,
        ) -> str:
            if prefix and normalized[max(0, match.start() - len(prefix)) : match.start()].casefold() == prefix.casefold():
                return match.group(0)
            return expected

        normalized = pattern.sub(replace, normalized)
        normalized = re.sub(
            rf"(?<!\w)(?:(?:nas?|das?|as?)\s+)?bacias?\s+"
            rf"(?={re.escape(expected)}(?!\w))",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
    locative_refs = sorted(
        {basin.editorial_reference for basin in packet.basins},
        key=len,
        reverse=True,
    )
    reference_pattern = "|".join(re.escape(item) for item in locative_refs)
    interrupt_and_register = re.compile(
        rf"(?P<refs>(?:{reference_pattern})(?:\s+e\s+(?:{reference_pattern}))?)"
        r"\s+interrompe[m]?\s+(?P<state>[^,.;!?]+?)\s+e\s+"
        r"registra[m]?\s+(?P<value>-?\d+(?:[.,]\d+)?\s+mm)",
        re.IGNORECASE,
    )

    def normalize_interrupt_and_register(match: re.Match[str]) -> str:
        refs = match.group("refs")
        state = match.group("state").strip()
        if state.casefold().startswith("o "):
            state = "do " + state[2:]
        elif state.casefold().startswith("a "):
            state = "da " + state[2:]
        trailing_refs = refs[:1].lower() + refs[1:] if refs[:1].isupper() else refs
        replacement = (
            f"há interrupção {state}, com {match.group('value')} {trailing_refs}"
        )
        return replacement[:1].upper() + replacement[1:] if refs[:1].isupper() else replacement

    normalized = interrupt_and_register.sub(
        normalize_interrupt_and_register,
        normalized,
    )
    subject_verbs = (
        r"mant[eé]m|mant[eê]m|permanece[m]?|registra[m]?|projeta[m]?|"
        r"indica[m]?|aponta[m]?|espera[m]?|evolui(?:m)?|transita[m]?|"
        r"interrompe[m]?"
    )
    locative_subject = re.compile(
        rf"(?P<refs>(?:{reference_pattern})(?:\s+e\s+(?:{reference_pattern}))?)"
        rf"\s+(?P<verb>{subject_verbs})\s+"
        rf"(?P<tail>.*?)(?=\s+e\s+(?:{reference_pattern})\s+"
        rf"(?:{subject_verbs})\b|[,.;!?]|$)",
        re.IGNORECASE,
    )

    def move_locative_after_claim(match: re.Match[str]) -> str:
        refs = match.group("refs")
        verb = _strip_accents(match.group("verb")).casefold()
        tail = match.group("tail").strip()
        sentence_initial = refs[:1].isupper()
        trailing_refs = refs[:1].lower() + refs[1:] if sentence_initial else refs
        if verb.startswith("mant"):
            replacement = f"mantém-se {tail} {trailing_refs}"
        elif verb.startswith("permanec"):
            replacement = f"permanece {tail} {trailing_refs}"
        elif verb.startswith("registr"):
            replacement = f"há {tail} {trailing_refs}"
        elif verb.startswith("evolu") or verb.startswith("transit"):
            replacement = f"há evolução {tail} {trailing_refs}"
        elif verb.startswith("interromp"):
            if tail.casefold().startswith("o "):
                tail = "do " + tail[2:]
            elif tail.casefold().startswith("a "):
                tail = "da " + tail[2:]
            replacement = f"há interrupção {tail} {trailing_refs}"
        else:
            singular = {
                "projetam": "projeta",
                "indicam": "indica",
                "apontam": "aponta",
                "esperam": "espera",
            }.get(verb, verb)
            replacement = (
                f"o cenário {singular} {tail} "
                f"{trailing_refs}"
            )
        return replacement[:1].upper() + replacement[1:] if sentence_initial else replacement

    normalized = locative_subject.sub(move_locative_after_claim, normalized)
    return normalized


def _normalize_basin_editorial_references_until_stable(
    text: str,
    packet: AnalysisPacket,
) -> str:
    """Converge multi-stage basin rewrites before validation/publication."""

    normalized = str(text)
    for _ in range(3):
        updated = _normalize_basin_editorial_references(normalized, packet)
        if updated == normalized:
            return updated
        normalized = updated
    return normalized


def _normalize_mechanism_absence_clauses(text: str) -> str:
    """Remove internal diagnostic-availability prose without changing weather claims."""

    normalized = re.sub(
        r"\bA\s+ausência\s+de\s+sistemas?\s+sinóticos?(?:\s+estruturados?)?\s+"
        r"mantém\s+uma\s+distribuição\s+difusa\s+de\s+instabilidade\b",
        "A instabilidade permanece distribuída de forma difusa",
        str(text),
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\b(?:Essa\s+evolu[cç][aã]o\s+)?decorre\s+da\s+aus[êe]ncia\s+de\s+"
        r"sistemas?\s+organizados?\s+na\s+janela\s+atual\s+e\s+da\s+"
        r"persist[êe]ncia\s+apenas\s+de\s+distribui[cç][aã]o\s+e\s+"
        r"persist[êe]ncia\s+de\s+umidade\b",
        "A umidade permanece restrita e distribuída de forma irregular",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = _MECHANISM_ABSENCE_CLAUSE.sub(
        lambda match: match.group("punctuation"),
        normalized,
    )
    normalized = re.sub(r"([.!?])\s*[.!?]+", r"\1", normalized)
    normalized = re.sub(r",\s*,+", ",", normalized)
    normalized = re.sub(r"[ \t]{2,}", " ", normalized)
    normalized = re.sub(r"\s+([,.;!?])", r"\1", normalized)
    return normalized


def _normalize_observed_rain_language(text: str) -> str:
    """Keep antecedent claims within observed precipitation, not soil state."""

    normalized = re.sub(
        r"\bantecedente\s+de\s+encharcamento\s+recente\b",
        "período chuvoso recente",
        str(text),
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\bsatura[cç][aã]o\s+(?:recente\s+)?nas?\s+bacias?\b",
        "chuva antecedente nas bacias",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized


def _normalize_internal_forecast_language(text: str) -> str:
    normalized = re.sub(
        r"\b(?P<verb>atua|atuam)\s+sobre\s+(?P<reference>n[oa]s?)\b",
        r"\g<verb> \g<reference>",
        str(text),
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\bmantém\s+no\s+(?P<basin>[A-ZÀ-ÖØ-Ý][^,.;!?]{0,48}?)\s+"
        r"sob\s+condi[cç][aã]o\s+seca\b",
        r"mantém condição seca no \g<basin>",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\bfamília\s+(GFS|GEFS|EC|ECENS)\b",
        lambda match: f"o {match.group(1)}",
        normalized,
        flags=re.IGNORECASE,
    )
    # A negative model-comparison disclaimer carries no reader value and used
    # to trigger the positive-evidence guard merely because it contained the
    # words `contraste` and `modelos`.  Omit it; material contrasts remain
    # untouched and still require structured `model_views` evidence below.
    normalized = re.sub(
        r"(?:,\s*|\s+e\s+)?sem\s+(?:contraste|divergência|discrepância)"
        r"(?:\s+\w+){0,3}\s+entre\s+(?:(?:as|os)\s+)?"
        r"(?:famílias\s+(?:de\s+)?)?(?:modelos?|soluções?|famílias)\b",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\b(?:(?:boa|forte|alta|maior)\s+)?concordância\s+entre\s+"
        r"(?:as\s+)?famílias\b",
        "consistência do sinal",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\bconsenso\s+entre\s+(?:as\s+)?famílias\b",
        "consistência do sinal",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\b(?:alta|boa|forte|maior)\s+concordância\s+(?=n[oa]s?\s)",
        "maior segurança ",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\b(Nordeste|Sudeste|Norte|Sul|Centro-Oeste)\s+setorial\b",
        r"\1",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\ba\s+gradual\s+enfraquecimento\b",
        "o gradual enfraquecimento",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\b(?P<label>menor\s+certeza|incerteza|confian[cç]a)\s+"
        r"associad[ao]\s+(?=n[oa]s?\s)",
        r"\g<label> ",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\benquanto\s+mantém-se\b",
        "enquanto se mantém",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\bPermanece\s+sem\s+transição\s+material\s+(?=n[oa]s?\s)",
        "Não há transição material ",
        normalized,
    )
    normalized = re.sub(
        r",?\s*(?:limitad[ao]|condicionad[ao])\s+(?:pela|por|à)\s+"
        r"[^,.;!?]{0,80}\b(?:método|diagnóstico|atribuição\s+sinótica)\b"
        r"[^,.;!?]*",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\s+([,.;!?])", r"\1", normalized)
    normalized = re.sub(r"[ \t]{2,}", " ", normalized)
    return normalized


def _normalize_repeated_qualified_basin_shorthand(
    text: str,
    packet: AnalysisPacket,
) -> str:
    """Expand a repeated base name only when one qualified basin is explicit."""

    bases = ("Tocantins", "Paraná", "Paranaíba", "Grande", "São Francisco")
    basin_by_name = {basin.name: basin for basin in packet.basins}
    lines = []
    for line in str(text).splitlines():
        normalized = line
        for base in bases:
            qualified = [
                basin
                for name, basin in basin_by_name.items()
                if name != base and name.endswith(base)
                and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", normalized)
            ]
            unique = {basin.basin_id: basin for basin in qualified}
            if len(unique) != 1:
                continue
            basin = next(iter(unique.values()))
            normalized = re.sub(
                rf"\b(?:no|na|nos|nas|do|da|dos|das|em)\s+{re.escape(base)}\b",
                basin.editorial_reference,
                normalized,
                flags=re.IGNORECASE,
            )
        lines.append(normalized)
    return "\n".join(lines)


def _validate_basin_names(paragraph: str, packet: AnalysisPacket) -> None:
    masked = _mask_exact_canonical_names(
        paragraph, basin_names=(basin.name for basin in packet.basins)
    )
    for basin in packet.basins:
        expected = basin.editorial_reference
        if re.search(
            rf"\b(?:o|a|os|as)\s+{re.escape(expected)}(?!\w)",
            paragraph,
            re.IGNORECASE,
        ):
            raise SummaryValidationError(
                "redundant_basin_article",
                f"do not place an article before the locative reference {expected!r}",
            )
        for match in re.finditer(
            rf"(?<!\w){re.escape(basin.name)}(?!\w)",
            paragraph,
        ):
            reference_start = match.end() - len(expected)
            actual = paragraph[max(0, reference_start) : match.end()]
            if reference_start < 0 or actual.casefold() != expected.casefold():
                raise SummaryValidationError(
                    "invalid_basin_editorial_reference",
                    f"every prose reference to {basin.name!r} must use {expected!r}",
                )
    # Accentless spellings are aliases, not canonical output names.
    accentless_aliases = {
        _strip_accents(name)
        for name in CANONICAL_BASINS.values()
        if _strip_accents(name) != name
    }
    packet_aliases = {
        alias
        for basin in packet.basins
        for alias in basin.aliases
        if alias and alias.casefold() != basin.name.casefold()
    }
    for alias in sorted(accentless_aliases | packet_aliases, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", masked, re.IGNORECASE):
            raise SummaryValidationError(
                "noncanonical_basin_alias",
                f"noncanonical basin alias is not allowed: {alias!r}",
            )

    # These base names are ambiguous whenever they are presented as a basin.
    ambiguous = r"(?:Paraná|Grande|São Francisco|Sao Francisco|Paranaíba|Paranaiba|Tocantins)"
    if re.search(rf"\bbacias?\s+(?:(?:do|da|de)\s+)?{ambiguous}\b", masked, re.IGNORECASE):
        raise SummaryValidationError(
            "ambiguous_basin_name",
            "use Alto, Médio or Baixo in every divisible canonical basin name",
        )
    if re.search(
        rf"\b(?:no|na|nos|nas|do|da|dos|das|em)\s+{ambiguous}\b",
        masked,
        re.IGNORECASE,
    ):
        raise SummaryValidationError(
            "ambiguous_basin_name",
            "use the complete canonical basin name in locative references",
        )
    if re.search(
        r"\b(?:Alto\s+e\s+Baixo\s+(?:Grande|Paraná|Paranaíba|Tocantins)|"
        r"Alto\s*,\s*Médio\s+e\s+Baixo\s+São Francisco)\b",
        paragraph,
        re.IGNORECASE,
    ):
        raise SummaryValidationError(
            "abbreviated_basin_names",
            "repeat each complete canonical basin name instead of sharing a qualifier",
        )
    locative_refs = sorted(
        {basin.editorial_reference for basin in packet.basins},
        key=len,
        reverse=True,
    )
    locative_ref_pattern = "|".join(re.escape(item) for item in locative_refs)
    adjacent_locatives = re.search(
        rf"(?<!\w)(?:{locative_ref_pattern})\s+(?:{locative_ref_pattern})(?!\w)",
        paragraph,
        re.IGNORECASE,
    )
    if adjacent_locatives:
        raise SummaryValidationError(
            "adjacent_basin_references",
            "separate basin locatives with a conjunction or punctuation",
        )
    locative_list_pattern = (
        rf"(?:{locative_ref_pattern})"
        rf"(?:\s*,\s*(?:{locative_ref_pattern}))*"
        rf"(?:\s+e\s+(?:{locative_ref_pattern}))?"
    )
    locative_as_subject = re.compile(
        rf"(?:\A|[.!?]\s+|,\s+|"
        rf"\b(?:e|mas|porém|enquanto|com|ao\s+passo\s+que)\s+)"
        rf"{locative_list_pattern}\s*,?\s+"
        r"(?:mant[eé]m|mant[eê]m|permanece[m]?|registra[m]?|projeta[m]?|"
        r"indica[m]?|aponta[m]?|espera[m]?|evolui(?:m)?|transita[m]?|"
        r"interrompe[m]?|segue[m]?|concentra[m]?|tende[m]?|consolida[m]?|"
        r"finaliza(?:m|ndo)?)\b",
        re.IGNORECASE,
    )
    if locative_as_subject.search(paragraph):
        raise SummaryValidationError(
            "locative_reference_used_as_subject",
            "use the basin reference as a locative, e.g. 'a chuva se mantém no Jacuí', not 'no Jacuí mantém'",
        )


def _available_canonical_names(paragraph: str, packet: AnalysisPacket) -> tuple[str, ...]:
    available = {
        basin.name
        for basin in packet.basins
        if basin.windows is not None and basin.area_km2 is not None
    }
    return tuple(
        name for name in available if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", paragraph)
    )


def _paragraph_mm_values(paragraph: str) -> tuple[float, ...]:
    values: list[float] = []
    for match in _MM_NUMBERS.finditer(paragraph):
        for token in (match.group("first"), match.group("second")):
            if token is not None:
                values.append(float(token.replace(",", ".")))
    return tuple(values)


def _authorized_basin_mm_values(
    packet: AnalysisPacket,
    window_id: WindowId,
) -> dict[str, tuple[float, ...]]:
    """Return only values explicitly authorized at the editorial boundary."""

    authorized: dict[str, tuple[float, ...]] = {}
    for basin in packet.basins:
        if basin.windows is None:
            continue
        window = getattr(basin.windows, window_id.value)
        values = [float(item) for item in window.opinion.range_mm]
        if window.opinion.rounded_mm is not None:
            values.append(float(window.opinion.rounded_mm))
        authorized[basin.name] = tuple(sorted(set(values)))
    return authorized


def _distance_between(left: tuple[int, int], right: tuple[int, int]) -> int:
    if left[1] <= right[0]:
        return right[0] - left[1]
    if right[1] <= left[0]:
        return left[0] - right[1]
    return 0


def _clause_spans(paragraph: str) -> tuple[tuple[int, int], ...]:
    spans = []
    start = 0
    for boundary in _CLAUSE_BOUNDARY.finditer(paragraph):
        if boundary.start() > start:
            spans.append((start, boundary.start()))
        start = boundary.end()
    if start < len(paragraph):
        spans.append((start, len(paragraph)))
    return tuple(spans)


def _closest_mm_index(
    clause: str,
    mm_matches: list[re.Match[str]],
    basin_match: re.Match[str],
    basin_matches: list[tuple[str, re.Match[str]]],
) -> int | None:
    # Also support basin-first constructions with an explicit value predicate,
    # e.g. `no Iguaçu ... chegando a 10 mm` or `no Madeira (5 mm)`.
    for index, mm_match in enumerate(mm_matches):
        if mm_match.start() < basin_match.end():
            continue
        if any(
            other_match is not basin_match
            and basin_match.end() <= other_match.start() < mm_match.start()
            for _other_name, other_match in basin_matches
        ):
            break
        connector = clause[basin_match.end() : mm_match.start()]
        if re.search(
            r"(?:\(|\b(?:cheg\w*\s+a|result\w*\s+em|acumul\w*(?:\s+\w+){0,4}\s+(?:é|fica\w*\s+em)|"
            r"fica\w*\s+em|ating\w*|totaliz\w*|produz\w*)\s*)$",
            connector,
            re.IGNORECASE,
        ):
            return index
        break

    # Prefer the explicit and required editorial construction `20 mm no
    # Uruguai`. Pure character distance would incorrectly attach `Uruguai` to
    # the following value in `20 mm no Uruguai e 25 mm no Iguaçu` because the
    # conjunction after the basin is one character shorter than ` no `.
    for index in reversed(range(len(mm_matches))):
        mm_match = mm_matches[index]
        if mm_match.end() > basin_match.start():
            continue
        connector = clause[mm_match.end() : basin_match.start()]
        if _VALUE_TO_BASIN_CONNECTOR.fullmatch(connector):
            return index
        # One rounded value can legitimately govern a short list, as in
        # `10 mm no Baixo Tocantins e no Xingu`.  In that construction the
        # connector contains an earlier basin before ending in the locative
        # that introduces the current basin.  Keep the association with the
        # latest preceding value, while rejecting contrast clauses that would
        # make that reading ambiguous.
        intervening_basin_matches = [
            other_match
            for _other_name, other_match in basin_matches
            if mm_match.end() <= other_match.start()
            and other_match.end() <= basin_match.start()
        ]
        list_syntax = connector
        for other_match in sorted(
            intervening_basin_matches,
            key=lambda item: item.start(),
            reverse=True,
        ):
            relative_start = other_match.start() - mm_match.end()
            relative_end = other_match.end() - mm_match.end()
            list_syntax = list_syntax[:relative_start] + list_syntax[relative_end:]
        list_words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", list_syntax)
        if (
            intervening_basin_matches
            and re.match(
                r"\s*,?\s*(?:no|na|nos|nas|em|do|da|de)\s+",
                connector,
                re.IGNORECASE,
            )
            and re.search(
                r"(?:\be\s+|,\s*)(?:no|na|nos|nas|em|do|da|de)\s+$",
                connector,
                re.IGNORECASE,
            )
            and all(
                word.casefold() in {"no", "na", "nos", "nas", "em", "do", "da", "de", "e"}
                for word in list_words
            )
        ):
            return index
        break

    # If the clause contains an explicit `valor mm no/na bacia` construction,
    # do not let a basin mentioned earlier in the narrative attach itself to
    # that value by mere proximity. Example: `Jacuí e Uruguai ... 70 mm no
    # Jacuí` must not validate 70 mm against Uruguai too.
    has_explicit_value_to_basin = any(
        any(
            following_match.start() >= mm_match.end()
            and _VALUE_TO_BASIN_CONNECTOR.fullmatch(
                clause[mm_match.end() : following_match.start()]
            )
            for _following_name, following_match in basin_matches
        )
        for mm_match in mm_matches
    )
    if has_explicit_value_to_basin:
        return None

    basin_span = basin_match.span()
    return min(
        range(len(mm_matches)),
        key=lambda index: (
            _distance_between(mm_matches[index].span(), basin_span),
            0 if mm_matches[index].end() <= basin_match.start() else 1,
            index,
        ),
    )


def _validate_supported_mm_values(
    packet: AnalysisPacket,
    window_id: WindowId,
    paragraph: str,
) -> None:
    """Bind every mm expression to its nearest basin in the same clause.

    The association is deliberately local. A value authorized for one named
    basin cannot validate a swapped value assigned to another basin elsewhere
    in the sentence.
    """

    authorized = _authorized_basin_mm_values(packet, window_id)
    basin_patterns = tuple(
        (name, re.compile(rf"(?<!\w){re.escape(name)}(?!\w)"))
        for name in sorted(authorized, key=len, reverse=True)
    )
    for clause_start, clause_end in _clause_spans(paragraph):
        clause = paragraph[clause_start:clause_end]
        mm_matches = list(_MM_NUMBERS.finditer(clause))
        if not mm_matches:
            continue
        basin_matches = [
            (name, match)
            for name, pattern in basin_patterns
            for match in pattern.finditer(clause)
        ]
        if not basin_matches:
            raise SummaryValidationError(
                "mm_without_basin",
                "every mm value must be associated with a canonical basin in the same clause",
            )

        assigned: dict[int, list[str]] = {index: [] for index in range(len(mm_matches))}
        for basin_name, basin_match in basin_matches:
            closest_index = _closest_mm_index(
                clause,
                mm_matches,
                basin_match,
                basin_matches,
            )
            if closest_index is not None:
                assigned[closest_index].append(basin_name)

        for index, mm_match in enumerate(mm_matches):
            basin_names = tuple(dict.fromkeys(assigned[index]))
            if not basin_names:
                rendered = mm_match.group(0)
                raise SummaryValidationError(
                    "mm_without_basin",
                    f"mm value is not locally associated with a canonical basin: {rendered}",
                )
            values = tuple(
                float(token.replace(",", "."))
                for token in (mm_match.group("first"), mm_match.group("second"))
                if token is not None
            )
            for basin_name in basin_names:
                supported = authorized[basin_name]
                unsupported = [
                    value
                    for value in values
                    if not any(
                        abs(value - candidate) <= MM_SUPPORT_TOLERANCE
                        for candidate in supported
                    )
                ]
                if unsupported:
                    rendered = ", ".join(f"{value:g} mm" for value in unsupported)
                    allowed = ", ".join(f"{value:g}" for value in supported)
                    raise SummaryValidationError(
                        "unsupported_mm_value",
                        f"{rendered} is not an authorized opinion value for {basin_name}; "
                        f"allowed: {allowed} mm",
                    )


def _validate_plain_prose(paragraph: str) -> None:
    if _LOWERCASE_SENTENCE_START.search(paragraph):
        raise SummaryValidationError(
            "lowercase_sentence_start",
            "each sentence must start with an uppercase letter",
        )
    if _INVALID_NUMBER_AGREEMENT.search(paragraph):
        raise SummaryValidationError(
            "invalid_number_agreement",
            "subject, verb and weather-state number must agree",
        )
    if _BULLET_OR_LIST.search(paragraph):
        raise SummaryValidationError("list_not_allowed", "bullets and numbered lists are prohibited")
    if "|" in paragraph:
        raise SummaryValidationError("table_not_allowed", "table or pipe formatting is prohibited")
    if (
        "```" in paragraph
        or "**" in paragraph
        or "__" in paragraph
        or _MARKDOWN_LINK.search(paragraph)
        or _HTML_TAG.search(paragraph)
        or paragraph.startswith("#")
    ):
        raise SummaryValidationError("markdown_not_allowed", "reader output must be plain prose")
    if re.search(r"\b(?:fontes?|referências?|notas?|metadados?)\s*:", paragraph, re.IGNORECASE):
        raise SummaryValidationError("metadata_not_allowed", "sources and notes belong in audit metadata")
    if re.search(
        r"\b(?:qualidade|incerteza|ressalva|limitaç(?:ão|ões))\s+operacional(?:is)?\b",
        paragraph,
        re.IGNORECASE,
    ):
        raise SummaryValidationError(
            "reader_quality_metadata_not_allowed",
            "internal operational quality metadata must not appear in reader prose",
        )
    hydrological_claim = _UNSUPPORTED_HYDROLOGICAL_OUTCOME.search(paragraph)
    if hydrological_claim:
        raise SummaryValidationError(
            "unsupported_hydrological_claim",
            "hydrological outcomes require a separate hydrological model or observed flow input: "
            f"{hydrological_claim.group(0)!r}",
        )
    soil_claim = _UNSUPPORTED_SOIL_STATE.search(paragraph)
    if soil_claim:
        raise SummaryValidationError(
            "unsupported_soil_state_claim",
            "observed precipitation alone does not establish soil saturation: "
            f"{soil_claim.group(0)!r}",
        )
    model_name = _PROHIBITED_MODEL_NAME.search(paragraph)
    if model_name:
        raise SummaryValidationError(
            "model_name_not_allowed",
            f"reader output can cite only authorized forecast models: {model_name.group(0)!r}",
        )
    if _MECHANISM_ABSENCE.search(paragraph):
        raise SummaryValidationError(
            "mechanism_absence_not_allowed",
            "when no mechanism is authorized, describe the rain pattern directly instead of announcing its absence",
        )
    if _VERTICAL_THERMAL_INVERSION.search(paragraph):
        raise SummaryValidationError(
            "unsupported_vertical_thermal_inversion",
            "use 'inversão do padrão térmico' for an opposing regional change; "
            "the packet does not diagnose a vertical thermal inversion",
        )
    if re.search(r"\d(?:mm|°C|m/s)\b", paragraph):
        raise SummaryValidationError("unit_spacing", "use one space between every number and unit")
    if re.search(r"(?<![\w.,])-?\d+[.,]0+\s+mm\b", paragraph):
        raise SummaryValidationError(
            "integer_mm_has_decimal",
            "integer millimetre values must be written without a redundant decimal",
        )
    if not re.search(r"[.!?]$", paragraph) or _INCOMPLETE_ENDING.search(paragraph):
        raise SummaryValidationError(
            "incomplete_sentence_ending",
            "each paragraph must end with a complete sentence",
        )


def _validate_point_mean_distinction(paragraph: str) -> None:
    if not _POINT_CONCEPT.search(paragraph):
        return
    mm_values = _MM_VALUE.findall(paragraph)
    if not mm_values:
        return
    if re.search(r"\bmáxim\w*\b", paragraph, re.IGNORECASE) and not _EXPLICIT_POINT.search(paragraph):
        raise SummaryValidationError(
            "point_max_not_labeled",
            "a grid maximum must be explicitly labeled as a point maximum",
        )
    if len(mm_values) >= 2 and not _EXPLICIT_BASIN_MEAN.search(paragraph):
        raise SummaryValidationError(
            "basin_mean_not_labeled",
            "when a point maximum and basin accumulation are both quantified, label the basin mean explicitly",
        )


def _validate_third_window(paragraph: str) -> None:
    if re.search(r"\bquatro\s+dias\b|\b4\s+dias\b", paragraph, re.IGNORECASE):
        raise SummaryValidationError("third_window_is_five_days", "the third window always has five days")


def _validate_synoptic_evidence(
    paragraph: str,
    packet: AnalysisPacket,
    window_id: WindowId,
) -> None:
    diagnostic = getattr(packet.synoptic_diagnostics, window_id.value)
    supported = {
        canonical
        for feature in (
            *diagnostic.supported_features,
            *(item.canonical_name for item in diagnostic.structured_features),
        )
        if (canonical := _canonical_mechanism(feature)) is not None
    }
    unsupported = []
    for match in _NAMED_SYNOPTIC_MECHANISM.finditer(paragraph):
        named = _canonical_mechanism(match.group(0))
        if named is None:
            unsupported.append(match.group(0))
            continue
        # A structured subtype supports a deliberately generic `frente`, but a
        # generic feature does not authorize inventing `frente fria/quente`.
        if named == "frente":
            present = any(item in {"frente", "frente fria", "frente quente"} for item in supported)
        else:
            present = named in supported
        if not present:
            unsupported.append(match.group(0))
    if unsupported:
        raise SummaryValidationError(
            "unsupported_synoptic_mechanism",
            "named synoptic mechanisms require the corresponding structured feature "
            f"in the packet window: {', '.join(dict.fromkeys(unsupported))}",
        )


_PORTUGUESE_WEEKDAYS = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)


def _validate_required_transition_timing(
    paragraph: str,
    packet: AnalysisPacket,
    window_id: WindowId,
) -> None:
    diagnostic = getattr(packet.synoptic_diagnostics, window_id.value)
    for feature in diagnostic.structured_features:
        if feature.lifecycle.casefold() != "developing" or feature.continues_into is None:
            continue
        start = feature.start_utc
        date_token = start.strftime("%d/%m")
        weekday_token = _PORTUGUESE_WEEKDAYS[start.weekday()]
        if date_token not in paragraph or not re.search(
            rf"\b{re.escape(weekday_token)}\b",
            paragraph,
            re.IGNORECASE,
        ):
            raise SummaryValidationError(
                "synoptic_transition_timing_missing",
                "a developing system that continues into the next window must state "
                f"its authorized start as {weekday_token}, {date_token}",
            )


def _validate_antecedent_transition_language(
    paragraph: str,
    packet: AnalysisPacket,
    window_id: WindowId,
) -> None:
    if window_id != WindowId.P1:
        return

    qualifying_basins = []
    for basin in packet.basins:
        if basin.observed is None or basin.windows is None:
            continue
        antecedent = basin.observed.rain_5d_mm
        opinion = basin.windows.p1.opinion
        forecast_p1 = (
            float(opinion.rounded_mm)
            if opinion.rounded_mm is not None
            else sum(float(value) for value in opinion.range_mm) / len(opinion.range_mm)
            if opinion.range_mm
            else None
        )
        daily_timing = basin.windows.p1.daily_timing
        first_day_mm = (
            daily_timing[0].deterministic_center_mm
            if daily_timing
            else None
        )
        if (
            antecedent is not None
            and float(antecedent) < 15.0
            and forecast_p1 is not None
            and forecast_p1 >= 25.0
            and first_day_mm is not None
            and float(first_day_mm) >= 5.0
        ):
            qualifying_basins.append(basin)

    if not qualifying_basins:
        return

    for clause in re.split(r"[.;!?]", paragraph):
        if not _AMBIGUOUS_DRY_TO_WET_TRANSITION.search(clause):
            continue
        if not any(
            re.search(
                rf"(?<!\w){re.escape(basin.name)}(?!\w)",
                clause,
                re.IGNORECASE,
            )
            for basin in qualifying_basins
        ):
            continue
        if (
            _EXPLICIT_OBSERVED_ANTECEDENT.search(clause)
            and _EXPLICIT_FIRST_FORECAST_DAY.search(clause)
        ):
            continue
        raise SummaryValidationError(
            "ambiguous_antecedent_transition",
            "a dry-to-wet comparison with rain on the first forecast day must "
            "identify the observed antecedent and say that precipitation returns "
            "already on the first day of the pentad",
        )


def _validate_model_evidence(
    paragraph: str,
    packet: AnalysisPacket,
    window_id: WindowId,
) -> None:
    if not (
        _AUTHORIZED_MODEL_NAME.search(paragraph)
        or _MODEL_COMPARISON_LANGUAGE.search(paragraph)
    ):
        return

    # Import locally to keep the schema/validator boundary acyclic while using
    # exactly the same two-family contrast calculation sent to the editor.
    from .openai_client import compact_editorial_packet

    brief = compact_editorial_packet(packet, window_id)
    highlights = brief["basin_highlights"][window_id.value]
    if not any("model_views" in item for item in highlights):
        raise SummaryValidationError(
            "unsupported_model_comparison",
            "model citations or comparisons require a moderate or strong contrast "
            "between the independent GFS/GEFS and EC/ECENS families",
        )


def _canonical_mechanism(value: str) -> str | None:
    normalized = _strip_accents(str(value or "")).casefold()
    normalized = re.sub(r"[_-]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if re.fullmatch(
        r"(?:atividade|instabilidade|precipitacao|chuva)\s+convectiv(?:o|a|os|as)",
        normalized,
    ):
        return "conveccao"
    return _MECHANISM_ALIASES.get(normalized)


def _wind_material_claim(paragraph: str) -> bool:
    if _WIND_SECTOR.search(paragraph):
        return True
    for match in _WIND_WORD.finditer(paragraph):
        sentence_end = _CLAUSE_BOUNDARY.search(paragraph, match.end())
        end = sentence_end.start() if sentence_end else len(paragraph)
        context = paragraph[match.start() : min(end, match.end() + 100)]
        if _WIND_OPERATIONAL_CONTEXT.search(context):
            return True
    return False


def _energy_claim_categories(paragraph: str) -> tuple[str, ...]:
    categories = []
    if _wind_material_claim(paragraph):
        categories.append("wind")
    if _TEMPERATURE_TREND.search(paragraph) or _LOAD_SECTOR.search(paragraph):
        categories.append("temperature")
    if _SOLAR_SECTOR.search(paragraph):
        categories.append("solar")
    if _SEVERE_WEATHER.search(paragraph):
        categories.append("severe_weather")
    return tuple(categories)


def _validate_energy_evidence(
    paragraph: str,
    packet: AnalysisPacket,
    window_id: WindowId,
) -> None:
    labels = {
        "wind": "vento/eólica",
        "temperature": "temperatura/carga",
        "solar": "solar",
        "severe_weather": "tempo severo",
    }
    for category in _energy_claim_categories(paragraph):
        if category == "temperature" and not _LOAD_SECTOR.search(paragraph):
            diagnostics = getattr(
                packet.synoptic_diagnostics,
                window_id.value,
            ).regional_temperature
            if diagnostics:
                if _BELOW_AVERAGE_TEMPERATURE.search(paragraph) and not any(
                    item.start_regime == "below_average"
                    or item.end_regime == "below_average"
                    for item in diagnostics
                ):
                    raise SummaryValidationError(
                        "unsupported_temperature_regime",
                        "temperature below average requires a matching regional diagnostic",
                    )
                if _ABOVE_AVERAGE_TEMPERATURE.search(paragraph) and not any(
                    item.start_regime == "above_average"
                    or item.end_regime == "above_average"
                    for item in diagnostics
                ):
                    raise SummaryValidationError(
                        "unsupported_temperature_regime",
                        "temperature above average requires a matching regional diagnostic",
                    )
                if _WARMING_TEMPERATURE.search(paragraph) and not any(
                    item.trend == "warming" for item in diagnostics
                ):
                    raise SummaryValidationError(
                        "unsupported_temperature_trend",
                        "warming requires a matching regional temperature diagnostic",
                    )
                if _COOLING_TEMPERATURE.search(paragraph) and not any(
                    item.trend == "cooling" for item in diagnostics
                ):
                    raise SummaryValidationError(
                        "unsupported_temperature_trend",
                        "cooling requires a matching regional temperature diagnostic",
                    )
                continue
        signals = getattr(packet.energy_signals, category)
        if not any(signal.window_id == window_id for signal in signals):
            raise SummaryValidationError(
                "unsupported_energy_claim",
                f"material claim about {labels[category]} requires a material "
                f"energy_signals.{category} entry for {window_id.value}",
            )


def _word_count(paragraph: str) -> int:
    return len(re.findall(r"\b[\wÀ-ÖØ-öø-ÿ]+(?:[-'][\wÀ-ÖØ-öø-ÿ]+)*\b", paragraph))


def _normalize_editorial_text(text: str, packet: AnalysisPacket) -> str:
    text = _normalize_basin_editorial_references_until_stable(text, packet)
    locative_refs = sorted(
        {basin.editorial_reference for basin in packet.basins},
        key=len,
        reverse=True,
    )
    text = re.sub(
        rf"\bentre\s+(?=(?:{'|'.join(re.escape(item) for item in locative_refs)})(?!\w))",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # ``sobre no/na`` is never grammatical in the locative constructions used
    # in the bulletin. Treat it before semantic validation so an otherwise
    # supported basin mention does not reach publication malformed.
    text = re.sub(r"\bsobre\s+(n[oa]s?)\b", r"\1", text, flags=re.IGNORECASE)
    text = _normalize_mechanism_absence_clauses(text)
    text = _normalize_observed_rain_language(text)
    text = _normalize_internal_forecast_language(text)
    return _normalize_repeated_qualified_basin_shorthand(text, packet)


def _validate_section(
    paragraph: str,
    packet: AnalysisPacket,
    window_id: WindowId,
    heading: str,
) -> SummarySection:
    if paragraph != paragraph.strip():
        raise SummaryValidationError("paragraph_whitespace", "paragraphs cannot start or end with spaces")
    _validate_plain_prose(paragraph)
    _validate_basin_names(paragraph, packet)
    canonical_names = _available_canonical_names(paragraph, packet)
    if not canonical_names:
        raise SummaryValidationError(
            "available_basin_missing",
            "each section must name at least one canonical basin with operational geometry",
        )
    mm_values = _paragraph_mm_values(paragraph)
    if not mm_values:
        raise SummaryValidationError(
            "basin_accumulation_missing",
            "each section must quantify at least one supported basin accumulation in mm",
        )
    _validate_point_mean_distinction(paragraph)
    _validate_supported_mm_values(packet, window_id, paragraph)
    _validate_antecedent_transition_language(paragraph, packet, window_id)
    _validate_synoptic_evidence(paragraph, packet, window_id)
    _validate_required_transition_timing(paragraph, packet, window_id)
    _validate_model_evidence(paragraph, packet, window_id)
    _validate_energy_evidence(paragraph, packet, window_id)
    if window_id == WindowId.P3:
        _validate_third_window(paragraph)
    if not _CONFIDENCE_LANGUAGE.search(paragraph):
        raise SummaryValidationError(
            "confidence_language_missing",
            "each section must communicate forecast confidence or uncertainty",
        )
    if _AMBIGUOUS_CONFIDENCE.search(paragraph):
        raise SummaryValidationError(
            "ambiguous_confidence_language",
            "use an explicit confidence level instead of 'confiança mista'",
        )
    if _VAGUE_MODEL_GROUP.search(paragraph):
        raise SummaryValidationError(
            "vague_model_group",
            "cite the authorized model name instead of a vague model group",
        )
    word_count = _word_count(paragraph)
    if word_count < MIN_PARAGRAPH_WORDS:
        raise SummaryValidationError(
            "paragraph_too_short",
            f"each section must contain at least {MIN_PARAGRAPH_WORDS} words",
        )
    if word_count > MAX_PARAGRAPH_WORDS:
        raise SummaryValidationError(
            "paragraph_too_long",
            f"each section must contain at most {MAX_PARAGRAPH_WORDS} words",
        )
    return SummarySection(
        window_id=window_id,
        heading=heading,
        paragraph=paragraph,
        word_count=word_count,
        canonical_basin_names=list(canonical_names),
        mm_values=list(mm_values),
    )


def validate_window_output(
    text: str,
    packet: AnalysisPacket | Mapping[str, Any],
    window_id: WindowId | str,
) -> SummarySection:
    """Validate one generated pentad before it is combined with the others."""

    validated_packet = packet if isinstance(packet, AnalysisPacket) else AnalysisPacket.model_validate(packet)
    selected = WindowId(window_id)
    if not isinstance(text, str) or not text:
        raise SummaryValidationError("empty_output", "summary output is empty")
    normalized = _normalize_editorial_text(text, validated_packet)
    if _MALFORMED_PUNCTUATION.search(normalized):
        raise SummaryValidationError(
            "malformed_punctuation",
            "summary must not contain incompatible consecutive punctuation",
        )
    if normalized != normalized.strip():
        raise SummaryValidationError("outer_whitespace", "summary must not contain leading or trailing blank space")
    if "\r" in normalized:
        raise SummaryValidationError("line_endings", "summary must use canonical LF line endings")
    heading = _expected_heading(validated_packet, selected, int(selected.value[-1]))
    match = re.fullmatch(rf"{re.escape(heading)}\n(?P<paragraph>[^\n]+)", normalized)
    if not match:
        raise SummaryValidationError(
            "invalid_structure",
            "output must contain the requested dated heading and one single-line paragraph",
        )
    return _validate_section(match.group("paragraph"), validated_packet, selected, heading)


def validate_summary_output(
    text: str,
    packet: AnalysisPacket | Mapping[str, Any],
) -> ValidatedSummary:
    """Validate and parse the exact three-section public format.

    The function is intentionally fail-closed: callers publish only the
    returned ``ValidatedSummary.text``. Any deviation raises a coded
    ``SummaryValidationError``.
    """

    validated_packet = packet if isinstance(packet, AnalysisPacket) else AnalysisPacket.model_validate(packet)
    if not isinstance(text, str) or not text:
        raise SummaryValidationError("empty_output", "summary output is empty")
    text = _normalize_editorial_text(text, validated_packet)
    if _MALFORMED_PUNCTUATION.search(text):
        raise SummaryValidationError(
            "malformed_punctuation",
            "summary must not contain incompatible consecutive punctuation",
        )
    if text != text.strip():
        raise SummaryValidationError(
            "outer_whitespace",
            "summary must not contain leading or trailing blank space",
        )
    if "\r" in text:
        raise SummaryValidationError("line_endings", "summary must use canonical LF line endings")

    headings = (
        _expected_heading(validated_packet, WindowId.P1, 1),
        _expected_heading(validated_packet, WindowId.P2, 2),
        _expected_heading(validated_packet, WindowId.P3, 3),
    )
    pattern = re.compile(
        rf"\A{re.escape(headings[0])}\n(?P<p1>[^\n]+)\n\n"
        rf"{re.escape(headings[1])}\n(?P<p2>[^\n]+)\n\n"
        rf"{re.escape(headings[2])}\n(?P<p3>[^\n]+)\Z"
    )
    match = pattern.fullmatch(text)
    if not match:
        raise SummaryValidationError(
            "invalid_structure",
            "output must contain exactly the three dated headings and one single-line paragraph per section",
        )

    sections: list[SummarySection] = []
    for window_id, heading, group_name in zip(
        (WindowId.P1, WindowId.P2, WindowId.P3),
        headings,
        ("p1", "p2", "p3"),
    ):
        sections.append(
            _validate_section(
                match.group(group_name),
                validated_packet,
                window_id,
                heading,
            )
        )

    return ValidatedSummary(
        text=text,
        sections=sections,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        mm_support_tolerance=MM_SUPPORT_TOLERANCE,
    )
__all__ = [
    "SummarySection",
    "SummaryValidationError",
    "ValidatedSummary",
    "MAX_PARAGRAPH_WORDS",
    "MIN_PARAGRAPH_WORDS",
    "MM_SUPPORT_TOLERANCE",
    "validate_window_output",
    "validate_summary_output",
]
