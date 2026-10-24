"""Exportação da consulta finalizada para o e-SUS APS (#52): LEDI, com as mesmas travas do /export da nota."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.br import cadastro, ledi
from app.br import identificadores as ids
from app.consult import service
from app.db.models import User
from app.fhir_client.client import FhirSource
from app.patients.service import load_record


def exportar(db: Session, fhir: FhirSource, user: User, cid: int) -> dict:
    c = service._get(db, cid)
    if c.kind == "cirurgia":
        raise HTTPException(422, "A exportação e-SUS (LEDI) vale para consulta ambulatorial, não para cirurgia.")
    if c.sandbox:
        raise HTTPException(422, "Consulta de sandbox (cenário sintético) não é exportada para o e-SUS.")
    service._require_review(c)  # consulta não finalizada, não revisada ou sem atestação: 409
    service._require_not_purged(c)
    service._gate_clinical_export(c)
    record = load_record(fhir, c.patient_id)
    # quem atendeu (autor da consulta) é o profissional da ficha; outro colega da organização só exporta
    autor = db.get(User, c.created_by) if c.created_by and c.created_by != user.id else None
    cns_prof, cbo, cnes, _ = cadastro.do_profissional(db, autor or user)
    paciente = ids.do_paciente(record.patient)
    faltas = []
    if not cnes:
        faltas.append("CNES da organização (cadastro da organização, feito pelo gestor)")
    if not cbo:
        faltas.append("CBO do profissional (cadastro feito pelo gestor)")
    if not cns_prof:
        faltas.append("CNS do profissional (cadastro feito pelo gestor)")
    if not (paciente["cns"] or paciente["cpf"]):
        faltas.append("CNS ou CPF válido do paciente (no cadastro do prontuário)")
    if faltas:
        raise HTTPException(
            422,
            {
                "codigo": "cadastro_br_incompleto",
                "mensagem": "Faltam dados para o e-SUS (LEDI): " + "; ".join(faltas) + ".",
                "faltas": faltas,
            },
        )
    out = ledi.montar(
        consulta_id=c.id,
        quando=service._local(c.finished_at or c.created_at),
        nota=service.reidentify(service._nota(c), record),
        paciente=paciente | {"nascimento": ledi.nascimento_iso(record.patient.get("birthDate"))},
        sexo=record.sex,
        cns_profissional=cns_prof,  # type: ignore[arg-type]
        cbo=cbo,  # type: ignore[arg-type]
        cnes=cnes,  # type: ignore[arg-type]
    )
    aps = ((service._nota(c) or {}).get("modelo") or {}).get("tipo") == "aps"
    if not aps:
        out["avisos"].append("A consulta não usou o modelo de nota da APS; confira o enquadramento antes de importar.")
    log_action(
        db,
        user,
        "consulta.ledi_exportado",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "versao": ledi.VERSAO_LEDI,
            "cid": len(out["arquivo"]["atendimentosIndividuais"][0]["problemaCondicaoAvaliada"]["cid10"]),
            "procedimentos": len(out["arquivo"]["atendimentosIndividuais"][0]["procedimentosSigtap"]),
            "pendencias": len(out["pendencias"]),
            "aps": aps,
            "em": clock.now().isoformat(),
        },
    )
    return out | {"versao_ledi": ledi.VERSAO_LEDI}
