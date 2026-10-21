"""Propostas para o prontuário (#17): o que a conversa trouxe e pode virar recurso FHIR estruturado.

Nada vai sozinho: cada proposta é aceita item a item na revisão. Cada recurso carrega a fala de origem (Fala/N) e é
ligado ao Provenance do Bundle. Alergia relatada entra como `unconfirmed`; medicamento relatado, com
`informationSource` = o paciente; CID confirmado como `confirmed` e hipótese como `provisional`/`differential`;
PA e peso ditos, como Observation preliminar (valor referido, não aferido).
"""

from __future__ import annotations

import re
from datetime import datetime

from app import reference
from app.consult import text as textutil
from app.consult.schemas import Insights, Segment
from app.fhir_client import codes

TIPO_ROTULO = {
    "alergia": "Alergia relatada",
    "medicamento": "Medicamento em uso (relatado)",
    "condicao": "Diagnóstico (CID-10)",
    "observacao": "Medida referida pelo paciente",
}
TAG_SYSTEM = "urn:housia:consultation"
FALA = re.compile(r"^Fala/(\d+)$")
OBS_CATEGORY_VITALS = {"coding": [{"system": codes.OBS_CATEGORY, "code": "vital-signs", "display": "Vital Signs"}]}

PA_RE = re.compile(r"\b(\d{2,3})\s*(?:por|x|/|\bde\b)\s*(\d{2,3})\b")
PESO_RE = re.compile(r"\b(\d{2,3}(?:[.,]\d)?)\s*(?:quilos?|kilos?|kg)\b")
PESO_CTX = re.compile(r"\b(?:peso|pesa|pesando|pesei|pesava)\b")
PA_CTX = re.compile(r"\b(?:pressao|pa|aferi\w*|medi\w*)\b")


def _norm(text: str) -> str:
    return textutil.norm(text)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _norm(text)).strip("-")[:40] or "item"


def _fala_n(refs: list[str]) -> list[int]:
    return [int(m.group(1)) for r in refs if (m := FALA.match(r))]


def _evidence(segments: list[Segment], refs: list[str]) -> str:
    byn = {s.n: s for s in segments}
    quotes = [f"Fala/{n}: «{byn[n].texto.strip()[:160]}»" for n in _fala_n(refs) if n in byn]
    return " | ".join(quotes)


def _meta(cid: int, key: str) -> dict:
    return {"tag": [{"system": TAG_SYSTEM, "code": f"{cid}:{key}"}]}


def _base(cid: int, key: str, rtype: str, patient_id: str, encounter_id: str | None) -> dict:
    """AllergyIntolerance usa `patient` e (em R4) não tem encontro; MedicationStatement liga o encontro em `context`."""
    res: dict = {"resourceType": rtype, "meta": _meta(cid, key)}
    res["patient" if rtype == "AllergyIntolerance" else "subject"] = {"reference": f"Patient/{patient_id}"}
    if encounter_id and rtype in ("Condition", "Observation"):
        res["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    return res


def _allergy(a, cid: int, key: str, patient_id: str, encounter_id: str | None, when: datetime, segs) -> dict:
    code: dict = {"text": a.alergeno}
    if a.chave.startswith("@") and a.chave[1:] in reference.class_labels():  # classe terapêutica da base do HousIA
        code["coding"] = [{"system": codes.DRUG_CLASS, "code": a.chave[1:], "display": a.alergeno}]
    elif a.chave and not a.chave.startswith(("@", "alimento:", "veneno:", "antisseptico:")):
        code["coding"] = [{"system": codes.INGREDIENT, "code": a.chave, "display": a.alergeno}]
    res = _base(cid, key, "AllergyIntolerance", patient_id, encounter_id)
    res |= {
        "clinicalStatus": {
            "coding": [
                {"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical", "code": "active"}
            ]
        },
        "verificationStatus": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification",
                    "code": "unconfirmed",
                    "display": "Unconfirmed",
                }
            ]
        },
        "type": "allergy",
        "code": code,
        "recordedDate": when.isoformat(),
        "asserter": {"reference": f"Patient/{patient_id}"},
        "note": [
            {"text": f"Relatada pelo paciente na consulta com IA, ainda não confirmada. {_evidence(segs, a.refs)}"}
        ],
    }
    crit = {"alta": "high", "baixa": "low"}.get(a.criticidade)
    if crit:
        res["criticality"] = crit
    if a.reacao:
        res["reaction"] = [{"manifestation": [{"text": a.reacao}]}]
    return res


def _medication(m, cid: int, key: str, patient_id: str, encounter_id: str | None, when: datetime, segs) -> dict:
    res = _base(cid, key, "MedicationStatement", patient_id, encounter_id)
    res |= {
        "status": "active",
        "medicationCodeableConcept": {"text": m.medicamento},
        "dateAsserted": when.isoformat(),
        "informationSource": {"reference": f"Patient/{patient_id}"},
        "note": [{"text": f"Referido pelo paciente na consulta; não consta na prescrição. {_evidence(segs, m.refs)}"}],
    }
    if encounter_id:
        res["context"] = {"reference": f"Encounter/{encounter_id}"}
    if m.posologia:
        res["dosage"] = [{"text": m.posologia}]
    return res


def _condition(c, status: str, cid: int, key: str, patient_id: str, encounter_id: str | None, when, segs) -> dict:
    res = _base(cid, key, "Condition", patient_id, encounter_id)
    res |= {
        "clinicalStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]
        },
        "verificationStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": status}]
        },
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/condition-category",
                        "code": "encounter-diagnosis",
                    }
                ]
            }
        ],
        "code": {
            "coding": [{"system": codes.ICD10, "code": c["codigo"], "display": c["descricao"]}],
            "text": c["descricao"],
        },
        "recordedDate": when.isoformat(),
    }
    ev = _evidence(segs, c.get("refs", []))
    if ev:
        res["note"] = [{"text": ev}]
    return res


def _observation(key: str, cid: int, patient_id: str, encounter_id: str | None, when, ref: str, segs, **v) -> dict:
    res = _base(cid, key, "Observation", patient_id, encounter_id)
    res |= {
        "status": "preliminary",
        "category": [OBS_CATEGORY_VITALS],
        "effectiveDateTime": when.isoformat(),
        "note": [{"text": f"Valor referido pelo paciente, não aferido. {_evidence(segs, [ref])}"}],
    }
    res.update(v)
    return res


def _spoken_measures(segments: list[Segment]) -> list[tuple[str, str, float | tuple[int, int]]]:
    """(chave, ref, valor): PA e peso ditos pelo paciente. A fala do médico não vale; negação e pergunta não contam."""
    found: list[tuple[str, str, float | tuple[int, int]]] = []
    seen: set[str] = set()
    for s in segments:
        if s.falante not in ("paciente", "acompanhante") or s.marco or s.expurgada:
            continue
        text = _norm(s.texto)
        if "?" in s.texto:
            continue
        ref = f"Fala/{s.n}"
        if "pa" not in seen and PA_CTX.search(text):
            m = PA_RE.search(text)
            if m and 60 <= int(m[1]) <= 260 and 30 <= int(m[2]) <= 160 and int(m[1]) > int(m[2]):
                found.append(("pa", ref, (int(m[1]), int(m[2]))))
                seen.add("pa")
        if "peso" not in seen and PESO_CTX.search(text):
            m = PESO_RE.search(text)
            if m and 2 <= float(m[1].replace(",", ".")) <= 300:
                found.append(("peso", ref, float(m[1].replace(",", "."))))
                seen.add("peso")
    return found


def build(
    *,
    cid: int,
    patient_id: str,
    encounter_id: str | None,
    when: datetime,
    insights: Insights | None,
    note: dict | None,
    segments: list[Segment],
    accepted: set[str] | None = None,
) -> list[dict]:
    """Propostas da consulta: {id, tipo, rotulo, detalhe, refs, aceita, recurso}. `aceita` vem das decisões salvas."""
    out: list[dict] = []
    accepted = accepted or set()

    def add(tipo: str, key: str, rotulo: str, detalhe: str, refs: list[str], recurso: dict) -> None:
        pid = f"{tipo}:{key}"
        if any(p["id"] == pid for p in out):
            return
        recurso["meta"] = _meta(cid, pid)
        out.append(
            {
                "id": pid,
                "tipo": tipo,
                "tipo_rotulo": TIPO_ROTULO[tipo],
                "rotulo": rotulo,
                "detalhe": detalhe,
                "refs": refs,
                "aceita": pid in accepted,
                "recurso": recurso,
            }
        )

    if insights:
        for a in insights.alergias:
            if a.fonte != "relatado":
                continue
            key = _slug(a.chave or a.alergeno)
            add(
                "alergia",
                key,
                a.alergeno,
                (f"Reação: {a.reacao}. " if a.reacao else "") + "Entra como não confirmada, com a fala como evidência.",
                a.refs,
                _allergy(a, cid, key, patient_id, encounter_id, when, segments),
            )
        for m in insights.medicamentos:
            if m.status != "relatado":
                continue
            key = _slug(m.medicamento)
            add(
                "medicamento",
                key,
                m.medicamento,
                (m.posologia + ". " if m.posologia else "") + "Fonte da informação: o paciente.",
                m.refs,
                _medication(m, cid, key, patient_id, encounter_id, when, segments),
            )
    hyp = {h.cid: h for h in (insights.hipoteses if insights else [])}
    for c in (note or {}).get("cids") or []:
        key = _slug(c["codigo"])
        if c.get("tipo") == "hipotese":
            h = hyp.get(c["codigo"])
            status = "provisional" if h is None or h.confianca == "alta" else "differential"
            detalhe = "Hipótese diagnóstica: entra como " + (
                "provisória." if status == "provisional" else "diferencial."
            )
        else:
            status, detalhe = "confirmed", "CID confirmado na revisão da nota."
        add(
            "condicao",
            key,
            f"{c['codigo']} {c['descricao']}",
            detalhe,
            c.get("refs", []),
            _condition(c, status, cid, key, patient_id, encounter_id, when, segments),
        )
    for kind, ref, value in _spoken_measures(segments):
        if kind == "pa":
            sys_, dia = value  # type: ignore[misc]
            comp = [
                {
                    "code": {"coding": [{"system": codes.LOINC, "code": code, "display": label}]},
                    "valueQuantity": {
                        "value": v,
                        "unit": "mmHg",
                        "system": "http://unitsofmeasure.org",
                        "code": "mm[Hg]",
                    },
                }
                for code, label, v in (("8480-6", "PA sistólica", sys_), ("8462-4", "PA diastólica", dia))
            ]
            obs = _observation(
                "pa",
                cid,
                patient_id,
                encounter_id,
                when,
                ref,
                segments,
                code={"coding": [{"system": codes.LOINC, "code": codes.BP_PANEL[0], "display": codes.BP_PANEL[1]}]},
                component=comp,
            )
            add("observacao", "pa", "Pressão arterial", f"{sys_}x{dia} mmHg (referida).", [ref], obs)
        else:
            m = codes.VITALS["peso"]
            obs = _observation(
                "peso",
                cid,
                patient_id,
                encounter_id,
                when,
                ref,
                segments,
                code={"coding": [{"system": m.system, "code": m.code, "display": m.label}]},
                valueQuantity={"value": value, "unit": "kg", "system": "http://unitsofmeasure.org", "code": "kg"},
            )
            add("observacao", "peso", "Peso", f"{value:g} kg (referido).".replace(".", ","), [ref], obs)
    return out


def public(props: list[dict]) -> list[dict]:
    return [{k: v for k, v in p.items() if k != "recurso"} for p in props]
