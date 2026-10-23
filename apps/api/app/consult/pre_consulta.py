"""Cartão "Antes de entrar" (#44): o que o profissional precisa saber antes de abrir a consulta.

Montado por regras sobre o prontuário (FHIR) e sobre os registros do próprio HousIA (última nota, avaliação de alta),
com fonte em cada linha: referência FHIR e/ou a nota ou avaliação de origem. A IA só acrescenta, se pedido, um resumo
de até 3 frases, e cada frase precisa citar uma referência que já está no cartão. As pendências somem do cartão
(`tratada`) quando a conversa cita o assunto. Dados do paciente nunca vão ao LLM sem pseudonimização.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.consult import sinais
from app.consult.chart import ChartView, fmt_date, fmt_num
from app.consult.schemas import Segment, fala_ref
from app.consult.text import norm
from app.fhir_client import codes
from app.timeline.record import parse_dt

AVISO = "Montado por regras sobre o prontuário e os registros do HousIA; confira antes de decidir."
MAX_DAYS_DISCHARGE = 180  # a alta só entra se for recente


class PreLine(BaseModel):
    texto: str
    refs: list[str] = []  # recursos FHIR de origem
    fonte: str  # de onde veio ("Prontuário (Observation)", "Nota da consulta de 12/07 (HousIA #4)")
    tipo: str = ""
    tratada: bool = False  # pendência citada na conversa desta consulta
    tratada_refs: list[str] = []  # Fala/N que a trataram


class PreResumo(BaseModel):
    texto: str
    refs: list[str]


class PreConsulta(BaseModel):
    patient_id: str
    encounter_id: str | None = None
    ultima_consulta: str | None = None  # data da última consulta ambulatorial finalizada
    motivo: list[PreLine] = []
    mudou: list[PreLine] = []
    pendencias: list[PreLine] = []
    ultima_alta: list[PreLine] = []
    resumo_ia: list[PreResumo] = []
    modo_resumo: Literal["regras", "ia"] | None = None
    aviso: str = AVISO


@dataclass
class Sources:
    """O que vem do banco do HousIA (a camada de serviço lê e entrega pronto; este módulo não acessa o banco)."""

    last_note: dict | None = None  # {"id", "data", "pendencias": [{"texto","refs"}]} da última nota finalizada
    tasks: list[dict] = field(default_factory=list)  # tarefas de jornada abertas: {"texto","refs","prazo"}
    discharge: dict | None = (
        None  # {"id","data","lace","banda","risco","plano":{texto,refs},"alarmes":[...],"achados":[...]}
    )


def _visit(chart: ChartView, encounter_id: str | None) -> tuple[dict | None, dict | None]:
    """(consulta de hoje, última consulta ambulatorial finalizada antes dela)."""
    encs = [e for e in chart.r.encounters.values() if e.get("class", {}).get("code") == "AMB"]
    today = None
    if encounter_id:
        today = next((e for e in encs if e["id"] == encounter_id), None)
    else:
        open_ = [e for e in encs if e.get("status") != "finished"]
        today = max(open_, key=lambda e: parse_dt(e.get("period", {}).get("start")) or chart.now, default=None)
    limit = parse_dt((today or {}).get("period", {}).get("start")) or chart.now
    done = [
        e
        for e in encs
        if e.get("status") == "finished"
        and e is not today
        and (parse_dt(e.get("period", {}).get("start")) or limit) < limit
    ]
    last = max(done, key=lambda e: parse_dt(e.get("period", {}).get("start")), default=None)
    return today, last


def _motivo(today: dict | None) -> list[PreLine]:
    if not today:
        return []
    reason = ((today.get("reasonCode") or [{}])[0]).get("text", "")
    if not reason:
        return []
    return [
        PreLine(
            texto=f"Motivo agendado: {reason}.",
            refs=[f"Encounter/{today['id']}"],
            fonte="Agenda (Encounter.reasonCode)",
            tipo="motivo",
        )
    ]


def _labs(chart: ChartView, end: datetime) -> list[PreLine]:
    changed: list[PreLine] = []
    stable: list[PreLine] = []
    for key, items in chart.labs.items():
        new = [e for e in items if e.timestamp > end]
        if not new:
            continue
        cur = new[-1]
        m = codes.LABS[key]
        unit = f" {m.unit}" if m.unit else ""
        value = fmt_num(float(cur.value["valor"]), m.decimals)
        before = [e for e in items if e.timestamp < cur.timestamp]
        base = dict(fonte="Prontuário (Observation); tendência calculada pelo HousIA", tipo="exame")
        if not before:
            changed.append(
                PreLine(
                    texto=f"{m.label}: novo exame, {value}{unit} ({fmt_date(cur.timestamp)}).",
                    refs=[cur.source_ref],
                    **base,
                )
            )
            continue
        prev = before[-1]
        a, b = float(prev.value["valor"]), float(cur.value["valor"])
        refs = [prev.source_ref, cur.source_ref]
        when = f"{fmt_date(prev.timestamp)} → {fmt_date(cur.timestamp)}"
        if abs(b - a) < max(0.05 * abs(a), 10**-m.decimals):
            stable.append(PreLine(texto=f"{m.label} estável em {value}{unit} ({when}).", refs=refs, **base))
        else:
            verb = "subiu" if b > a else "caiu"
            changed.append(
                PreLine(
                    texto=f"{m.label} {verb} de {fmt_num(a, m.decimals)} para {value}{unit} ({when}).",
                    refs=refs,
                    **base,
                )
            )
    return changed + stable


def _meds(chart: ChartView, start: datetime) -> list[PreLine]:
    out: list[PreLine] = []
    for m in chart.r.medications:
        if m.status == "draft":
            continue
        if m.start and m.start >= start and m.status == "active":
            out.append(
                PreLine(
                    texto=f"{m.name} iniciado em {fmt_date(m.start)}" + (f" ({m.text})" if m.text else "") + ".",
                    refs=[m.ref],
                    fonte="Prontuário (MedicationRequest)",
                    tipo="medicamento",
                )
            )
        elif m.status == "stopped" and m.end and m.end >= start:
            why = f": {m.reason}" if m.reason else ""
            out.append(
                PreLine(
                    texto=f"{m.name} suspenso em {fmt_date(m.end)}{why}.",
                    refs=[m.ref],
                    fonte="Prontuário (MedicationRequest)",
                    tipo="medicamento",
                )
            )
    return out


def _stays(chart: ChartView, start: datetime, last_id: str | None) -> list[PreLine]:
    out: list[PreLine] = []
    for e in sorted(chart.r.encounters.values(), key=lambda x: x.get("period", {}).get("start", "")):
        cls = e.get("class", {}).get("code")
        began = parse_dt(e.get("period", {}).get("start"))
        if e["id"] == last_id or not began or began < start or e.get("partOf"):
            continue
        reason = ((e.get("reasonCode") or [{}])[0]).get("text", "motivo não registrado")
        ended = parse_dt(e.get("period", {}).get("end"))
        if cls == "EMER":
            out.append(
                PreLine(
                    texto=f"Ida ao pronto-socorro em {fmt_date(began)}: {reason}.",
                    refs=[f"Encounter/{e['id']}"],
                    fonte="Prontuário (Encounter)",
                    tipo="pronto_socorro",
                )
            )
        elif cls == "IMP":
            span = f"{fmt_date(began)} a {fmt_date(ended)}" if ended else f"desde {fmt_date(began)}"
            days = f" ({max(1, (ended - began).days)} dia(s))" if ended else " (em curso)"
            out.append(
                PreLine(
                    texto=f"Internação: {reason}, {span}{days}.",
                    refs=[f"Encounter/{e['id']}"],
                    fonte="Prontuário (Encounter)",
                    tipo="internacao",
                )
            )
    return out


def _pending(chart: ChartView, src: Sources) -> list[PreLine]:
    out: list[PreLine] = []
    for e in chart.r.pending_exams:
        origin = "proposto" if e.proposto else "pedido"
        out.append(
            PreLine(
                texto=f"Exame {origin} sem resultado: {e.texto}"
                + (f" (em {fmt_date(e.pedido_em)})" if e.pedido_em else "")
                + ".",
                refs=[e.ref],
                fonte="Prontuário (ServiceRequest sem resultado)",
                tipo="pedido",
            )
        )
    note = src.last_note
    for p in (note or {}).get("pendencias", []):
        out.append(
            PreLine(
                texto=f"Pendência da última nota: {p['texto']}",
                refs=[r for r in p.get("refs", []) if not r.startswith("Fala/")],
                fonte=f"Nota da consulta de {note['data']} (HousIA #{note['id']})",
                tipo="nota",
            )
        )
    in_note = [p["texto"] for p in (note or {}).get("pendencias", [])]
    for t in src.tasks:
        if any(x.startswith(t["texto"]) for x in in_note):
            continue  # a tarefa do protocolo atrasado (#47) é a mesma pendência que a nota já listou
        out.append(
            PreLine(
                texto=f"Tarefa de acompanhamento aberta: {t['texto']}"
                + (f" (prazo {t['prazo']})" if t.get("prazo") else ""),
                refs=[r for r in t.get("refs", []) if not r.startswith("Fala/")],  # Fala/N é de outra consulta
                fonte="Jornada do paciente (HousIA)",
                tipo="tarefa",
            )
        )
    return out


def _discharge(src: Sources) -> list[PreLine]:
    d = src.discharge
    if not d:
        return []
    fonte = f"Avaliação de alta de {d['data']} (HousIA #{d['id']})"
    out = [
        PreLine(
            texto=f"Alta de {d['data']}: risco de reinternação {d['risco']} (LACE {d['lace']}, {d['banda']}).",
            refs=d.get("lace_refs", []),
            fonte=fonte,
            tipo="alta",
        )
    ]
    if d.get("plano"):
        out.append(
            PreLine(
                texto=f"Plano de alta: {d['plano']['texto']}", refs=d["plano"].get("refs", []), fonte=fonte, tipo="alta"
            )
        )
    for a in d.get("achados", []):
        out.append(
            PreLine(
                texto=f"Reconciliação ({a['severidade']}): {a['titulo']}.",
                refs=a.get("refs", []),
                fonte="Reconciliação medicamentosa (HousIA)",
                tipo="alta",
            )
        )
    for s in d.get("alarmes", []):
        out.append(
            PreLine(texto=f"Sinal de alarme orientado: {s['texto']}", refs=s.get("refs", []), fonte=fonte, tipo="alta")
        )
    return out


NEGATION = re.compile(r"\b(nao|nunca|nem|ainda nao|esqueci|esqueceu|sem|faltou|perdi)\b")


def _mentions(segments: list[Segment]) -> list[tuple[int, set[str]]]:
    """(n da fala, radicais de uma oração) de cada oração que pode tratar uma pendência. A oração do paciente ou do
    acompanhante que nega ("não fiz a retinografia") confirma que a pendência continua: não a trata."""
    out: list[tuple[int, set[str]]] = []
    for s in segments:
        if s.origem == "marco":
            continue
        for sentence in sinais.sentences(s.texto):
            if s.falante in sinais.PATIENT_SIDE and NEGATION.search(norm(sentence)):
                continue
            out.append((s.n, {sinais.stem(w) for w in sinais.terms(sentence)}))
    return out


def mark_treated(lines: list[PreLine], segments: list[Segment]) -> None:
    """A pendência some do cartão quando a conversa cita o assunto: pelo menos 2 termos de conteúdo em comum (1, se
    o assunto só tem 1) na mesma oração. Guarda as `Fala/N` que a trataram."""
    spoken = _mentions(segments)
    for line in lines:
        subject = line.texto.split(":", 1)[-1] if ":" in line.texto else line.texto
        stems = {sinais.stem(w) for w in sinais.terms(subject)}
        need = min(2, len(stems))
        if not need:
            continue
        hits = list(dict.fromkeys(n for n, words in spoken if len(stems & words) >= need))
        if hits:
            line.tratada, line.tratada_refs = True, [fala_ref(n) for n in hits[:3]]


def build(
    chart: ChartView,
    patient_id: str,
    src: Sources,
    segments: list[Segment] | None = None,
    encounter_id: str | None = None,
) -> PreConsulta:
    today, last = _visit(chart, encounter_id)
    out = PreConsulta(patient_id=patient_id, encounter_id=(today or {}).get("id"), motivo=_motivo(today))
    if last:
        began = parse_dt(last["period"]["start"])
        ended = parse_dt(last["period"].get("end")) or began
        out.ultima_consulta = fmt_date(began)
        out.mudou = [*_labs(chart, ended), *_meds(chart, began), *_stays(chart, began, last["id"])]  # type: ignore[arg-type]
    out.pendencias = _pending(chart, src)
    out.ultima_alta = _discharge(src)
    if segments:
        mark_treated(out.pendencias, segments)
    return out


def all_refs(pre: PreConsulta) -> set[str]:
    return {r for part in (pre.motivo, pre.mudou, pre.pendencias, pre.ultima_alta) for line in part for r in line.refs}


def rules_summary(pre: PreConsulta) -> list[PreResumo]:
    """Resumo de até 3 frases sem IA: as linhas mais importantes, cada uma com a sua fonte."""
    pick: list[PreLine] = []
    for part in (
        pre.motivo,
        [x for x in pre.mudou if x.tipo in ("internacao", "pronto_socorro")],
        pre.mudou,
        pre.pendencias,
    ):
        for line in part:
            if line.refs and line not in pick and not line.tratada:
                pick.append(line)
                break
    return [PreResumo(texto=x.texto, refs=x.refs) for x in pick[:3]]
