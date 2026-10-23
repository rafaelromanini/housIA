"""Conferência da passagem verbal: o que foi dito × o que o prontuário registra.

Inspirado em ferramentas de captura da consulta (benchmark em docs/benchmark-lya.md), mas focado na transição:
aponta fatos críticos não mencionados (omissões) e relatos que contradizem os registros (divergências).
"""

import re
from typing import Literal

from pydantic import BaseModel

from app.handoff.schemas import Fact
from app.timeline.events import ClinicalEvent
from app.timeline.record import PatientRecord, normalize

Status = Literal["mencionado", "omitido", "divergente"]

TYPE_KEYWORDS = {
    "diurese": ["diurese", "urin", "svd", "sonda vesical", "miccao", "globo", "xixi"],
    "risco_queda": ["queda", "morse", "grade"],
    "lesao_por_pressao": ["braden", "lesao por pressao", "lpp", "decubito"],
    "acesso": ["acesso", "avp", "jelco", "puncao", "cateter venoso", "venoso"],
    "transferencia": ["transfer", "veio da", "veio do", "chegou d", "recebid"],
}
MEASURE_KEYWORDS = {
    "pa": ["pa ", "pa:", "pa,", "pressao", "hipertens", "pico", "pas "],
    "pas": ["pa ", "pa:", "pa,", "pressao", "hipertens", "pico"],
    "pad": ["pa ", "pressao", "hipertens"],
    "fc": ["fc", "frequencia cardiaca", "taquicard", "bradicard", "pulso"],
    "fr": ["fr ", "fr:", "respirat", "bradipn", "taquipn"],
    "spo2": ["sat", "spo2", "oxigen", "o2"],
    "temp": ["febr", "temperat", "tax", "afebril"],
    "glicemia": ["glicem", "hgt", "dextro", "insulin"],
    "dor": ["dor", "algia", "analges"],
}
LAB_KEYWORDS = {
    "hemoglobina": ["hemoglob", "hb ", "hb:", "anemia", "sangramento"],
    "leucocitos": ["leucocit", "leuco"],
    "pcr": ["pcr", "proteina c"],
    "potassio": ["potassio", "k ", "k+", "hipercalem", "hipocalem"],
    "creatinina": ["creatinin", "funcao renal", "cr "],
    "glicose": ["glicose", "glicem"],
}
STABILITY = ["estavel", "sem intercorr", "sem alterac", "tranquil", "normotens", "sem queixa", "bem", "ok"]


class CheckItem(BaseModel):
    fato: str
    status: Status
    severidade: Literal["info", "atencao", "critico"]
    comentario: str
    refs: list[str]


class HandoffCheckOutput(BaseModel):
    resumo: str
    itens: list[CheckItem]


def fact_keywords(fact: Fact, record: PatientRecord, events_by_ref: dict[str, ClinicalEvent]) -> list[str]:
    words: list[str] = list(TYPE_KEYWORDS.get(fact.tipo, []))
    if fact.tipo == "tarefa_aberta":  # o assunto da tarefa (#47): as palavras do título, sem o responsável e o prazo
        title = normalize(fact.texto.split(" — ", 1)[0])
        words += [w for w in re.findall(r"[a-z0-9]{5,}", title) if w not in ("solicitar", "retorno", "ligar")]
    meds = {m.ref: m for m in record.medications}
    for ref in fact.refs:
        if ref in meds:
            words.append(normalize(meds[ref].name).split(" ")[0])
        ev = events_by_ref.get(ref)
        key = (ev.value or {}).get("chave") if ev else None
        if key:
            words += MEASURE_KEYWORDS.get(key, []) + LAB_KEYWORDS.get(key, [])
        if ref.startswith("AllergyIntolerance/"):
            words.append("alerg")
    return list(dict.fromkeys(w for w in words if w))


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.;\n!?]", normalize(text)) if s.strip()]


def mock_check(
    text: str, facts: list[Fact], audience: str, record: PatientRecord, events_by_ref: dict[str, ClinicalEvent]
) -> dict:
    """Conferência determinística por palavras-chave (modo mock)."""
    sentences = _sentences(f" {text} ")
    items = []
    relevant = [f for f in facts if audience in f.publico and f.severidade in ("critico", "atencao") and f.refs]
    for f in relevant:
        keys = fact_keywords(f, record, events_by_ref)
        hits = [s for s in sentences if any(k in f" {s} " for k in keys)]
        if not hits:
            items.append(
                {
                    "fato": f.texto,
                    "status": "omitido",
                    "severidade": f.severidade,
                    "comentario": "Não aparece no relato verbal.",
                    "refs": f.refs,
                }
            )
        elif f.severidade == "critico" and any(any(w in s for w in STABILITY) for s in hits):
            items.append(
                {
                    "fato": f.texto,
                    "status": "divergente",
                    "severidade": f.severidade,
                    "comentario": "O relato descreve estabilidade, mas o registro é crítico.",
                    "refs": f.refs,
                }
            )
        else:
            items.append(
                {
                    "fato": f.texto,
                    "status": "mencionado",
                    "severidade": f.severidade,
                    "comentario": "Citado no relato.",
                    "refs": f.refs,
                }
            )
    omitted = [i for i in items if i["status"] == "omitido"]
    critical_omitted = [i for i in omitted if i["severidade"] == "critico"]
    diverging = [i for i in items if i["status"] == "divergente"]
    resumo = (
        f"{len(items) - len(omitted) - len(diverging)} de {len(items)} fatos relevantes citados; "
        f"{len(omitted)} omitido(s) ({len(critical_omitted)} crítico(s)) e {len(diverging)} divergência(s)."
    )
    return {"resumo": resumo, "itens": items}
