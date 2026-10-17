"""Idiomas da consulta (data/reference/languages.csv): BCP-47 com rótulo e os códigos da Web Speech e do Whisper.

O idioma deixa de ser uma lista fixa no código: entra qualquer código do CSV (≥ 95 idiomas). Também resolve o idioma
preferido do paciente (Patient.communication) e prepara a fala estrangeira para as regras quando ainda não há tradução.
"""

import re
from dataclasses import dataclass
from functools import lru_cache

from app.reference import _read_csv
from app.timeline.record import normalize

PADRAO = "pt-BR"
MARCA_MOCK = " — tradução com LLM real"  # aviso que o modo mock põe numa fala sem tradução pré-gravada


def placeholder(traducao: str | None) -> bool:
    return bool(traducao) and MARCA_MOCK in traducao


@dataclass(frozen=True)
class Idioma:
    codigo: str
    rotulo: str
    web_speech: str  # reconhecimento e voz do navegador ("" quando o navegador não cobre)
    whisper: str  # código do Whisper para a transcrição no servidor ("" quando não cobre)


@lru_cache
def todos() -> dict[str, Idioma]:
    return {
        r["codigo"]: Idioma(r["codigo"], r["rotulo"], r.get("web_speech", ""), r.get("whisper", ""))
        for r in _read_csv("languages.csv")
    }


def resolver(codigo: str | None) -> str | None:
    """Código canônico do CSV para `pt`, `PT-br`, `es-MX`, `zh_CN`…; None se o idioma não existe."""
    if not codigo:
        return None
    raw = codigo.strip().replace("_", "-")
    known = todos()
    by_lower = {k.lower(): k for k in known}
    if raw.lower() in by_lower:
        return by_lower[raw.lower()]
    base = raw.split("-")[0].lower()
    if base == "pt":
        return PADRAO
    return by_lower.get(base)


def validar(codigo: str) -> str:
    """Validador do schema: devolve o código canônico ou recusa o idioma desconhecido."""
    found = resolver(codigo)
    if found is None:
        raise ValueError(f"Idioma desconhecido: {codigo!r} (use um código de data/reference/languages.csv)")
    return found


def rotulo(codigo: str) -> str:
    found = todos().get(resolver(codigo) or codigo)
    return found.rotulo if found else codigo


def opcoes() -> list[dict]:
    return [
        {"value": i.codigo, "label": i.rotulo, "web_speech": i.web_speech or i.codigo, "whisper": i.whisper}
        for i in todos().values()
    ]


def do_paciente(patient: dict) -> tuple[str | None, str]:
    """Idioma preferido em Patient.communication (preferred=true, senão o primeiro) e o rótulo da fonte."""
    comms = patient.get("communication") or []
    ordered = sorted(comms, key=lambda c: not c.get("preferred"))
    for comm in ordered:
        for coding in (comm.get("language") or {}).get("coding", []):
            found = resolver(coding.get("code"))
            if found:
                return found, "Patient.communication"
    return None, ""


# ---------- fala estrangeira ainda sem tradução ----------
# As regras são em português. Enquanto a tradução não chega, espanhol e inglês são lidos pelos sinônimos dos CSVs;
# a negação deles é levada ao "não" que o motor entende ("no me duele" → "nao me duele"). Em português "no" é
# "em o" ("dor no peito"), por isso a troca só vale para falas que NÃO estão em português.
_NEGA = {
    "es": re.compile(r"(?<!\w)(no|nunca|jamas|sin|ningun[oa]?|ni)(?!\w)"),
    "en": re.compile(r"(?<!\w)(no|not|never|without|none|nor|neither)(?!\w)|n't(?!\w)"),
}


def texto_para_regras(texto: str, idioma: str) -> str:
    """Texto que o motor lê numa fala estrangeira sem tradução; em pt-BR (ou outro idioma) devolve o texto como está."""
    base = (resolver(idioma) or idioma).split("-")[0]
    pattern = _NEGA.get(base)
    if pattern is None:
        return texto
    return pattern.sub(" nao ", normalize(texto))
