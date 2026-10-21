"""Glossário clínico brasileiro (#27): fala popular → termo técnico.

Duas camadas: o glossário comum (data/reference/glossario_clinico.yaml) e o do pacote da especialidade. Vale em dois
lugares: na nota (as citações do paciente e os rascunhos por palavras saem com o termo técnico: "xixi" vira
"diurese") e no prompt do reconhecimento de fala (STT), para o motor acertar os termos técnicos que o profissional usa.
A fala original nunca é alterada: a conversa e as refs `Fala/N` seguem com o que foi dito.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

import yaml

from app.config import get_settings

MAX_PROMPT_TERMS = 80


def _fold(text: str) -> str:
    """Minúsculas e sem acento, com o mesmo tamanho do texto (para devolver os trechos na posição certa)."""
    out = []
    for ch in text.replace("’", "'"):
        base = unicodedata.normalize("NFD", ch)[0]
        out.append(base.lower() if base.isascii() else ch.lower())
    return "".join(out)


def _key(term: str) -> str:
    return re.sub(r"\s+", " ", _fold(term)).strip()


@lru_cache
def comum() -> dict[str, str]:
    with open(get_settings().data_dir / "reference" / "glossario_clinico.yaml", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return {_key(k): v for k, v in data["termos"].items()}


@lru_cache
def termos(specialty: str = "") -> dict[str, str]:
    """Popular (sem acento) → técnico, do glossário comum mais o do pacote; o pacote tem a última palavra."""
    from app.consult import knowledge  # import tardio: knowledge também lê dados de referência

    out = dict(comum())
    if specialty in knowledge.SPECIALTIES:
        out |= {_key(k): v for k, v in knowledge.specialty_pack(specialty).glossario.items()}
    return out


@lru_cache
def _pattern(specialty: str) -> re.Pattern | None:
    keys = sorted(termos(specialty), key=len, reverse=True)
    if not keys:
        return None
    alt = "|".join(re.escape(k).replace(r"\ ", r"\s+") for k in keys)
    return re.compile(rf"(?<![a-z0-9]){'(' + alt + ')'}(?![a-z0-9])")


def tecnico(text: str, specialty: str = "") -> tuple[str, list[tuple[str, str]]]:
    """Texto com os termos populares trocados pelos técnicos, e a lista (dito, técnico) do que foi trocado."""
    pattern = _pattern(specialty)
    if not text or pattern is None:
        return text, []
    table = termos(specialty)
    folded = _fold(text)
    out, last, swaps = [], 0, []
    for m in pattern.finditer(folded):
        tech = table[re.sub(r"\s+", " ", m.group(1))]
        said = text[m.start() : m.end()]
        if said[:1].isupper() and not tech[:1].isupper():
            tech = tech[:1].upper() + tech[1:]
        out += [text[last : m.start()], tech]
        swaps.append((said, tech))
        last = m.end()
    out.append(text[last:])
    return "".join(out), swaps


def prompt_stt(specialty: str = "") -> str:
    """Dica de vocabulário para o STT (campo `prompt` da API de transcrição compatível com Whisper)."""
    tech = list(dict.fromkeys(termos(specialty).values()))[:MAX_PROMPT_TERMS]
    if not tech:
        return ""
    return "Consulta clínica em português do Brasil. Vocabulário: " + ", ".join(tech) + "."


NOTE_SECTIONS = ("queixa_principal", "hda", "antecedentes")  # as seções que reproduzem a fala do paciente


def aplicar_nota(note, specialty: str = "") -> list[tuple[str, str]]:
    """Troca, no texto da nota, o termo popular pelo técnico (seções que citam o paciente e seções do modelo).

    Mexe só no texto e nas frases: as refs `Fala/N` seguem apontando para a fala original, que não é alterada.
    Devolve os pares (dito, técnico) trocados, para auditoria.
    """
    swaps: list[tuple[str, str]] = []

    def fix(section) -> None:
        section.texto, found = tecnico(section.texto, specialty)
        swaps.extend(found)
        for f in getattr(section, "frases", []):
            f.texto, found = tecnico(f.texto, specialty)
            swaps.extend(found)

    for name in NOTE_SECTIONS:
        fix(getattr(note, name))
    for extra in note.secoes:
        fix(extra)
    return swaps
