"""Laudos em PDF ou foto (#48): recebe, extrai por regras, propõe Observations e descarta o arquivo.

O arquivo só existe na memória desta chamada. Fica guardado (DocumentReference) apenas se o médico pedir
(`guardar=true`) e o prontuário aceitar escrita; por padrão nada do arquivo, do nome ou do texto é gravado
além dos valores extraídos (com a página e o trecho). O audit guarda só contagens.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import date

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.config import get_settings
from app.consult import laudos, propostas, service
from app.consult.chart import ChartView
from app.db.models import User
from app.fhir_client.client import FhirSource

MAX_LAUDOS = 10  # por consulta


def _consulta(db: Session, patient_id: str, cid: int):
    c = service._get(db, cid)  # a consulta de outra organização não existe para esta (RLS): 404
    if c.patient_id != patient_id:
        raise HTTPException(404, "Consulta não encontrada")
    return c


def listar(db: Session, patient_id: str, cid: int) -> dict:
    c = _consulta(db, patient_id, cid)
    return {"laudos": [laudos.publico(item) for item in (c.extra_json or {}).get("laudos") or []]}


def _recusar(db: Session, user: User, c, payload: dict, status: int, detail) -> HTTPException:
    log_action(db, user, "consulta.laudo_recusado", "consultation", c.id, {"patient_id": c.patient_id} | payload)
    return HTTPException(status, detail)


def enviar(
    db: Session,
    fhir: FhirSource,
    user: User,
    patient_id: str,
    cid: int,
    data: bytes,
    ciente: bool,
    guardar: bool,
) -> dict:
    if not ciente:
        raise HTTPException(422, "Confirme que o laudo é deste paciente e será lido pelo HousIA (nada fica gravado).")
    c = _consulta(db, patient_id, cid)
    if c.status == "enviada":
        raise HTTPException(409, "A consulta já foi enviada: não aceita novos laudos.")
    service._require_capture(c)  # consentimento revogado ou pausa de privacidade barram
    if not data:
        raise HTTPException(422, "Arquivo vazio.")
    if len(data) > laudos.MAX_BYTES:
        raise HTTPException(413, f"Arquivo maior que {laudos.MAX_BYTES // (1024 * 1024)} MB.")
    existentes = list((c.extra_json or {}).get("laudos") or [])
    if len(existentes) >= MAX_LAUDOS:
        raise HTTPException(409, f"Limite de {MAX_LAUDOS} laudos por consulta.")
    if guardar:
        if user.role != "medico":
            raise HTTPException(403, "Só o médico pode aprovar guardar o arquivo no prontuário.")
        if c.sandbox:
            raise HTTPException(409, "Consulta de sandbox nunca grava no prontuário.")
        if not get_settings().ehr_writeback:
            raise HTTPException(409, "Escrita no prontuário desligada (EHR_WRITEBACK=false): o arquivo não é guardado.")

    digest = hashlib.sha256(data).hexdigest()
    if any(item.get("sha256") == digest for item in existentes):
        raise HTTPException(409, "Este laudo já foi enviado nesta consulta.")
    record, events, ctx = service._chart(fhir, patient_id)
    try:
        kind = laudos.detectar(data)
        if kind == "pdf":
            laudos.conferir_pdf(data)
            pages = laudos.paginas_pdf(data)
        else:
            pages = [laudos.texto_imagem(data, kind, get_settings().llm_mode == "mock")]
        found = laudos.extrair("pdf" if kind == "pdf" else "imagem", pages, clock.now().date())
    except laudos.LaudoInvalido as exc:
        raise _recusar(db, user, c, {"bytes": len(data)}, exc.status, str(exc)) from exc
    ident = laudos.identidade(found.nome_laudo, record.name)
    if ident == "divergente":
        raise _recusar(
            db,
            user,
            c,
            {"motivo": "identidade"},
            422,
            "O nome no laudo não confere com o paciente desta consulta: nada foi aceito.",
        )
    if not found.valores:
        raise _recusar(
            db,
            user,
            c,
            {"motivo": "sem_valores", "ignorados": len(found.ignorados)},
            422,
            {
                "mensagem": "Nenhum exame reconhecido no laudo. Confira se há texto legível e unidades conhecidas.",
                "ignorados": found.ignorados,
            },
        )

    n = max((c.extra_json or {}).get("laudos_seq", 0), *[item["n"] for item in existentes], 0) + 1  # nunca reaproveita
    dia = date.fromisoformat(found.data_laudo) if found.data_laudo else None
    chart = ChartView(record, events, ctx, clock.now())
    for v in found.valores:
        v["ref"] = laudos.ref(n, v["pagina"])
        v["comparacao"], v["anterior_ref"] = laudos.comparar(chart, v, dia)
    laudo = {
        "n": n,
        "tipo": found.tipo,
        "paginas": len(found.paginas),
        "sha256": digest,
        "em": clock.now().isoformat(),
        "por": user.id,
        "data_laudo": found.data_laudo,
        "identidade": ident,
        "avisos": found.avisos,
        "valores": found.valores,
        "ignorados": found.ignorados,
        "guardado_ref": None,
    }
    if guardar:
        laudo["guardado_ref"] = _guardar(fhir, user, c, record, data, kind, n)
    c.extra_json = (c.extra_json or {}) | {"laudos": [*existentes, laudo], "laudos_seq": n}
    if c.status == "em_andamento":
        service._refresh_insights(c, record, events, ctx, service._segments(c))
    log_action(
        db,
        user,
        "consulta.laudo_enviado",
        "consultation",
        c.id,
        {
            "patient_id": patient_id,
            "tipo": found.tipo,
            "bytes": len(data),
            "paginas": len(found.paginas),
            "valores": len(found.valores),
            "ignorados": len(found.ignorados),
            "guardado": bool(laudo["guardado_ref"]),
            "identidade": ident,
        },
        commit=False,
    )
    db.commit()
    return {"laudo": laudos.publico(laudo), "propostas": _propostas_do_laudo(c, n)}


def _propostas_do_laudo(c, n: int) -> list[dict]:
    # as propostas só existem depois que a nota existe; antes disso o laudo aparece nos exames e na nota
    return [p for p in propostas.public(service._propostas(c)) if p["id"].startswith(f"exame:laudo-{n}-")]


def _guardar(fhir: FhirSource, user: User, c, record, data: bytes, kind: str, n: int) -> str:
    mime = {"pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg"}[kind]
    doc = {
        "resourceType": "DocumentReference",
        "status": "current",
        "docStatus": "preliminary",
        "type": {"coding": [{"system": "http://loinc.org", "code": "11502-2", "display": "Laboratory report"}]},
        "subject": {"reference": f"Patient/{record.id}"},
        "date": clock.now().isoformat(),
        "author": [{"display": user.name}],
        "description": f"Laudo enviado na consulta #{c.id} (Documento/{n}); guardado por decisão do médico.",
        "content": [
            {
                "attachment": {
                    "contentType": mime,
                    "data": base64.b64encode(data).decode(),
                    "size": len(data),
                    "title": f"Laudo Documento/{n}",
                }
            }
        ],
    }
    if c.encounter_id:
        doc["context"] = {"encounter": [{"reference": f"Encounter/{c.encounter_id}"}]}
    return f"DocumentReference/{fhir.create(doc)['id']}"


def remover(db: Session, fhir: FhirSource, user: User, patient_id: str, cid: int, n: int) -> dict:
    """Descarta os valores de um laudo (o arquivo nunca ficou). O DocumentReference guardado, se houver, permanece."""
    c = _consulta(db, patient_id, cid)
    if c.status == "enviada":
        raise HTTPException(409, "A consulta já foi enviada: o laudo não pode mais ser retirado.")
    items = list((c.extra_json or {}).get("laudos") or [])
    alvo = next((item for item in items if item["n"] == n), None)
    if alvo is None:
        raise HTTPException(404, "Laudo não encontrado")
    prefix = f"exame:laudo-{n}-"
    aceitas = [p for p in (c.extra_json or {}).get("propostas_aceitas") or [] if not p.startswith(prefix)]
    c.extra_json = (c.extra_json or {}) | {
        "laudos": [item for item in items if item["n"] != n],
        "propostas_aceitas": aceitas,
    }
    if c.status == "em_andamento":
        record, events, ctx = service._chart(fhir, patient_id)
        service._refresh_insights(c, record, events, ctx, service._segments(c))
    log_action(
        db,
        user,
        "consulta.laudo_removido",
        "consultation",
        c.id,
        {"patient_id": patient_id, "valores": len(alvo["valores"])},
    )
    return {"removido": n}
