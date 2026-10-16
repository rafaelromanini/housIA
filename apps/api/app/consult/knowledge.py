"""Bases curadas da consulta assistida (data/reference/*.csv). Base de DEMONSTRAÇÃO, não exaustiva.

Elas alimentam a camada determinística dos Smart Apps: rodam sempre (com ou sem LLM) e são a referência de
segurança — a IA complementa, não substitui.
"""

import json
from dataclasses import dataclass
from functools import lru_cache

from app.config import get_settings
from app.reference import _read_csv


def _words(cell: str) -> tuple[str, ...]:
    return tuple(w for w in (cell or "").split("|") if w)


@dataclass(frozen=True)
class SymptomDef:
    key: str
    label: str
    words: tuple[str, ...]


@dataclass(frozen=True)
class EmotionDef:
    key: str
    label: str
    words: tuple[str, ...]
    approach: str


@dataclass(frozen=True)
class QuestionDef:
    trigger: str
    text: str
    reason: str
    asked_words: tuple[str, ...]
    priority: str
    specialty: str


@dataclass(frozen=True)
class DifferentialDef:
    hypothesis: str
    icd: str
    evidence: tuple[str, ...]
    minimum: int
    next_steps: str
    red_flags: str


@dataclass(frozen=True)
class AllergyDef:
    allergen: str
    label: str
    words: tuple[str, ...]
    cross: str
    avoid: str
    alternatives: str
    tips: str


@dataclass(frozen=True)
class DoseAlertDef:
    item: str
    criterion: str
    limit: float
    severity: str
    recommendation: str


@dataclass(frozen=True)
class ProtocolDef:
    protocol: str
    condition: str
    item: str
    source: str
    interval_days: int
    guidance: str


@dataclass(frozen=True)
class OpmeDef:
    code: str
    description: str
    words: tuple[str, ...]


@lru_cache
def symptoms() -> tuple[SymptomDef, ...]:
    return tuple(SymptomDef(r["chave"], r["rotulo"], _words(r["palavras"])) for r in _read_csv("consult_symptoms.csv"))


@lru_cache
def emotions() -> tuple[EmotionDef, ...]:
    return tuple(
        EmotionDef(r["emocao"], r["rotulo"], _words(r["palavras"]), r["abordagem"])
        for r in _read_csv("consult_emotions.csv")
    )


@lru_cache
def questions() -> tuple[QuestionDef, ...]:
    return tuple(
        QuestionDef(
            r["gatilho"], r["pergunta"], r["motivo"], _words(r["ja_perguntado"]), r["prioridade"], r["especialidade"]
        )
        for r in _read_csv("consult_questions.csv")
    )


@lru_cache
def differentials() -> tuple[DifferentialDef, ...]:
    return tuple(
        DifferentialDef(
            r["hipotese"],
            r["cid"],
            _words(r["evidencias"]),
            int(r["minimo"]),
            r["proximos_passos"],
            r["sinais_de_alarme"],
        )
        for r in _read_csv("consult_differentials.csv")
    )


@lru_cache
def lab_lay() -> dict[str, dict[str, str]]:
    return {r["chave"]: r for r in _read_csv("lab_lay.csv")}


@lru_cache
def allergy_defs() -> tuple[AllergyDef, ...]:
    return tuple(
        AllergyDef(
            r["alergeno"],
            r["rotulo"],
            _words(r["palavras"]),
            r["reatividade_cruzada"],
            r["evitar"],
            r["alternativas"],
            r["dicas"],
        )
        for r in _read_csv("allergy_cross.csv")
    )


@lru_cache
def dose_alerts() -> tuple[DoseAlertDef, ...]:
    return tuple(
        DoseAlertDef(r["item"], r["criterio"], float(r["limite"]), r["severidade"], r["recomendacao"])
        for r in _read_csv("dose_alerts.csv")
    )


@lru_cache
def protocols() -> tuple[ProtocolDef, ...]:
    return tuple(
        ProtocolDef(r["protocolo"], r["condicao"], r["item"], r["fonte"], int(r["intervalo_dias"]), r["orientacao"])
        for r in _read_csv("care_protocols.csv")
    )


@lru_cache
def opme_catalog() -> tuple[OpmeDef, ...]:
    return tuple(OpmeDef(r["codigo"], r["descricao"], _words(r["palavras"])) for r in _read_csv("opme_demo.csv"))


@lru_cache
def demo_scripts() -> dict[str, dict]:
    """Roteiros de demonstração (data/consultations/*.json), por id do arquivo."""
    folder = get_settings().data_dir / "consultations"
    out: dict[str, dict] = {}
    for path in sorted(folder.glob("*.json")):
        with open(path, encoding="utf-8") as fh:
            out[path.stem] = json.load(fh) | {"id": path.stem}
    return out


SPECIALTIES: dict[str, str] = {
    "clinica_medica": "Clínica médica",
    "cardiologia": "Cardiologia",
    "endocrinologia": "Endocrinologia",
    "geriatria": "Geriatria",
    "urologia": "Urologia",
    "pediatria": "Pediatria",
    "enfermagem": "Consulta de enfermagem",
    "cirurgia_geral": "Cirurgia geral",
}

SPECIALTY_FOCUS: dict[str, str] = {
    "clinica_medica": "visão integral: queixas, comorbidades, polifarmácia, prevenção e protocolos",
    "cardiologia": "dor torácica, dispneia e classe funcional, edema, palpitações, pressão e risco cardiovascular",
    "endocrinologia": "controle glicêmico, hipoglicemias, alimentação, atividade física, complicações e tireoide",
    "geriatria": "quedas, cognição, funcionalidade, polifarmácia (critérios de Beers), humor, sono e rede de apoio",
    "urologia": "sintomas do trato urinário inferior (IPSS), hematúria, incontinência, função renal e próstata",
    "pediatria": "hidratação, alimentação, febre, vacinação e desenvolvimento; doses por peso",
    "enfermagem": "autocuidado, técnica de uso de medicamentos, pele, eliminações, sono, alimentação e educação",
    "cirurgia_geral": "indicação, achados, técnica, materiais (OPME), intercorrências e documentação para faturamento",
}
