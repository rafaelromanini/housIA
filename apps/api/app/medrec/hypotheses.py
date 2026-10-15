"""Pilar 3 — camada de IA: sintoma novo compatível com efeito adverso ou retirada de medicação?

Detecção de sinais é determinística (sinais vitais, exames e palavras-chave de evoluções nas últimas 48h).
O LLM (ou o gerador mock) cruza sinais com as mudanças de medicação da transição. Toda hipótese é marcada
"Hipótese da IA — confirmar" e precisa citar fontes.
"""

import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from app.medrec.rules import Finding, RecRow
from app.reference import adverse_patterns, matches
from app.timeline.events import ClinicalEvent
from app.timeline.record import MedInfo, normalize

Confidence = Literal["baixa", "media", "alta"]

NOTE_KEYWORDS = {
    "agitacao": ["agitad", "agitacao", "ansios", "inquiet"],
    "taquicardia": ["palpitac", "taquicard"],
    "sedacao": ["sonolent", "sonolencia", "rebaixamento", "torporos"],
    "confusao": ["confus", "delirium", "desorientad"],
    "tosse": ["tosse"],
    "nausea": ["nausea", "vomit", "nausead"],
    "prurido": ["prurido", "coceira", "urticaria", "rash"],
    "convulsao": ["convuls"],
    "sangramento": ["sangramento", "hematuria", "melena", "hematoma", "hematemese"],
}
SIGN_LABEL = {
    "taquicardia": "taquicardia",
    "bradicardia": "bradicardia",
    "hipertensao": "elevação pressórica",
    "hipotensao": "hipotensão",
    "bradipneia": "bradipneia",
    "dessaturacao": "dessaturação",
    "febre": "febre",
    "hipoglicemia": "hipoglicemia",
    "hiperglicemia": "hiperglicemia",
    "hipercalemia": "hipercalemia",
    "hipocalemia": "hipocalemia",
    "agitacao": "agitação/ansiedade",
    "sedacao": "sonolência/sedação",
    "confusao": "confusão",
    "tosse": "tosse",
    "nausea": "náusea/vômito",
    "prurido": "prurido/urticária",
    "convulsao": "convulsão",
    "sangramento": "sangramento",
}


class Symptom(BaseModel):
    sinal: str
    texto: str
    ref: str
    timestamp: datetime
    critico: bool = False


class Hypothesis(BaseModel):
    texto: str
    medicamento: str
    mecanismo: Literal["retirada", "efeito_adverso", "interacao"]
    confidence: Confidence
    refs: list[str]


class MedrecLLMOutput(BaseModel):
    hipoteses: list[Hypothesis]


def _vital_signs(e: ClinicalEvent) -> list[tuple[str, bool]]:
    v = e.value or {}
    key = v.get("chave")
    crit = e.severity == "critico"
    if key == "pa":
        out = []
        if v["sistolica"] >= 150 or v["diastolica"] >= 95:
            out.append(("hipertensao", crit))
        if v["sistolica"] < 90:
            out.append(("hipotensao", crit))
        return out
    val = v.get("valor")
    if val is None:
        return []
    rules = {
        "fc": [("taquicardia", val > 100), ("bradicardia", val < 50)],
        "fr": [("bradipneia", val < 12)],
        "spo2": [("dessaturacao", val < 92)],
        "temp": [("febre", val >= 37.8)],
        "glicemia": [("hipoglicemia", val < 70), ("hiperglicemia", val > 250)],
        "glicose": [("hipoglicemia", val < 70), ("hiperglicemia", val > 250)],
        "potassio": [("hipercalemia", val > 5.5), ("hipocalemia", val < 3.5)],
    }
    return [(sign, crit) for sign, hit in rules.get(key, []) if hit]


def detect_symptoms(events: list[ClinicalEvent], now: datetime, hours: int = 48) -> list[Symptom]:
    since = now - timedelta(hours=hours)
    out: list[Symptom] = []
    for e in events:
        if not (since <= e.timestamp <= now):
            continue
        if e.kind in ("vital", "lab"):
            for sign, crit in _vital_signs(e):
                out.append(Symptom(sinal=sign, texto=e.summary, ref=e.source_ref, timestamp=e.timestamp, critico=crit))
        elif e.kind == "note" and e.text:
            # frases que só citam antecedentes/alergias ("alergia a dipirona: urticária") não são sinais novos
            sentences = [x for x in re.split(r"[.;\n]", normalize(e.text)) if "alergi" not in x and "nega" not in x]
            text = " ".join(sentences)
            for sign, words in NOTE_KEYWORDS.items():
                if any(w in text for w in words):
                    out.append(
                        Symptom(
                            sinal=sign,
                            texto=f"Evolução: {e.summary.split(': ', 1)[-1][:140]}",
                            ref=e.source_ref,
                            timestamp=e.timestamp,
                        )
                    )
    return out


def _change_time(row: RecRow, meds_by_ref: dict[str, MedInfo], transfer_time: datetime | None) -> datetime | None:
    if row.status in ("omitida", "suspensa"):
        ref = row.origem.ref if row.origem else None
        med = meds_by_ref.get(ref or "")
        if row.status == "suspensa" and row.destino:
            med = meds_by_ref.get(row.destino.ref) or med
        return (med.end if med and med.end else None) or transfer_time
    ref = row.destino.ref if row.destino else None
    med = meds_by_ref.get(ref or "")
    return (med.start if med else None) or transfer_time


def mock_hypotheses(
    rows: list[RecRow],
    findings: list[Finding],
    symptoms: list[Symptom],
    meds_by_ref: dict[str, MedInfo],
    transfer_time: datetime | None,
) -> dict:
    """Cruza padrões curados (data/reference/adverse_patterns.csv) com sinais posteriores à mudança."""
    out = []
    interactions = [f for f in findings if f.tipo == "interacao"]
    for row in rows:
        if row.status in ("omitida", "suspensa"):
            mechanism = "retirada"
        elif row.status in ("nova", "alterada"):
            mechanism = "efeito_adverso"
        else:
            continue
        changed_at = _change_time(row, meds_by_ref, transfer_time)
        patterns = [
            p
            for p in adverse_patterns()
            if p["tipo"] == mechanism and matches(p["medicamento"], row.principio_ativo, row.classe)
        ]
        hits: dict[str, list[Symptom]] = {}
        for p in patterns:
            for s in symptoms:
                if s.sinal == p["sinal"] and (changed_at is None or s.timestamp > changed_at):
                    hits.setdefault(p["sinal"], []).append(s)
        if not hits:
            continue
        signs = list(hits)
        score = (
            len(signs)
            + (1 if row.status == "omitida" else 0)
            + (1 if any(s.critico for group in hits.values() for s in group) else 0)
        )
        confidence: Confidence = "alta" if score >= 4 else "media" if score >= 2 else "baixa"
        refs = [r for r in row.refs] + [s.ref for group in hits.values() for s in group[-2:]]
        description = next(p["descricao"] for p in patterns if p["sinal"] == signs[0])
        what = (
            f"omissão de {row.nome} na transferência"
            if row.status == "omitida"
            else f"suspensão de {row.nome}"
            if row.status == "suspensa"
            else f"início de {row.nome}"
        )
        sign_text = ", ".join(SIGN_LABEL.get(s, s) for s in signs).capitalize()
        text = f"{sign_text} após {what}. Padrão conhecido: {description}."
        mech = mechanism
        related = [f for f in interactions if row.principio_ativo in f.medicamentos]
        if related and mechanism == "efeito_adverso":
            mech = "interacao"
            text += f" Efeito possivelmente potencializado pela associação ({related[0].titulo.split(': ', 1)[-1]})."
            refs += [r for r in related[0].refs if r not in refs]
        out.append(
            {
                "texto": text + " Confirmar clinicamente.",
                "medicamento": row.nome,
                "mecanismo": mech,
                "confidence": confidence,
                "refs": list(dict.fromkeys(refs)),
            }
        )
    return {"hipoteses": out}
