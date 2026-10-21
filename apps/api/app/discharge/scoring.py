"""Pilar 2 — score de risco de reinternação: LACE (do FHIR) + score social (checklist).

Toda a composição é devolvida para exibição (sem caixa-preta).

LACE (van Walraven et al., CMAJ 2010):
  L  tempo de internação (dias): <1→0, 1→1, 2→2, 3→3, 4–6→4, 7–13→5, ≥14→7
  A  internação por urgência (acuity): 3 pontos
  C  índice de Charlson (simplificado por CID-10): 0→0, 1→1, 2→2, 3→3, ≥4→5
  E  idas ao pronto-socorro nos 6 meses anteriores: 0–3 → 0–3, ≥4→4
  Faixas: <5 baixo, 5–9 moderado, ≥10 alto.

Score social: soma ponderada do checklist (pesos em data/reference/social_weights.json).
HEURÍSTICA PROVISÓRIA, NÃO VALIDADA. Faixas no mesmo arquivo (padrão: ≥3 moderado, ≥6 alto).

Risco final (heurística provisória, documentada no ADR 0007):
  - ALTO     se social alto, ou LACE alto com social moderado/alto
  - MODERADO se LACE alto com social baixo (risco clínico mitigado por rede de apoio e plano de alta),
             ou LACE moderado, ou social moderado
  - BAIXO    se LACE baixo e social baixo
"""

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel

from app.fhir_client import codes
from app.reference import charlson, social_weights
from app.timeline.record import PatientRecord, coding, parse_dt

Risk = Literal["baixo", "moderado", "alto"]


class SocialChecklist(BaseModel):
    mora_sozinho: bool
    tem_cuidador: bool
    cuidador_capacitado: bool | None = None
    escadas_em_casa: bool
    mobilidade_reduzida: bool
    acesso_medicamentos: bool
    precisa_dispositivos: bool
    dispositivos: list[str] = []
    ja_tem_dispositivos: bool | None = None
    consulta_retorno_agendada: bool
    teach_back_realizado: bool
    observacoes: str | None = None


def _l_points(days: int) -> int:
    if days < 1:
        return 0
    if days <= 3:
        return days
    if days <= 6:
        return 4
    if days <= 13:
        return 5
    return 7


VERIFICATION_SYSTEM = "http://terminology.hl7.org/CodeSystem/condition-ver-status"
UNCONFIRMED = frozenset({"provisional", "differential", "refuted", "entered-in-error"})


def charlson_index(record: PatientRecord) -> tuple[int, list[dict]]:
    """Charlson simplificado: categoria por prefixo de CID-10; dentro do mesmo grupo vale o maior peso."""
    table = charlson()
    best: dict[str, dict] = {}
    for c in record.resources.get("Condition", []):
        icd = coding(c.get("code"), codes.ICD10)
        status = (coding(c.get("verificationStatus"), VERIFICATION_SYSTEM) or {}).get("code", "")
        if (
            not icd or status in UNCONFIRMED
        ):  # hipótese (provisória/diferencial), refutada ou lançada por engano não pontua
            continue
        code = icd["code"].replace(".", "").upper()
        for row in table:
            if code.startswith(row["prefixo_cid10"]):
                weight = int(row["peso"])
                group = row["grupo"]
                current = best.get(group)
                if current is None or weight > current["peso"]:
                    best[group] = {
                        "categoria": row["categoria"],
                        "peso": weight,
                        "cid10": icd["code"],
                        "refs": [f"Condition/{c['id']}"],
                    }
                break
    items = sorted(best.values(), key=lambda x: -x["peso"])
    return sum(i["peso"] for i in items), items


def lace(record: PatientRecord) -> tuple[int, dict]:
    days = record.length_of_stay_days
    hosp_ref = f"Encounter/{record.hospitalization['id']}" if record.hospitalization else None
    l_pts = _l_points(days)
    a_pts = 3 if record.admission_urgent else 0
    ci, categories = charlson_index(record)
    c_pts = 5 if ci >= 4 else ci
    start = record.admission_start or record.now
    window_start = start - timedelta(days=182)
    visits = [
        e
        for e in record.previous_emergency_visits
        if (when := parse_dt(e.get("period", {}).get("start"))) and when >= window_start
    ]
    e_pts = min(len(visits), 4)
    components = {
        "L": {
            "rotulo": "Tempo de internação",
            "valor": f"{days} dia(s)",
            "pontos": l_pts,
            "refs": [hosp_ref] if hosp_ref else [],
        },
        "A": {
            "rotulo": "Internação por urgência",
            "valor": "sim" if a_pts else "não (eletiva)",
            "pontos": a_pts,
            "refs": [hosp_ref] if hosp_ref else [],
        },
        "C": {
            "rotulo": "Comorbidades (Charlson simplificado)",
            "valor": f"índice {ci}",
            "pontos": c_pts,
            "categorias": categories,
            "refs": [r for cat in categories for r in cat["refs"]],
        },
        "E": {
            "rotulo": "Idas ao PS nos 6 meses anteriores",
            "valor": f"{len(visits)} visita(s)",
            "pontos": e_pts,
            "refs": [f"Encounter/{e['id']}" for e in visits],
        },
    }
    return l_pts + a_pts + c_pts + e_pts, components


def lace_band(score: int) -> Risk:
    return "alto" if score >= 10 else "moderado" if score >= 5 else "baixo"


def social_score(checklist: SocialChecklist) -> tuple[int, list[dict]]:
    cfg = social_weights()
    answers = checklist.model_dump()
    items = []
    for field, rule in cfg["itens"].items():
        condition = rule.get("so_se", {})
        if any(answers.get(k) != v for k, v in condition.items()):
            continue
        value = answers.get(field)
        if value is None:
            continue
        if value == rule["risco_quando"]:
            items.append(
                {"campo": field, "rotulo": rule["rotulo"], "peso": rule["peso"], "refs": [f"Checklist/{field}"]}
            )
    return sum(i["peso"] for i in items), items


def social_band(score: int) -> Risk:
    bands = social_weights()["faixas"]
    return "alto" if score >= bands["alto"] else "moderado" if score >= bands["moderado"] else "baixo"


def final_risk(lace_score: int, social: int) -> tuple[Risk, str]:
    lb, sb = lace_band(lace_score), social_band(social)
    if sb == "alto":
        return "alto", f"Score social alto ({social})."
    if lb == "alto" and sb == "moderado":
        return "alto", f"LACE alto ({lace_score}) com fatores sociais de risco ({social})."
    if lb == "alto":
        return "moderado", f"LACE alto ({lace_score}), mitigado por checklist social favorável ({social})."
    if lb == "moderado" or sb == "moderado":
        return "moderado", f"LACE {lb} ({lace_score}) e social {sb} ({social})."
    return "baixo", f"LACE baixo ({lace_score}) e social baixo ({social})."


def prefill(record: PatientRecord) -> dict:
    """Sugestões do prontuário para o checklist (o profissional confirma cada resposta)."""
    values: dict[str, bool] = {}
    sources: dict[str, dict] = {}
    household = record.household_size
    for o in record.resources.get("Observation", []):
        if (coding(o.get("code"), codes.LOINC) or {}).get("code") == codes.LIVES_ALONE_LOINC and household:
            values["mora_sozinho"] = household == 1
            sources["mora_sozinho"] = {
                "ref": f"Observation/{o['id']}",
                "explicacao": f"{household} morador(es) no domicílio registrado(s)",
            }
    caregiver = record.caregiver
    values["tem_cuidador"] = caregiver is not None
    sources["tem_cuidador"] = (
        {"ref": f"RelatedPerson/{caregiver['id']}", "explicacao": "Cuidador(a) principal registrado(a)"}
        if caregiver
        else {"ref": None, "explicacao": "Nenhum cuidador registrado no prontuário"}
    )
    morse = [
        o
        for o in record.resources.get("Observation", [])
        if (coding(o.get("code"), codes.LOINC) or {}).get("code") == codes.SCALES["morse"].code
    ]
    if morse:
        last = max(morse, key=lambda o: o.get("effectiveDateTime", ""))
        value = last.get("valueQuantity", {}).get("value", 0)
        values["mobilidade_reduzida"] = value >= 45
        sources["mobilidade_reduzida"] = {"ref": f"Observation/{last['id']}", "explicacao": f"Morse {value:g}"}
    return {"valores": values, "fontes": sources}
