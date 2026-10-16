"""Waze da jornada: regras determinísticas sobre o FHIR e o banco do HousIA.

Cada regra devolve itens com fonte (recursos FHIR e registros do HousIA citados), severidade e prazo. Nada aqui chama
LLM: a lista precisa ser a mesma com ou sem IA, e cada número é auditável. Parâmetros e textos de fonte ficam em
data/reference/journey_rules.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.consult import knowledge
from app.consult.chart import ChartView
from app.db.models import Consultation, DischargeAssessment, JourneyContact, JourneyTask
from app.discharge.scoring import SocialChecklist, final_risk, lace, prefill, social_score
from app.fhir_client import codes
from app.journey import reference
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from app.timeline.record import PatientRecord, coding, parse_dt

RULES = (
    "alta_sem_contato_72h",
    "retorno_nao_agendado",
    "falta_ao_retorno",
    "protocolo_aps_vencido",
    "nao_adesao_consulta",
    "troca_sem_reconciliacao",
)
# Appointment que conta como retorno marcado (cancelado e falta não contam)
SCHEDULED = {"proposed", "pending", "booked", "arrived", "checked-in", "fulfilled", "waitlist"}
ACTIVE_ENCOUNTER = {"in-progress", "arrived", "triaged", "onleave"}
SEVERITY_RANK = {"critico": 0, "atencao": 1, "info": 2}


def local_day(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(get_settings().timezone)).strftime("%d/%m/%Y")


def ago(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours < 48:
        return f"há {max(hours, 0)} h"
    return f"há {delta.days} dias"


@dataclass
class Facts:
    """Fatos do banco do HousIA usados pelas regras (carregados uma vez por lista)."""

    assessments: dict[str, list[DischargeAssessment]] = field(default_factory=dict)  # mais nova primeiro
    contacts: dict[str, list[JourneyContact]] = field(default_factory=dict)
    medrec: dict[str, list[datetime]] = field(default_factory=dict)
    consultations: dict[str, list[Consultation]] = field(default_factory=dict)
    tasks: dict[tuple[str, str], JourneyTask] = field(default_factory=dict)  # só as abertas


@dataclass(frozen=True)
class Discharge:
    at: datetime
    encounter_id: str | None
    origem: str  # encontro (Encounter IMP encerrado) | confirmacao (alta confirmada no HousIA)
    risk: str  # baixo | moderado | alto
    risk_text: str
    risk_origin: str  # avaliacao | estimado
    risk_refs: list[str]
    assessment: DischargeAssessment | None
    refs: list[str]


@dataclass
class Item:
    rule: str
    patient_id: str
    severity: str
    situacao: str  # vencido | a_vencer
    titulo: str
    detalhe: str
    refs: list[str]
    desde: datetime
    prazo: datetime | None = None
    contexto: dict = field(default_factory=dict)


@dataclass
class Ctx:
    record: PatientRecord
    facts: Facts
    now: datetime
    admitted: bool
    discharge: Discharge | None

    @property
    def pid(self) -> str:
        return self.record.id

    @property
    def contacts(self) -> list[JourneyContact]:
        return self.facts.contacts.get(self.pid, [])

    def reached_after(self, moment: datetime) -> list[JourneyContact]:
        return [c for c in self.contacts if c.reached and c.created_at >= moment]

    @property
    def appointments(self) -> list[dict]:
        """Agendamentos ambulatoriais (a sala cirúrgica, ligada a um pedido, não é retorno)."""
        out = []
        for a in self.record.resources.get("Appointment", []):
            if any((b.get("reference") or "").startswith("ServiceRequest/") for b in a.get("basedOn", [])):
                continue
            out.append(a)
        return out


# ---------- alta e risco ----------
def _estimate(record: PatientRecord, now: datetime) -> tuple[str, str, list[str]]:
    """Risco sem avaliação de alta: LACE do prontuário + o pouco de social que o FHIR sabe (moradores, cuidador,
    Morse). O que o prontuário não sabe fica neutro: não inventa fator de risco."""
    record.now = now
    score, components = lace(record)
    pre = prefill(record)["valores"]
    checklist = SocialChecklist(
        mora_sozinho=pre.get("mora_sozinho", False),
        tem_cuidador=pre.get("tem_cuidador", False),
        escadas_em_casa=False,
        mobilidade_reduzida=pre.get("mobilidade_reduzida", False),
        acesso_medicamentos=True,
        precisa_dispositivos=False,
        consulta_retorno_agendada=True,
        teach_back_realizado=True,
    )
    social, items = social_score(checklist)
    risk, why = final_risk(score, social)
    refs = [r for c in components.values() for r in c.get("refs", [])]
    text = f"Risco {risk} estimado do prontuário (LACE {score}, social {social}): {why} Sem avaliação de alta."
    return risk, text, list(dict.fromkeys(refs))


def discharge_of(record: PatientRecord, facts: Facts, now: datetime) -> tuple[bool, Discharge | None]:
    """(internado agora, última alta). A alta é o fim do Encounter IMP ou, se o prontuário ainda o mostra aberto,
    a confirmação da alta no HousIA (o HousIA não escreve no encontro)."""
    hosp = record.hospitalization
    if hosp is None:
        return False, None
    start = record.admission_start
    own = facts.assessments.get(record.id, [])
    mine = [a for a in own if a.encounter_id == hosp["id"]] or [
        a for a in own if start and a.created_at >= start - timedelta(days=1)
    ]
    assessment = mine[0] if mine else None
    ended = record.admission_end if hosp.get("status") == "finished" else None
    confirmed = next((a.confirmed_at for a in mine if a.confirmed_at), None)
    if ended is not None:
        at, origin = ended, "encontro"
    elif confirmed is not None and confirmed <= now:
        at, origin = confirmed, "confirmacao"
    else:
        return hosp.get("status") == "in-progress", None
    if at > now:
        return False, None
    refs = [f"Encounter/{hosp['id']}"] + ([f"DischargeAssessment/{assessment.id}"] if assessment else [])
    if assessment is not None:
        extra = assessment.suggestions_json or {}
        comps = (assessment.lace_components_json or {}).get("componentes") or {}
        risk_refs = [r for c in comps.values() for r in c.get("refs", [])]
        risk_refs += [r for i in extra.get("social_itens", []) for r in i.get("refs", [])]
        text = (
            f"Risco {assessment.final_risk} (LACE {assessment.lace_score}, social {assessment.social_score}) pela "
            f"avaliação de alta #{assessment.id}."
        )
        return False, Discharge(
            at,
            hosp["id"],
            origin,
            assessment.final_risk,
            text,
            "avaliacao",
            list(dict.fromkeys(risk_refs)),
            assessment,
            refs,
        )
    risk, text, risk_refs = _estimate(record, now)
    return False, Discharge(at, hosp["id"], origin, risk, text, "estimado", risk_refs, None, refs)


def _caregiver_text(record: PatientRecord) -> str:
    for rp in record.related_persons:
        name = (rp.get("name") or [{}])[0].get("text")
        phone = next((t["value"] for t in rp.get("telecom", []) if t.get("value")), None)
        if name:
            return f" Familiar de contato: {name}" + (f", {phone}" if phone else "") + "."
    return ""


# ---------- regras ----------
def alta_sem_contato(ctx: Ctx) -> list[Item]:
    p, d = reference.params(), ctx.discharge
    if d is None or d.risk != "alto":
        return []
    elapsed = ctx.now - d.at
    if elapsed > timedelta(days=p["janela_pos_alta_dias"]) or ctx.reached_after(d.at):
        return []
    tries = [c for c in ctx.contacts if not c.reached and c.created_at >= d.at]
    deadline = d.at + timedelta(hours=p["alta_sem_contato_horas"])
    overdue = ctx.now >= deadline
    situacao = "vencido" if overdue else "a_vencer"
    if overdue:
        title = f"Alta de alto risco {ago(elapsed)} sem contato pós-alta"
    else:
        left = int((deadline - ctx.now).total_seconds() // 3600)
        title = f"Alta de alto risco: contato pós-alta vence em {left} h"
    detail = f"Alta em {local_day(d.at)}. {d.risk_text}"
    if tries:
        detail += f" {len(tries)} tentativa(s) sem sucesso registrada(s)."
        if len(tries) >= int(p["max_tentativas"]):
            detail += " Considere visita domiciliar ou contato pelo familiar."
    detail += _caregiver_text(ctx.record)
    severity = "critico" if overdue else "atencao"
    if tries and len(tries) >= int(p["max_tentativas"]):
        severity = "critico"
    return [
        Item(
            "alta_sem_contato_72h",
            ctx.pid,
            severity,
            situacao,
            title,
            detail,
            list(dict.fromkeys(d.refs + d.risk_refs)),
            d.at,
            deadline,
            {"risco": d.risk, "risco_origem": d.risk_origin, "tentativas": len(tries), "alta_em": d.at.isoformat()},
        )
    ]


def retorno_nao_agendado(ctx: Ctx) -> list[Item]:
    p, d = reference.params(), ctx.discharge
    if d is None:
        return []
    elapsed = ctx.now - d.at
    if elapsed < timedelta(hours=p["retorno_carencia_horas"]) or elapsed > timedelta(days=p["janela_pos_alta_dias"]):
        return []
    after = d.at - timedelta(days=1)
    for a in ctx.appointments:  # qualquer agendamento (inclusive falta) depois da alta: a falta tem a sua regra
        if (when := parse_dt(a.get("start"))) and when >= after and a.get("status") != "cancelled":
            return []
    for e in ctx.record.encounters.values():  # retorno que aconteceu sem agenda no FHIR
        if e.get("class", {}).get("code") == "AMB" and (when := parse_dt(e.get("period", {}).get("start"))):
            if when >= d.at:
                return []
    late = elapsed >= timedelta(days=p["retorno_atrasado_dias"])
    severity = "critico" if d.risk == "alto" and late else "atencao"
    detail = f"Alta em {local_day(d.at)} ({ago(elapsed)}) e nenhum agendamento depois dela. {d.risk_text}"
    if d.assessment and (d.assessment.social_checklist_json or {}).get("consulta_retorno_agendada"):
        detail += " O checklist da alta marcou retorno agendado, mas não há Appointment: conferir o agendamento."
    return [
        Item(
            "retorno_nao_agendado",
            ctx.pid,
            severity,
            "vencido",
            f"Retorno pós-alta não agendado ({ago(elapsed)} da alta)",
            detail,
            list(dict.fromkeys(d.refs)),
            d.at,
            d.at + timedelta(hours=p["retorno_carencia_horas"]),
            {"risco": d.risk, "risco_origem": d.risk_origin, "alta_em": d.at.isoformat()},
        )
    ]


def falta_ao_retorno(ctx: Ctx) -> list[Item]:
    p = reference.params()
    window = ctx.now - timedelta(days=p["falta_janela_dias"])
    misses = [
        a
        for a in ctx.appointments
        if a.get("status") == "noshow" and (when := parse_dt(a.get("start"))) and window <= when <= ctx.now
    ]
    if not misses:
        return []
    last = max(misses, key=lambda a: a["start"])
    when = parse_dt(last["start"])
    assert when is not None
    for a in ctx.appointments:  # remarcou: o próximo agendamento resolve
        s = parse_dt(a.get("start"))
        if s and s > when and a.get("status") in SCHEDULED:
            return []
    if ctx.reached_after(when):  # a equipe falou com o paciente depois da falta
        return []
    d = ctx.discharge
    severity = "critico" if d is not None and d.risk == "alto" else "atencao"
    what = last.get("description") or "consulta agendada"
    detail = f"Faltou em {local_day(when)} ({what}) e não há novo agendamento nem contato registrado depois."
    if len(misses) > 1:
        detail += f" {len(misses)} faltas nos últimos {p['falta_janela_dias']} dias."
    if d is not None:
        detail += f" {d.risk_text}"
    refs = [f"Appointment/{a['id']}" for a in misses] + (d.refs if d else [])
    return [
        Item(
            "falta_ao_retorno",
            ctx.pid,
            severity,
            "vencido",
            f"Faltou à consulta de {local_day(when)}",
            detail,
            list(dict.fromkeys(refs)),
            when,
            when + timedelta(days=1),
            {"faltas": len(misses), "risco": d.risk if d else None},
        )
    ]


def protocolo_aps_vencido(ctx: Ctx) -> list[Item]:
    r = ctx.record
    seen_now = [
        e
        for e in r.encounters.values()
        if e.get("class", {}).get("code") == "AMB" and e.get("status") in ACTIVE_ENCOUNTER
    ]
    if seen_now:
        return []  # está sendo atendido agora
    for a in ctx.appointments:  # consulta marcada: já há plano
        s = parse_dt(a.get("start"))
        if s and s > ctx.now and a.get("status") in ("booked", "pending", "proposed"):
            return []
    wanted = {p.condition for p in knowledge.protocols()}
    prefixes = {
        (coding(c.get("code"), codes.ICD10) or {}).get("code", "").replace(".", "") for c in r.active_conditions
    }
    if not any(code.startswith(w) for code in prefixes for w in wanted):
        return []
    chart = ChartView(r, build_timeline(r), build_context(r), ctx.now)
    interval = {(p.protocol, p.item): p.interval_days for p in knowledge.protocols()}
    late = [i for i in chart.protocols() if i.status == "pendente"]
    if not late:
        return []
    lines = [
        f"{i.protocolo} — {i.item}: último {i.ultimo or 'sem data'} (intervalo {interval[(i.protocolo, i.item)]} dias)"
        for i in late
    ]
    refs = list(dict.fromkeys(ref for i in late for ref in i.refs))
    stamps = [e.timestamp for e in chart.events if f"{e.source_ref}" in refs and e.kind in ("lab", "note", "procedure")]
    return [
        Item(
            "protocolo_aps_vencido",
            ctx.pid,
            "atencao",
            "vencido",
            f"{len(late)} item(ns) de protocolo de acompanhamento vencido(s)",
            "; ".join(lines[:4]) + (f"; e mais {len(lines) - 4}" if len(lines) > 4 else "") + ".",
            refs,
            min(stamps) if stamps else ctx.now,
            None,
            {"itens": [i.item for i in late]},
        )
    ]


def nao_adesao_consulta(ctx: Ctx) -> list[Item]:
    p = reference.params()
    window = ctx.now - timedelta(days=p["adesao_janela_dias"])
    out = []
    for c in ctx.facts.consultations.get(ctx.pid, []):
        when = c.finished_at or c.created_at
        if when < window or ctx.reached_after(when):
            continue
        signs = [s for s in (c.insights_json or {}).get("sugestoes_medicamentos", []) if s.get("tipo") == "adesao"]
        if not signs:
            continue
        refs = [f"Consultation/{c.id}"] + [r for s in signs for r in s.get("refs", [])]
        out.append(
            Item(
                "nao_adesao_consulta",
                ctx.pid,
                "atencao",
                "vencido",
                f"Sinal de não adesão na consulta de {local_day(when)}",
                " ".join(s["texto"] for s in signs),
                list(dict.fromkeys(refs)),
                when,
                None,
                {"consulta": c.id},
            )
        )
    return out[:1]  # a consulta mais recente já resume o problema


def troca_sem_reconciliacao(ctx: Ctx) -> list[Item]:
    p = reference.params()
    window = ctx.now - timedelta(days=p["troca_janela_dias"])
    by_ref = {m.ref: m for m in ctx.record.medications}
    runs = ctx.facts.medrec.get(ctx.pid, [])
    swaps = []
    for m in ctx.record.medications:
        if not m.prior_ref or m.start is None or not (window <= m.start <= ctx.now):
            continue
        if any(run >= m.start for run in runs):
            continue
        swaps.append((m.start, m, by_ref.get(m.prior_ref)))
    if not swaps:
        return []
    swaps.sort(key=lambda s: s[0])
    lines = [
        f"{(old.name if old else 'prescrição anterior')} trocado por {new.name} em {local_day(when)}"
        for when, new, old in swaps
    ]
    refs = [r for _w, new, old in swaps for r in (new.ref, old.ref if old else None) if r]
    return [
        Item(
            "troca_sem_reconciliacao",
            ctx.pid,
            "atencao",
            "vencido",
            "Medicamento trocado sem reconciliação medicamentosa",
            "; ".join(lines) + ". Nenhuma reconciliação executada depois da troca.",
            list(dict.fromkeys(refs)),
            swaps[0][0],
            None,
            {"trocas": len(swaps)},
        )
    ]


EVALUATORS = {
    "alta_sem_contato_72h": alta_sem_contato,
    "retorno_nao_agendado": retorno_nao_agendado,
    "falta_ao_retorno": falta_ao_retorno,
    "protocolo_aps_vencido": protocolo_aps_vencido,
    "nao_adesao_consulta": nao_adesao_consulta,
    "troca_sem_reconciliacao": troca_sem_reconciliacao,
}


def evaluate(record: PatientRecord, facts: Facts, now: datetime) -> list[Item]:
    """Itens da lista para um paciente. Quem está internado (sem alta confirmada) não entra: a jornada começa na
    alta e vai até o retorno."""
    admitted, discharge = discharge_of(record, facts, now)
    if admitted:
        return []
    ctx = Ctx(record, facts, now, admitted, discharge)
    items: list[Item] = []
    for rule in RULES:
        items += EVALUATORS[rule](ctx)
    return items
