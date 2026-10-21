"""Lembretes ao vivo e alertas de sinais vitais críticos (camada determinística).

Lembrete = assunto que o profissional ainda não abordou: alergia sem registro, exame de protocolo vencido, sinal vital
não aferido na triagem, consulta pós-alta sem reconciliação. Cada lembrete some quando o assunto aparece na conversa.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from app.consult import knowledge
from app.consult.chart import fmt_num, tz
from app.consult.schemas import Highlight, Reminder
from app.consult.text import contains
from app.timeline.record import parse_dt

if TYPE_CHECKING:
    from app.consult.engine import Engine

TRIAGE_HOURS = 12  # sinais vitais deste atendimento
POST_DISCHARGE_DAYS = 30
ALLERGY_WORDS = ("alergi", "alergic")
RECONCILIATION_WORDS = ("reconcilia", "lista de remedios", "lista de medicamentos", "quais remedios", "remedios que")
# sinal vital → (rótulo, palavras que mostram que o assunto foi tratado na conversa)
VITALS = {
    "pa": ("pressão arterial", ("pressao",)),
    "fc": ("frequência cardíaca", ("batimento", "pulso", "frequencia cardiaca")),
    "spo2": ("saturação de oxigênio", ("saturacao", "oximetro", "oximetria", "spo2", "oxigenio")),
    "temp": ("temperatura", ("temperatura", "termometro", "febre")),
}
GLUCOSE_WORDS = ("glicemia", "glicose", "dextro", "acucar")


def _said(eng: Engine, words: tuple[str, ...]) -> bool:
    return any(contains(eng.norm[s.n], words) for s in eng.segments)


def _latest_vital(eng: Engine, key: str, hours: int):
    event = eng.chart.latest_vital(key, hours)
    if event is None and key == "pa":  # PA pode vir como sistólica e diastólica separadas
        event = eng.chart.latest_vital("pas", hours)
    return event


def allergy(eng: Engine) -> list[Reminder]:
    ctx = eng.chart.ctx
    if ctx.alergias or ctx.nega_alergias or _said(eng, ALLERGY_WORDS):
        return []
    return [
        Reminder(
            chave="alergia",
            tipo="alergia",
            texto="Alergias sem registro no prontuário: pergunte se tem alergia a medicamento, alimento ou látex.",
            refs=[f"Patient/{eng.r.id}"],
            severidade="atencao",
            fonte="Sem AllergyIntolerance no prontuário",
        )
    ]


def protocols(eng: Engine) -> list[Reminder]:
    labs = knowledge.lab_catalog()
    out = []
    for p in eng.protocols:
        if p.status not in ("pendente", "sem_registro"):
            continue
        defn = next((d for d in knowledge.protocols() if d.id == p.id), None)
        kind, _, spec = (defn.source if defn else "").partition(":")
        if kind == "lab":
            words = tuple(w for lab in labs if spec in lab.result_keys for w in lab.words)
        elif kind == "texto":
            words = tuple(w for w in spec.split("|") if w)
        else:
            words = ("pressao",)
        if words and _said(eng, words):
            continue
        late = p.status == "pendente"
        last = f" (último: {p.ultimo})" if p.ultimo else ""
        out.append(
            Reminder(
                chave=f"protocolo:{p.protocolo}:{p.item}",
                tipo="protocolo",
                texto=f"{p.protocolo}: {p.item} {'vencido' if late else 'sem registro'}{last}. {p.orientacao}",
                refs=p.refs,
                severidade="atencao" if late else "info",
                fonte=p.fonte or "data/reference/protocols",
            )
        )
    out.sort(key=lambda r: r.severidade != "atencao")
    return out[:4]


def vitals(eng: Engine) -> list[Reminder]:
    recent = [e for e in eng.chart.events if e.kind == "vital" and eng.chart.now - e.timestamp <= _hours(TRIAGE_HOURS)]
    missing, refs = [], [e.source_ref for e in recent][:3]
    wanted = dict(VITALS)
    if eng.chart.has_condition("E1") is not None:
        wanted["glicemia"] = ("glicemia capilar", GLUCOSE_WORDS)
    for key, (label, words) in wanted.items():
        if _latest_vital(eng, key, TRIAGE_HOURS) is None and not _said(eng, words):
            missing.append(label)
    if not missing:
        return []
    text = (
        "Nenhum sinal vital registrado hoje: aferir " + ", ".join(missing) + "."
        if not recent
        else "Não aferidos na triagem: " + ", ".join(missing) + "."
    )
    return [
        Reminder(
            chave="sinais_vitais",
            tipo="sinais_vitais",
            texto=text,
            refs=refs or [f"Patient/{eng.r.id}"],
            severidade="info",
            fonte="Triagem do atendimento (últimas 12 h)",
        )
    ]


def _hours(n: int):
    from datetime import timedelta

    return timedelta(hours=n)


def post_discharge(eng: Engine) -> list[Reminder]:
    hosp = eng.r.hospitalization
    end = eng.r.admission_end if hosp and hosp.get("status") == "finished" else None
    if end is None or not (0 <= (eng.chart.now - end).days <= POST_DISCHARGE_DAYS):
        return []
    start = eng.r.admission_start
    runs = [parse_dt(r) if isinstance(r, str) else r for r in eng.extra.get("medrec_runs", [])]
    if any(r is not None and start is not None and r >= start for r in runs) or _said(eng, RECONCILIATION_WORDS):
        return []
    days = (eng.chart.now - end).days
    return [
        Reminder(
            chave="pos_alta",
            tipo="pos_alta",
            texto=f"Consulta pós-alta (alta há {days} dia{'s' if days != 1 else ''}) sem reconciliação medicamentosa: "
            "confira com o paciente o que ele toma agora.",
            refs=[f"Encounter/{hosp['id']}"],
            severidade="atencao",
            fonte="Nenhuma reconciliação executada desde a internação",
        )
    ]


def build(eng: Engine) -> list[Reminder]:
    return [*allergy(eng), *post_discharge(eng), *protocols(eng), *vitals(eng)]


# ---------- sinais vitais críticos ----------
def _at(dt: datetime) -> str:
    return dt.astimezone(tz()).strftime("%H:%M")


def critical_vitals(eng: Engine) -> list[Highlight]:
    """Sinal vital crítico da triagem vira alerta com o Observation: PA ≥ 180/110, SpO2 < 92, FC > 120 ou < 50,
    glicemia < 70 ou > 300."""
    out = []

    def alert(text: str, event) -> None:
        out.append(Highlight(texto=text, severidade="critico", refs=[event.source_ref], categoria="sinal_vital"))

    pa = _latest_vital(eng, "pa", TRIAGE_HOURS)
    if pa is not None and "sistolica" in pa.value:
        sys_, dia = pa.value["sistolica"], pa.value["diastolica"]
        if sys_ >= 180 or dia >= 110:
            alert(
                f"Pressão arterial {fmt_num(sys_, 0)}/{fmt_num(dia, 0)} mmHg na triagem ({_at(pa.timestamp)}), "
                "no limite crítico (180/110): repita a medida e avalie agora.",
                pa,
            )
    spo2 = eng.chart.latest_vital("spo2", TRIAGE_HOURS)
    if spo2 is not None and spo2.value["valor"] < 92:
        alert(
            f"SpO2 {fmt_num(spo2.value['valor'], 0)}% na triagem ({_at(spo2.timestamp)}), abaixo de 92%: avalie agora.",
            spo2,
        )
    fc = eng.chart.latest_vital("fc", TRIAGE_HOURS)
    if fc is not None and (fc.value["valor"] > 120 or fc.value["valor"] < 50):
        way = "acima de 120" if fc.value["valor"] > 120 else "abaixo de 50"
        alert(
            f"Frequência cardíaca {fmt_num(fc.value['valor'], 0)} bpm na triagem ({_at(fc.timestamp)}), {way}: "
            "avalie agora.",
            fc,
        )
    glic = eng.chart.latest_vital("glicemia", TRIAGE_HOURS)
    if glic is not None and (glic.value["valor"] < 70 or glic.value["valor"] > 300):
        way = "abaixo de 70" if glic.value["valor"] < 70 else "acima de 300"
        alert(
            f"Glicemia capilar {fmt_num(glic.value['valor'], 0)} mg/dL na triagem ({_at(glic.timestamp)}), {way}: "
            "trate e reavalie agora.",
            glic,
        )
    return out
