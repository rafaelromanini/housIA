"""Métricas da jornada, calculadas sobre os Encounter IMP do FHIR: reinternação em 30 dias e retornos perdidos.

Tudo é recomputável a partir do prontuário e cada coorte devolve os casos (com a referência do Encounter), para a
conta poder ser conferida. Definições (também devolvidas no campo `metodo`):
- alta: Encounter IMP de nível superior (sem partOf) com período encerrado dentro da janela pedida;
- seguimento completo: a alta tem pelo menos N dias (N = janela da métrica); só essas entram nas taxas;
- reinternação em N dias: outro Encounter IMP do mesmo paciente que começa depois da alta e até N dias dela;
- retorno realizado: Appointment cumprido (fulfilled/arrived) ou Encounter ambulatorial iniciado até N dias da alta;
- retorno perdido: alta com seguimento completo sem retorno realizado; falta: Appointment noshow no mesmo período.
O risco da coorte é o LACE da internação índice (o score social só existe quando há avaliação de alta, então fica fora).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from app import clock
from app.discharge.scoring import _l_points, charlson_index, lace_band
from app.fhir_client import codes
from app.fhir_client.client import FhirSource
from app.journey import reference
from app.timeline.record import PatientRecord, coding, parse_dt, ref_id

COORTES = ("risco", "mes", "cid")
DONE = {"fulfilled", "arrived", "checked-in"}
METODO = (
    "Alta = Encounter IMP encerrado. Reinternação = novo Encounter IMP do mesmo paciente em até {r} dias da alta. "
    "Retorno realizado = Appointment cumprido ou consulta ambulatorial em até {p} dias da alta; sem isso é retorno "
    "perdido. Só entram nas taxas as altas com seguimento completo (≥ {r} dias). Risco da coorte = LACE da internação "
    "índice (sem o score social)."
)


def _lace_of(record: PatientRecord, hosp: dict, start: datetime, end: datetime) -> int:
    urgent = (coding(hosp.get("priority"), "http://terminology.hl7.org/CodeSystem/v3-ActPriority") or {}).get(
        "code"
    ) == "EM"
    ci, _ = charlson_index(record)
    visits = [
        e
        for e in record.encounters.values()
        if e.get("class", {}).get("code") == "EMER"
        and (w := parse_dt(e.get("period", {}).get("start")))
        and start - timedelta(days=182) <= w < start
    ]
    return _l_points((end - start).days) + (3 if urgent else 0) + (5 if ci >= 4 else ci) + min(len(visits), 4)


def _principal_cid(record: PatientRecord) -> tuple[str, str]:
    for c in record.resources.get("Condition", []):
        cats = {cd.get("code") for cat in c.get("category", []) for cd in cat.get("coding", [])}
        icd = coding(c.get("code"), codes.ICD10)
        if "encounter-diagnosis" in cats and icd:
            return icd["code"][:3], c.get("code", {}).get("text") or icd["code"]
    return "—", "Sem diagnóstico principal"


def _key(coorte: str, risk: str, end: datetime, cid: tuple[str, str]) -> tuple[str, str]:
    if coorte == "risco":
        return risk, f"Risco LACE {risk}"
    if coorte == "mes":
        return end.strftime("%Y-%m"), end.strftime("%m/%Y")
    return cid[0], f"{cid[0]} — {cid[1]}"


def _row(cases: list[dict]) -> dict:
    complete = [c for c in cases if c["seguimento_completo"]]
    readmitted = [c for c in complete if c["reinternado"]]
    lost = [c for c in complete if not c["retorno_realizado"]]
    return {
        "altas": len(cases),
        "seguimento_completo": len(complete),
        "em_seguimento": len(cases) - len(complete),
        "reinternacoes": len(readmitted),
        "taxa_reinternacao": round(len(readmitted) / len(complete), 3) if complete else None,
        "reinternados_em_seguimento": sum(1 for c in cases if not c["seguimento_completo"] and c["reinternado"]),
        "retornos_perdidos": len(lost),
        "taxa_retorno_perdido": round(len(lost) / len(complete), 3) if complete else None,
        "faltas": sum(1 for c in complete if c["falta_ref"]),
    }


def compute(fhir: FhirSource, coorte: str = "risco", dias: int = 365) -> dict:
    p = reference.params()
    re_days, ret_days = int(p["reinternacao_dias"]), int(p["retorno_perdido_dias"])
    now = clock.now()
    since = now - timedelta(days=dias)
    imp: dict[str, list[dict]] = defaultdict(list)
    for e in fhir.encounters_by_class("IMP"):
        if not e.get("partOf") and (pid := ref_id(e.get("subject"))):
            imp[pid].append(e)
    appts: dict[str, list[dict]] = defaultdict(list)
    for a in fhir.appointments():
        if any((b.get("reference") or "").startswith("ServiceRequest/") for b in a.get("basedOn", [])):
            continue  # sala cirúrgica não é retorno
        for part in a.get("participant", []):
            ref = (part.get("actor") or {}).get("reference", "")
            if ref.startswith("Patient/"):
                appts[ref.split("/", 1)[1]].append(a)
    amb: dict[str, list[dict]] = defaultdict(list)
    for e in fhir.encounters_by_class("AMB"):
        if pid := ref_id(e.get("subject")):
            amb[pid].append(e)

    cases: list[dict] = []
    for pid, encounters in imp.items():
        encounters.sort(key=lambda e: e["period"]["start"])
        record: PatientRecord | None = None
        for n, enc in enumerate(encounters):
            period = enc.get("period", {})
            start, end = parse_dt(period.get("start")), parse_dt(period.get("end"))
            if enc.get("status") != "finished" or start is None or end is None or not (since <= end <= now):
                continue
            record = record or PatientRecord.load(fhir, pid)
            if record is None:
                continue
            score = _lace_of(record, enc, start, end)
            nxt, nxt_start = next(
                (
                    (o, s)
                    for o in encounters[n + 1 :]
                    if (s := parse_dt(o["period"]["start"])) and end < s <= end + timedelta(days=re_days)
                ),
                (None, None),
            )
            window_end = end + timedelta(days=ret_days)
            done = next(
                (
                    f"Appointment/{a['id']}"
                    for a in appts[pid]
                    if a.get("status") in DONE and (s := parse_dt(a.get("start"))) and end <= s <= window_end
                ),
                None,
            ) or next(
                (
                    f"Encounter/{e['id']}"
                    for e in amb[pid]
                    if (s := parse_dt(e.get("period", {}).get("start"))) and end <= s <= window_end
                ),
                None,
            )
            miss = next(
                (
                    f"Appointment/{a['id']}"
                    for a in appts[pid]
                    if a.get("status") == "noshow" and (s := parse_dt(a.get("start"))) and end <= s <= window_end
                ),
                None,
            )
            cid = _principal_cid(record)
            cases.append(
                {
                    "paciente_id": pid,
                    "paciente": record.name,
                    "encounter_ref": f"Encounter/{enc['id']}",
                    "alta_em": end.isoformat(),
                    "lace": score,
                    "risco": lace_band(score),
                    "cid": cid[0],
                    "mes": end.strftime("%Y-%m"),
                    "seguimento_completo": end + timedelta(days=re_days) <= now,
                    "reinternado": nxt is not None,
                    "reinternacao_ref": f"Encounter/{nxt['id']}" if nxt else None,
                    "reinternacao_em": nxt_start.isoformat() if nxt_start else None,
                    "dias_ate_reinternacao": (nxt_start - end).days if nxt_start else None,
                    "retorno_realizado": done is not None,
                    "retorno_ref": done,
                    "falta_ref": miss,
                    "_coorte": _key(coorte, lace_band(score), end, cid),
                }
            )
    groups: dict[str, list[dict]] = defaultdict(list)
    labels: dict[str, str] = {}
    for c in cases:
        key, label = c.pop("_coorte")
        c["coorte"] = key
        groups[key].append(c)
        labels[key] = label
    order = {"alto": 0, "moderado": 1, "baixo": 2}
    keys = sorted(groups, key=(lambda k: order.get(k, 9)) if coorte == "risco" else (lambda k: k))
    cases.sort(key=lambda c: c["alta_em"], reverse=True)
    return {
        "gerado_em": now.isoformat(),
        "parametros": {"coorte": coorte, "dias": dias, "reinternacao_dias": re_days, "retorno_dias": ret_days},
        "metodo": METODO.format(r=re_days, p=ret_days),
        "total": _row(cases),
        "coortes": [{"chave": k, "rotulo": labels[k]} | _row(groups[k]) for k in keys],
        "casos": cases,
    }
