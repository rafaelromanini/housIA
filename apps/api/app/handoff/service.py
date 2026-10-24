"""Pilar 1 — passagem de plantão por perfil.

Pipeline: timeline da janela + contexto fixo → fatos duros determinísticos → pseudonimização → LLM com saída
estruturada → validação de refs (itens sem fonte válida são descartados e auditados) → cache por input_hash.
"""

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.alerts.service import communication_facts, consult_alert_facts, tarefa_facts, upsert_alert
from app.audit.service import log_action
from app.config import get_settings
from app.db.models import HandoffReport, User
from app.fhir_client.client import FhirSource
from app.handoff.ao_vivo import MAX_TEXTO, MAX_TEXTO_REGRAS, atribuir_falas, contagem, garantir_criticos
from app.handoff.ao_vivo import roteiro as roteiro_sbar
from app.handoff.check import HandoffCheckOutput, mock_check
from app.handoff.facts import compute_facts
from app.handoff.mock import mock_handoff
from app.handoff.schemas import Fact, HandoffCheckRequest, HandoffOutput, HandoffRequest
from app.handoff.validation import validate_refs
from app.llm.client import LLMError, generate_structured, load_prompt
from app.llm.pseudonymize import Pseudonymizer
from app.patients.service import load_record
from app.timeline.context import build_context
from app.timeline.events import build_timeline, window
from app.timeline.record import PatientRecord

PROMPT = "handoff"

AUDIENCE_INSTRUCTIONS = {
    "medico": (
        "Médico(a) que assume o plantão. Priorize instabilidades (hemodinâmicas, respiratórias, metabólicas), "
        "mudanças terapêuticas e seus riscos, exames alterados ou pendentes, hipóteses em aberto e o que pode "
        "piorar nas próximas horas. Não liste cuidados de rotina de enfermagem."
    ),
    "enfermagem": (
        "Enfermeiro(a) que assume o plantão. Priorize cuidados práticos e horários de medicação (com dupla checagem "
        "de medicamentos potencialmente perigosos), acessos venosos e dispositivos (sondas, drenos), risco de queda e "
        "de lesão por pressão, diurese e balanço, dieta, mobilidade e sinais vitais a vigiar. Não discuta hipóteses "
        "diagnósticas."
    ),
}


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def _local(dt: datetime) -> str:
    return dt.astimezone(_tz()).strftime("%d/%m %H:%M")


def _input_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _situation_refs(record: PatientRecord, ctx) -> list[str]:
    refs = []
    if record.current_sector:
        refs.append(f"Encounter/{record.current_sector.encounter_id}")
    refs += [d.source_ref for d in ctx.diagnosticos[:3]]
    return refs


def report_out(report: HandoffReport, cached: bool) -> dict:
    return {
        "id": report.id,
        "patient_id": report.patient_id,
        "audience": report.audience,
        "window_hours": report.window_hours,
        "to_location": report.to_location,
        "created_at": report.created_at.isoformat(),
        "model": report.model,
        "prompt_version": report.prompt_version,
        "cached": cached,
        "content": report.content_json,
    }


def generate_handoff(db: Session, fhir: FhirSource, user: User, patient_id: str, req: HandoffRequest) -> dict:
    settings = get_settings()
    prompt = load_prompt(PROMPT)
    now = clock.now()
    record = load_record(fhir, patient_id)
    events = build_timeline(record)
    in_window = window(events, now, req.window_hours)
    ctx = build_context(record)
    facts: list[Fact] = (
        compute_facts(record, events, now, req.window_hours)
        + consult_alert_facts(db, patient_id)
        + communication_facts(db, patient_id)
        + tarefa_facts(db, patient_id)
    )
    sector = record.current_sector

    model_id = "mock-deterministico" if settings.llm_mode == "mock" else settings.llm_model
    input_hash = _input_hash(
        {
            "audience": req.audience,
            "window": req.window_hours,
            "to": req.to_location,
            "prompt": prompt.version,
            "model": model_id,
            "hour": now.strftime("%Y%m%d%H"),
            "events": [(e.id, e.timestamp.isoformat()) for e in in_window],
            "facts": [f.texto for f in facts],
        }
    )

    if not req.force:
        cached = db.scalar(
            select(HandoffReport)
            .where(HandoffReport.patient_id == patient_id, HandoffReport.input_hash == input_hash)
            .order_by(HandoffReport.id.desc())
        )
        if cached:
            log_action(
                db,
                user,
                "handoff.cache_hit",
                "handoff_report",
                cached.id,
                {"audience": req.audience, "input_hash": input_hash},
            )
            return report_out(cached, cached=True)

    # ---------- entrada do LLM (pseudonimizada) ----------
    pseudo = Pseudonymizer(**record.identifiers_for_pseudonymization())
    patient_payload = {
        "idade": record.age,
        "sexo": record.sex,
        "setor": sector.ward if sector else None,
        "leito": sector.bed if sector else None,
        "dias_internacao": record.length_of_stay_days,
        "tipo_internacao": "urgência" if record.admission_urgent else "eletiva",
        "motivo": record.admission_reason,
    }
    context_payload = {
        "diagnosticos": [{"ref": d.source_ref, "texto": d.summary} for d in ctx.diagnosticos],
        "alergias": [{"ref": a.source_ref, "texto": a.summary} for a in ctx.alergias]
        or ("nega alergias conhecidas" if ctx.nega_alergias else "sem registro"),
        "medicamentos_ativos": [{"ref": m.source_ref, "texto": m.summary} for m in ctx.medicamentos_ativos],
        "setores_da_internacao": [
            {
                "ref": f"Encounter/{s.encounter_id}",
                "setor": s.ward,
                "leito": s.bed,
                "inicio": _local(s.start) if s.start else None,
                "fim": _local(s.end) if s.end else "atual",
            }
            for s in record.sectors
        ],
    }
    facts_payload = [
        {
            "tipo": f.tipo,
            "texto": f.texto,
            "severidade": f.severidade,
            "refs": f.refs,
            "horario": f.horario,
            "relevante_para": f.publico,
        }
        for f in facts
    ]
    events_payload = [
        {
            "ref": e.source_ref,
            "horario": _local(e.timestamp),
            "tipo": e.kind,
            "setor": e.location,
            "resumo": e.summary,
            **({"texto": e.text} if e.text else {}),
        }
        for e in in_window
    ]
    variables = pseudo.apply(
        {
            "paciente": patient_payload,
            "contexto": context_payload,
            "fatos": facts_payload,
            "eventos": events_payload,
        }
    )
    variables |= {
        "publico": req.audience,
        "instrucoes_publico": AUDIENCE_INSTRUCTIONS[req.audience],
        "agora": now.astimezone(_tz()).strftime("%d/%m/%Y %H:%M"),
        "janela_h": str(req.window_hours),
        "destino": req.to_location or "troca de turno no mesmo setor",
    }

    mock_patient = {
        **patient_payload,
        "diagnosticos": [d.summary for d in ctx.diagnosticos],
        "alergias": "; ".join(a.summary for a in ctx.alergias) or ("nega" if ctx.nega_alergias else "sem registro"),
    }
    situation_refs = _situation_refs(record, ctx)
    result = generate_structured(
        PROMPT,
        variables,
        HandoffOutput,
        mock=lambda _v: mock_handoff(req.audience, facts, mock_patient, situation_refs),
    )

    # ---------- validação obrigatória das citações ----------
    valid_refs = {e.source_ref for e in in_window} | ctx.refs() | {r for f in facts for r in f.refs}
    valid_refs |= {f"Encounter/{s.encounter_id}" for s in record.sectors}
    output, dropped = validate_refs(result.output, valid_refs)

    content = output.model_dump()
    claimed: set[int] = set()
    for risk in content["riscos_proximas_horas"]:
        if risk["severidade"] in ("critico", "atencao") and not risk["texto"].startswith(
            ("Alerta da consulta", "Preocupação sem resposta", "Tarefa aberta")
        ):
            alert = upsert_alert(  # o alerta da consulta já existe no módulo de alertas (source='consulta')
                db,
                patient_id=patient_id,
                source="handoff",
                severity=risk["severidade"],
                title=risk["texto"],
                detail=f"Passagem de plantão ({req.audience})",
                refs=risk["refs"],
                claimed=claimed,
            )
            risk["alert_id"] = alert.id
    content |= {
        "fatos": [f.model_dump() for f in facts],
        "descartados": dropped,
        "meta": {
            "gerado_em": now.isoformat(),
            "janela_h": req.window_hours,
            "publico": req.audience,
            "destino": req.to_location,
            "modo": result.mode,
            "reparado": result.repaired,
            "eventos_na_janela": len(in_window),
            "pseudonimizacoes": pseudo.replacements,
        },
    }
    report = HandoffReport(
        patient_id=patient_id,
        encounter_id=record.hospitalization["id"] if record.hospitalization else None,
        audience=req.audience,
        from_location=sector.ward if sector else None,
        to_location=req.to_location,
        window_hours=req.window_hours,
        content_json=content,
        model=result.model,
        prompt_version=result.prompt_version,
        input_hash=input_hash,
        created_by=user.id,
        created_at=now,
    )
    db.add(report)
    db.flush()
    log_action(
        db,
        user,
        "handoff.gerado",
        "handoff_report",
        report.id,
        {
            "patient_id": patient_id,
            "audience": req.audience,
            "model": result.model,
            "prompt_version": result.prompt_version,
            "itens_descartados": len(dropped),
            "forcado": req.force,
        },
        commit=False,
    )
    if dropped:
        log_action(
            db, user, "handoff.refs_invalidas", "handoff_report", report.id, {"descartados": dropped}, commit=False
        )
    db.commit()
    return report_out(report, cached=False)


class Conferidor:
    """Contexto da conferência verbal de um paciente: prontuário, fatos relevantes e fontes. Construído uma vez por
    passagem (a conexão de áudio reaproveita) e usado a cada fala; as regras são determinísticas e rápidas."""

    def __init__(self, db: Session, fhir: FhirSource, patient_id: str, audience: str, window_hours: int) -> None:
        self.now = clock.now()
        self.audience, self.window_hours = audience, window_hours
        self.record = load_record(fhir, patient_id)
        self.events = build_timeline(self.record)
        self.in_window = window(self.events, self.now, window_hours)
        self.facts = compute_facts(self.record, self.events, self.now, window_hours) + tarefa_facts(db, patient_id)
        self.relevant = [
            f for f in self.facts if audience in f.publico and f.severidade in ("critico", "atencao") and f.refs
        ]
        self.events_by_ref = {e.source_ref: e for e in self.events}
        self.valid = {e.source_ref for e in self.events} | {r for f in self.facts for r in f.refs}

    def regras(self, texto: str) -> dict:
        return mock_check(texto, self.facts, self.audience, self.record, self.events_by_ref)

    def modelo(self, texto: str):
        pseudo = Pseudonymizer(**self.record.identifiers_for_pseudonymization())
        variables = pseudo.apply(
            {
                "relato": texto,
                "fatos": [
                    {"texto": f.texto, "tipo": f.tipo, "severidade": f.severidade, "refs": f.refs}
                    for f in self.relevant
                ],
                "eventos": [
                    {"ref": e.source_ref, "horario": _local(e.timestamp), "tipo": e.kind, "resumo": e.summary}
                    for e in self.in_window
                    if e.kind != "med_admin"
                ],
            }
        ) | {"publico": self.audience}
        return generate_structured("handoff_check", variables, HandoffCheckOutput, mock=lambda _v: self.regras(texto))

    def validar(self, itens: list[dict]) -> tuple[list[dict], list[dict]]:
        ok, descartados = [], []
        for it in itens:
            bad = [r for r in it["refs"] if r not in self.valid]
            (descartados if not it["refs"] or bad else ok).append(it)
        return ok, descartados


def _ordenar(items: list[dict]) -> list[dict]:
    order = {"critico": 0, "atencao": 1, "info": 2}
    return sorted(items, key=lambda i: (order[i["severidade"]], i["status"]))


def conferir_ao_vivo(c: Conferidor, segs: list[tuple[int, str]], final: bool) -> dict:
    """Resultado da conferência para o relato acumulado (`segs` = (n, texto) de cada fala). Parcial: só as regras
    (milissegundos, o texto não sai do servidor). Final: o mesmo caminho da conferência digitada (IA em modo real), com
    as regras de segurança por cima. Cada item leva a fonte do prontuário e a fala (`Fala/N`) que o mencionou ou
    contradisse."""
    texto = "\n".join(t for _, t in segs)
    regras = c.regras(texto)["itens"]
    forcados = 0
    descartados: list[dict] = []
    resumo, modo, modelo_id, versao, reparado = None, "regras", "regras-deterministicas", None, False
    itens = regras
    if final:
        result = c.modelo(texto)
        itens, descartados = c.validar([it.model_dump() for it in result.output.itens])
        itens, forcados = garantir_criticos(itens, regras)
        resumo, modo, modelo_id, versao, reparado = (
            result.output.resumo,
            result.mode,
            result.model,
            result.prompt_version,
            result.repaired,
        )
    itens = atribuir_falas(_ordenar(itens), c.relevant, c.record, c.events_by_ref, segs)
    counts = contagem(itens)
    total = len(itens)
    return {
        "resumo": resumo
        or (
            f"{counts['mencionado']} de {total} fatos relevantes citados até agora; {counts['omitido']} ainda não "
            f"citado(s) e {counts['divergente']} divergência(s)."
        ),
        "itens": itens,
        "contagem": counts,
        "cobertura": round(counts["mencionado"] / total, 2) if total else None,
        "descartados": len(descartados),
        "roteiro": roteiro_sbar(texto, itens, c.relevant, final),
        "ao_vivo": True,
        "final": final,
        "segmentos": len(segs),
        "meta": {
            "modo": modo,
            "modelo": modelo_id,
            "prompt_version": versao,
            "reparado": reparado,
            "regra_prevaleceu": forcados,
            "gerado_em": c.now.isoformat(),
            "janela_h": c.window_hours,
            "publico": c.audience,
        },
    }


def verbal_check_ao_vivo(db: Session, fhir: FhirSource, user: User, patient_id: str, body) -> dict:
    """POST /patients/{id}/handoff/verbal-check/segments: reroda a conferência com as falas acumuladas. Só a chamada
    `final` audita (contagens, nunca o texto); o relato nunca fica gravado."""
    if not body.ciente:
        raise HTTPException(422, "Confirme que a fala será transcrita e conferida (nada fica gravado).")
    segs = sorted(((s.n, s.texto.strip()) for s in body.segmentos if s.texto.strip()), key=lambda x: x[0])
    if not segs:
        raise HTTPException(422, "Nenhuma fala com texto.")
    total = sum(len(t) for _, t in segs)
    if total > MAX_TEXTO_REGRAS:
        raise HTTPException(413, f"O relato passou de {MAX_TEXTO_REGRAS} caracteres: encerre a passagem.")
    conferidor = Conferidor(db, fhir, patient_id, body.audience, body.window_hours)
    so_regras = body.final and total > MAX_TEXTO  # passagem longa: fecha e audita, mas sem mandar o texto todo à IA
    try:
        if so_regras:
            raise LLMError("relato maior que o limite da IA")
        out = conferir_ao_vivo(conferidor, segs, body.final)
    except LLMError:
        if not body.final:
            raise
        out = conferir_ao_vivo(conferidor, segs, False) | {"final": True}
        out["meta"]["aviso"] = (
            f"O relato passou de {MAX_TEXTO} caracteres: este resultado vem só das regras determinísticas."
            if so_regras
            else "A IA não respondeu: este resultado vem só das regras determinísticas."
        )
    if body.final:
        auditar_conferencia(db, user, patient_id, body.audience, out, segmentos=segs, origens=body.segmentos)
    return out


def auditar_conferencia(db, user, patient_id, audience, out, *, segmentos=None, origens=None) -> None:
    """Auditoria da conferência: contagens, nunca o relato (texto clínico livre)."""
    counts = out["contagem"]
    payload = {
        "patient_id": patient_id,
        "audience": audience,
        **counts,
        "omitidos_criticos": sum(i["status"] == "omitido" and i["severidade"] == "critico" for i in out["itens"]),
        "divergentes_criticos": sum(i["status"] == "divergente" and i["severidade"] == "critico" for i in out["itens"]),
        "modelo": out["meta"]["modelo"],
    }
    if segmentos is not None:
        payload |= {
            "ao_vivo": True,
            "ciente": True,  # quem falou confirmou que a fala é transcrita e conferida (sem consentimento da consulta)
            "segmentos": len(segmentos),
            "origens": sorted({o.origem for o in origens or []}),
            "regra_prevaleceu": out["meta"].get("regra_prevaleceu", 0),
        }
    log_action(db, user, "handoff.conferencia", "patient", patient_id, payload)


def check_verbal_handoff(db: Session, fhir: FhirSource, user: User, patient_id: str, req: HandoffCheckRequest) -> dict:
    """Confere o relato verbal (digitado ou colado) contra os fatos duros e a timeline da janela."""
    c = Conferidor(db, fhir, patient_id, req.audience, req.window_hours)
    result = c.modelo(req.texto)
    items, dropped = c.validar([it.model_dump() for it in result.output.itens])
    items, forced = garantir_criticos(items, c.regras(req.texto)["itens"])  # a IA não rebaixa a regra de segurança
    items = _ordenar(items)
    counts = contagem(items)
    out = {
        "resumo": result.output.resumo,
        "itens": items,
        "contagem": counts,
        "cobertura": round(counts["mencionado"] / len(items), 2) if items else None,
        "descartados": len(dropped),
        "meta": {
            "modo": result.mode,
            "modelo": result.model,
            "prompt_version": result.prompt_version,
            "gerado_em": c.now.isoformat(),
            "janela_h": req.window_hours,
            "publico": req.audience,
            "regra_prevaleceu": forced,
        },
    }
    auditar_conferencia(db, user, patient_id, req.audience, out)
    return out
