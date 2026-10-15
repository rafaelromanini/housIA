"""Contexto fixo do paciente (diagnósticos ativos, alergias, medicamentos ativos), sempre com a fonte."""

from pydantic import BaseModel

from app.fhir_client import codes
from app.timeline.record import PatientRecord, coding


class ContextItem(BaseModel):
    source_ref: str
    summary: str
    detail: dict | None = None


class PatientContext(BaseModel):
    diagnosticos: list[ContextItem]
    alergias: list[ContextItem]
    medicamentos_ativos: list[ContextItem]
    familiares: list[ContextItem]
    nega_alergias: bool

    def refs(self) -> set[str]:
        return {
            i.source_ref
            for group in (self.diagnosticos, self.alergias, self.medicamentos_ativos, self.familiares)
            for i in group
        }


def build_context(record: PatientRecord) -> PatientContext:
    dx = []
    for c in record.active_conditions:
        icd = coding(c.get("code"), codes.ICD10)
        label = c.get("code", {}).get("text") or (icd or {}).get("display", "")
        dx.append(
            ContextItem(
                source_ref=f"Condition/{c['id']}",
                summary=f"{label} ({icd['code']})" if icd else label,
                detail={"cid10": icd["code"] if icd else None},
            )
        )
    allergies = []
    for a in record.real_allergies:
        reaction = "; ".join(m.get("text", "") for r in a.get("reaction", []) for m in r.get("manifestation", []))
        crit = {"high": "alta", "low": "baixa"}.get(a.get("criticality", ""), "indeterminada")
        sub = coding(a.get("code"), codes.INGREDIENT) or coding(a.get("code"), codes.DRUG_CLASS) or {}
        allergies.append(
            ContextItem(
                source_ref=f"AllergyIntolerance/{a['id']}",
                summary=f"{a.get('code', {}).get('text', 'Alergia')} — {reaction} (criticidade {crit})",
                detail={"sistema": sub.get("system"), "codigo": sub.get("code"), "criticidade": a.get("criticality")},
            )
        )
    meds = [
        ContextItem(
            source_ref=m.ref,
            summary=m.text,
            detail={
                "principio_ativo": m.ingredient,
                "classe": m.drug_class,
                "horarios": list(m.times),
                "se_necessario": m.prn,
                "inicio": m.start.isoformat() if m.start else None,
            },
        )
        for m in record.active_meds
    ]
    family = []
    for rp in record.related_persons:
        rel = (rp.get("relationship") or [{}])[0]
        is_caregiver = bool(coding(rel, codes.TAGS))
        family.append(
            ContextItem(
                source_ref=f"RelatedPerson/{rp['id']}",
                summary=f"{rel.get('text', 'familiar')}" + (" — cuidador(a) principal" if is_caregiver else ""),
                detail={"nome": (rp.get("name") or [{}])[0].get("text"), "cuidador": is_caregiver},
            )
        )
    nka = any(
        (coding(a.get("code"), codes.SNOMED) or {}).get("code") == codes.NO_KNOWN_ALLERGY for a in record.allergies
    )
    return PatientContext(
        diagnosticos=dx,
        alergias=allergies,
        medicamentos_ativos=meds,
        familiares=family,
        nega_alergias=nka and not allergies,
    )
