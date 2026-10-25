import csv
import hashlib
import hmac
import io
import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.audit import chain, impact
from app.audit.service import log_action
from app.auth.deps import require_roles
from app.config import get_settings
from app.consult import knowledge
from app.db.models import (
    AIFeedback,
    Alert,
    AuditLog,
    DischargeAssessment,
    DischargeOverride,
    HandoffReport,
    ImpactBaseline,
    MedrecRun,
    User,
)
from app.db.session import get_db

router = APIRouter(prefix="/audit", tags=["auditoria"])
gestor = require_roles("gestor")


def _range(since: date | None, until: date | None) -> tuple[datetime, datetime]:
    """Datas do filtro são dias no fuso do hospital (America/Sao_Paulo por padrão)."""
    tz = ZoneInfo(get_settings().timezone)
    end = datetime.combine(until, time.max, tzinfo=tz) if until else clock.now() + timedelta(minutes=1)
    start = datetime.combine(since, time.min, tzinfo=tz) if since else end - timedelta(days=30)
    return start.astimezone(UTC), end.astimezone(UTC)


@router.get("")
def list_audit(
    action: str | None = Query(None, description="Prefixo da ação, ex.: discharge. ou handoff.gerado"),
    patient_id: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: int = Query(100, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
    _: User = Depends(gestor),
) -> dict:
    start, end = _range(since, until)
    q = (
        select(AuditLog, User)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(AuditLog.created_at >= start, AuditLog.created_at <= end)
        .order_by(AuditLog.id.desc())
    )
    if action:
        q = q.where(AuditLog.action.startswith(action))
    rows = db.execute(q).all()
    if patient_id:
        rows = [
            (a, u)
            for a, u in rows
            if (a.payload_json or {}).get("patient_id") == patient_id
            or (a.entity == "patient" and a.entity_id == patient_id)
        ]
    return {"total": len(rows), "items": [_entry(a, u) for a, u in rows[offset : offset + limit]]}


MAX_EXPORT = 50_000
CSV_COLUNAS = ["id", "created_at", "user_id", "perfil", "action", "entity", "entity_id", "payload", "prev_hash", "hash"]


def _celula(valor: object) -> str:
    """Texto de célula de planilha sem fórmula: o que começa com = + - @ ganha apóstrofo (CSV injection)."""
    texto = "" if valor is None else str(valor)
    return "'" + texto if texto[:1] in ("=", "+", "-", "@", "\t", "\r") else texto


@router.get("/verify")
def verify_chain(db: Session = Depends(get_db), user: User = Depends(gestor)) -> dict:
    """Confere a cadeia de hash do audit_log da organização e aponta a primeira linha quebrada (#53)."""
    out = chain.verificar(db, user.organization_id)
    log_action(
        db,
        user,
        "audit.cadeia_verificada",
        "audit_log",
        None,
        {"integra": out["integra"], "total": out["total"], "primeira_quebra": (out["primeira_quebra"] or {}).get("id")},
    )
    return out


@router.get("/export")
def export_audit(
    formato: str = Query("json", pattern="^(json|csv)$"),
    since: date | None = None,
    until: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(gestor),
) -> Response:
    """Exporta o audit_log da organização no período (JSON ou CSV), só para o gestor/DPO. O arquivo sai com o SHA-256 do
    conteúdo e uma assinatura HMAC (cabeçalhos `X-Conteudo-SHA256` e `X-Assinatura`; no JSON, no corpo também).
    A própria exportação vira um registro (`audit.exportado`). Sem nome de usuário: só o id e o perfil."""
    start, end = _range(since, until)
    rows = db.execute(
        select(AuditLog, User.role)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(AuditLog.created_at >= start, AuditLog.created_at <= end)
        .order_by(AuditLog.id)
        .limit(MAX_EXPORT + 1)
    ).all()
    if len(rows) > MAX_EXPORT:
        raise HTTPException(422, f"Período grande demais (mais de {MAX_EXPORT} registros): reduza o intervalo.")
    itens = [
        {
            "id": a.id,
            "created_at": a.created_at.isoformat(),
            "user_id": a.user_id,
            "perfil": perfil,
            "action": a.action,
            "entity": a.entity,
            "entity_id": a.entity_id,
            "payload": a.payload_json,
            "prev_hash": a.prev_hash,
            "hash": a.hash,
        }
        for a, perfil in rows
    ]
    periodo = {"inicio": start.isoformat(), "fim": end.isoformat()}
    if formato == "csv":
        out = io.StringIO()
        writer = csv.writer(out, delimiter=";")
        writer.writerow(CSV_COLUNAS)
        for i in itens:
            writer.writerow(
                [_celula(json.dumps(i[c], ensure_ascii=False) if c == "payload" else i[c]) for c in CSV_COLUNAS]
            )
        body = ("﻿" + out.getvalue()).encode("utf-8")
        media, ext = "text/csv; charset=utf-8", "csv"
    else:
        conteudo = json.dumps(itens, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        sha = hashlib.sha256(conteudo).hexdigest()
        cadeia = {k: v for k, v in chain.verificar(db, user.organization_id).items() if k != "limite"}
        # a assinatura cobre também organização, período, total e a cabeça da cadeia (não só as linhas)
        selado = {
            "organizacao": user.organization_id,
            "periodo": periodo,
            "total": len(itens),
            "conteudo_sha256": sha,
            "cabeca": cadeia["cabeca"],
        }
        doc = {
            **selado,
            "gerado_em": clock.now().isoformat(),
            "itens": itens,
            "assinatura_hmac": hmac.new(chain.chave(), chain.canonico_simples(selado), hashlib.sha256).hexdigest(),
            "como_conferir": (
                "SHA-256 de json.dumps(itens, sort_keys=True, separators=(',', ':'), ensure_ascii=False); a assinatura "
                "é o HMAC-SHA256 do mesmo json.dumps de {organizacao, periodo, total, conteudo_sha256, cabeca}."
            ),
            "cadeia": cadeia,
        }
        body = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
        media, ext = "application/json", "json"
    sha_arquivo = hashlib.sha256(body).hexdigest()
    log_action(
        db,
        user,
        "audit.exportado",
        "audit_log",
        None,
        {"formato": formato, "linhas": len(itens), "periodo": periodo, "arquivo_sha256": sha_arquivo},
    )
    return Response(
        body,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="auditoria-housia.{ext}"',
            "X-Conteudo-SHA256": sha_arquivo,
            "X-Assinatura": hmac.new(chain.chave(), sha_arquivo.encode(), hashlib.sha256).hexdigest(),
            "Cache-Control": "no-store",
        },
    )


def _entry(a: AuditLog, u: User | None) -> dict:
    return {
        "id": a.id,
        "created_at": a.created_at.isoformat(),
        "user": {"id": u.id, "name": u.name, "role": u.role} if u else None,
        "action": a.action,
        "entity": a.entity,
        "entity_id": a.entity_id,
        "payload": a.payload_json,
    }


@router.get("/metrics")
def metrics(
    since: date | None = None,
    until: date | None = None,
    uso_regular_min: int = Query(impact.USO_REGULAR_MIN, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(gestor),
) -> dict:
    start, end = _range(since, until)

    def count(model, *conds) -> int:
        return db.scalar(select(func.count()).select_from(model).where(*conds)) or 0

    alerts_in = (Alert.created_at >= start, Alert.created_at <= end)
    status_rows = db.execute(select(Alert.status, func.count()).where(*alerts_in).group_by(Alert.status)).all()
    source_rows = db.execute(
        select(Alert.source, func.count()).where(*alerts_in, Alert.severity == "critico").group_by(Alert.source)
    ).all()
    dropped = 0
    for (payload,) in db.execute(
        select(AuditLog.payload_json).where(
            AuditLog.action == "handoff.gerado", AuditLog.created_at >= start, AuditLog.created_at <= end
        )
    ):
        dropped += int((payload or {}).get("itens_descartados", 0))
    feedback: dict[str, dict[str, int]] = {}
    for kind, useful, n in db.execute(
        select(AIFeedback.kind, AIFeedback.useful, func.count())
        .where(AIFeedback.created_at >= start, AIFeedback.created_at <= end, AIFeedback.kind != "nps")
        .group_by(AIFeedback.kind, AIFeedback.useful)
    ):
        feedback.setdefault(kind, {"util": 0, "nao_util": 0})["util" if useful else "nao_util"] += n
    total_useful = sum(v["util"] for v in feedback.values())
    total_feedback = total_useful + sum(v["nao_util"] for v in feedback.values())
    statuses = {s: n for s, n in status_rows}
    acted = statuses.get("reconhecido", 0) + statuses.get("descartado", 0)
    return {
        "periodo": {"inicio": start.isoformat(), "fim": end.isoformat()},
        "feedback_ia": feedback,
        "taxa_utilidade_ia": round(total_useful / total_feedback, 3) if total_feedback else None,
        "taxa_aceitacao_alertas": round(statuses.get("reconhecido", 0) / acted, 3) if acted else None,
        "alertas_criticos": count(Alert, *alerts_in, Alert.severity == "critico"),
        "alertas_criticos_por_origem": {s: n for s, n in source_rows},
        "alertas_por_status": statuses,
        "overrides": count(
            DischargeOverride, DischargeOverride.created_at >= start, DischargeOverride.created_at <= end
        ),
        "altas_alto_risco": count(
            DischargeAssessment,
            DischargeAssessment.final_risk == "alto",
            DischargeAssessment.created_at >= start,
            DischargeAssessment.created_at <= end,
        ),
        "altas_confirmadas": count(
            DischargeAssessment,
            DischargeAssessment.confirmed_at.is_not(None),
            DischargeAssessment.confirmed_at >= start,
            DischargeAssessment.confirmed_at <= end,
        ),
        "passagens_geradas": count(HandoffReport, HandoffReport.created_at >= start, HandoffReport.created_at <= end),
        "passagens_do_cache": count(
            AuditLog, AuditLog.action == "handoff.cache_hit", AuditLog.created_at >= start, AuditLog.created_at <= end
        ),
        "reconciliacoes": count(MedrecRun, MedrecRun.created_at >= start, MedrecRun.created_at <= end),
        "itens_ia_descartados_sem_fonte": dropped,
        "consulta_ia": impact.public(impact.compute(db, start, end, uso_regular_min)),
    }


def _report(db: Session, since: date | None, until: date | None, uso_regular_min: int = impact.USO_REGULAR_MIN) -> dict:
    start, end = _range(since, until)
    return impact.compute(db, start, end, uso_regular_min)


@router.get("/metrics/export.csv")
def metrics_csv(
    since: date | None = None, until: date | None = None, db: Session = Depends(get_db), _: User = Depends(gestor)
) -> Response:
    """Painel Impacto em CSV: uma linha por métrica, com a fórmula e o número de linhas do audit_log que a compõem."""
    out = io.StringIO()
    fields = ["metrica", "rotulo", "valor", "unidade", "numerador", "denominador", "linhas_audit", "formula"]
    writer = csv.DictWriter(out, fieldnames=fields, delimiter=";")
    writer.writeheader()
    writer.writerows(impact.flat_rows(_report(db, since, until)))
    return Response(
        "\ufeff" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="impacto-housia.csv"'},
    )


@router.get("/metrics/measure-report")
def metrics_measure_report(
    since: date | None = None, until: date | None = None, db: Session = Depends(get_db), _: User = Depends(gestor)
) -> dict:
    """Painel Impacto como FHIR R4 MeasureReport (summary)."""
    return impact.measure_report(_report(db, since, until), clock.now())


@router.get("/metrics/{metrica}/linhas")
def metric_lines(
    metrica: str,
    since: date | None = None,
    until: date | None = None,
    uso_regular_min: int = Query(impact.USO_REGULAR_MIN, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(gestor),
) -> dict:
    """Drill-down: as linhas do audit_log que compõem o número de uma métrica do painel Impacto."""
    report = _report(db, since, until, uso_regular_min)
    m = report["metricas"].get(metrica)
    if m is None:
        raise HTTPException(404, f"Métrica desconhecida. Use uma de: {', '.join(report['metricas'])}")
    rows = impact.lines(db, report, metrica)
    return {
        "metrica": metrica,
        "rotulo": m["rotulo"],
        "valor": m["valor"],
        "unidade": m["unidade"],
        "formula": m["formula"],
        "total": len(rows),
        "items": [_entry(a, u) for a, u in rows],
    }


class BaselineIn(BaseModel):
    setor: str = Field(min_length=1, max_length=60, description="Especialidade da consulta (ex.: clinica_geral)")
    minutos_antes: float = Field(
        gt=0, le=600, description="Tempo médio de documentação por atendimento antes do HousIA"
    )
    observacao: str | None = Field(None, max_length=500)


@router.get("/impact/baseline")
def get_baselines(db: Session = Depends(get_db), _: User = Depends(gestor)) -> list[dict]:
    """Linha de base vigente por setor (a mais recente) e quem informou."""
    rows = impact.baselines(db)
    users = {u.id: u.name for u in db.scalars(select(User))}
    return [
        {
            "id": b.id,
            "setor": b.sector,
            "rotulo": impact.setor_label(b.sector),
            "minutos_antes": b.minutes_before,
            "observacao": b.note,
            "por": users.get(b.created_by or 0),
            "em": b.created_at.isoformat(),
        }
        for b in rows.values()
    ]


@router.post("/impact/baseline")
def post_baseline(body: BaselineIn, db: Session = Depends(get_db), user: User = Depends(gestor)) -> dict:
    """Formulário de linha de base: "tempo médio antes do HousIA" de um setor; gera os minutos e horas devolvidas."""
    if body.setor not in knowledge.SPECIALTIES:
        raise HTTPException(422, f"Setor desconhecido. Use uma especialidade de: {', '.join(knowledge.SPECIALTIES)}")
    b = ImpactBaseline(
        sector=body.setor,
        minutes_before=body.minutos_antes,
        note=(body.observacao or "").strip() or None,
        created_by=user.id,
        created_at=clock.now(),
    )
    db.add(b)
    db.flush()
    log_action(
        db,
        user,
        "impacto.linha_de_base",
        "impact_baseline",
        b.id,
        {"setor": b.sector, "minutos_antes": b.minutes_before, "observacao": b.note},
    )
    return {"id": b.id, "setor": b.sector, "minutos_antes": b.minutes_before}
