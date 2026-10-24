"""Controle de acesso à consulta por vínculo, com quebra de vidro justificada (#53).

Decisão conservadora (o projeto não modela equipe nem CareTeam; só `Consultation.created_by`):
- Médico abre a consulta que ele mesmo criou. Qualquer outra consulta da organização responde 403 até que ele registre
  uma justificativa (quebra de vidro): motivo de pelo menos 10 caracteres, vale por 8 horas só para aquela consulta,
  fica no audit_log (com o motivo) e abre um alerta "atencao" para o gestor.
- O vínculo NÃO é herdado por "já atendeu o paciente": criar uma consulta nova para ler as outras seria um atalho.
- Enfermagem e gestor não passam por esta trava (enfermagem trabalha na fila da unidade; o gestor nem abre consulta,
  `clinician` barra). Pendência declarada no STATUS: vínculo por equipe/CareTeam para a enfermagem.
- Consulta de outra organização continua 404 (o filtro de organização roda antes).
"""

from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.alerts.service import upsert_alert
from app.audit.service import log_action, log_leitura
from app.consult import acoes
from app.db.models import AuditLog, Consultation, User

MOTIVO_MIN = 10
MOTIVO_MAX = 300
VALIDADE_H = 8
ACAO_QUEBRA = "consulta.quebra_de_vidro"
ACAO_NEGADO = "consulta.acesso_negado"


def tem_vinculo(user: User, c: Consultation) -> bool:
    return user.role != "medico" or (c.created_by is not None and c.created_by == user.id)


def quebra_ativa(db: Session, user: User, cid: int) -> bool:
    desde = clock.now() - timedelta(hours=VALIDADE_H)
    return (
        db.scalar(
            select(AuditLog.id)
            .where(
                AuditLog.user_id == user.id,
                AuditLog.action == ACAO_QUEBRA,
                AuditLog.entity == "consultation",
                AuditLog.entity_id == str(cid),
                AuditLog.created_at >= desde,
            )
            .limit(1)
        )
        is not None
    )


def exigir(db: Session, user: User, c: Consultation | None) -> None:
    """403 para o médico sem vínculo e sem quebra de vidro vigente; a tentativa é auditada (sem repetir)."""
    if c is None or tem_vinculo(user, c) or quebra_ativa(db, user, c.id):
        return
    log_leitura(db, user, ACAO_NEGADO, "consultation", c.id, {"patient_id": c.patient_id})
    raise HTTPException(
        403,
        "Você não tem vínculo com esta consulta. Para abrir mesmo assim, registre a justificativa (quebra de vidro): "
        "ela fica na auditoria e o gestor é avisado.",
    )


def registrar_quebra(db: Session, user: User, c: Consultation, motivo: str) -> dict:
    motivo = " ".join((motivo or "").split())
    if user.role != "medico":
        raise HTTPException(403, "A quebra de vidro é do perfil médico.")
    if len(acoes.efetivo(motivo)) < MOTIVO_MIN:  # caractere invisível não é justificativa
        raise HTTPException(422, f"Informe o motivo do acesso (mínimo de {MOTIVO_MIN} caracteres).")
    if len(motivo) > MOTIVO_MAX:
        raise HTTPException(422, f"Motivo longo demais (máximo de {MOTIVO_MAX} caracteres).")
    if tem_vinculo(user, c):
        return {"consulta": c.id, "acesso": "vinculo", "quebra_de_vidro": False}
    validade = clock.now() + timedelta(hours=VALIDADE_H)
    log_action(
        db,
        user,
        ACAO_QUEBRA,
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "motivo": motivo, "valido_ate": validade.isoformat()},
        commit=False,
    )
    upsert_alert(  # avisa o gestor (painel de alertas); o motivo fica só na auditoria
        db,
        patient_id=c.patient_id,
        source="consulta",
        severity="atencao",
        title=f"Acesso por quebra de vidro: {user.name} abriu a consulta {c.id} sem vínculo",
        detail="Justificativa registrada na auditoria (consulta.quebra_de_vidro).",
        refs=[f"Consultation/{c.id}"],
    )
    db.commit()
    return {"consulta": c.id, "acesso": "quebra_de_vidro", "quebra_de_vidro": True, "valido_ate": validade.isoformat()}
