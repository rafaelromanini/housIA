"""Documentos derivados da consulta (#45): gerar, editar, aprovar, exportar e enviar, um DocumentReference cada.

Estado em `Consultation.extra_json["documentos"]` ({tipo: documento}); sem migração. A nota de origem precisa estar
revisada e atestada (`_require_review`) e sem pendência crítica aberta (`_gate_clinical_export`). O aprovado vale
para a nota como estava: se a nota mudar depois, ele fica `desatualizado` e precisa ser gerado de novo.
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.config import get_settings
from app.consult import consentimento, documentos, idiomas, service
from app.consult.export import selo_profissional
from app.db.models import Consultation, DischargeAssessment, User
from app.discharge.service import assessment_out
from app.fhir_client.client import FhirSource
from app.llm.client import LLMError, generate_structured
from app.planos import service as planos


class _Item(BaseModel):
    id: str
    texto: str


class _Traducao(BaseModel):
    itens: list[_Item]


def _estado(c: Consultation) -> dict:
    return dict((c.extra_json or {}).get("documentos") or {})


def _salvar(c: Consultation, estado: dict) -> None:
    c.extra_json = (c.extra_json or {}) | {"documentos": estado}


def _fontes(db: Session, fhir: FhirSource, user: User, c: Consultation) -> documentos.Fontes:
    if c.kind == "cirurgia":
        raise HTTPException(409, "Os documentos derivados valem para consulta e visita; a cirurgia usa o relatório.")
    if c.note_json is None:
        raise HTTPException(409, "Finalize a consulta e revise a nota antes de gerar documentos.")
    record, events, ctx = service._chart(fhir, c.patient_id)
    alta = None
    if planos.tem_modulo(db, "alta"):
        a = db.scalars(
            select(DischargeAssessment)
            .where(DischargeAssessment.patient_id == c.patient_id)
            .order_by(DischargeAssessment.created_at.desc())
        ).first()
        alta = assessment_out(db, a) if a else None
    role = {"medico": "Médico(a)", "enfermagem": "Enfermagem"}.get(user.role, user.role)
    return documentos.Fontes(
        consulta_id=c.id,
        paciente_id=c.patient_id,
        encontro_id=c.encounter_id,
        nome=record.name,
        quando=clock.now(),
        nota=service.reidentify(service._nota(c), record),
        record=record,
        events=events,
        pedidos=service._signed_orders(c),
        encaminhamentos=(c.insights_json or {}).get("encaminhamentos") or [],
        alergias=[(a.summary, a.source_ref) for a in ctx.alergias],
        faturamento=(c.extra_json or {}).get("faturamento") or {},
        alta=alta,
        profissional=f"{user.name} ({role})",
        selo=selo_profissional(user),
        idioma=c.patient_language,
    )


def _traducao(c: Consultation):
    """Tradutor por IA (prompt `consult_documentos`); None quando o paciente não autorizou a IA externa."""
    if not consentimento.flags(c.extra_json).traducao:
        return None

    def traduzir(itens: dict[str, str], idioma: str) -> dict[str, str]:
        try:
            result = generate_structured(
                "consult_documentos",
                {
                    "idioma": f"{idiomas.rotulo(idioma)} ({idioma})",
                    "itens": [{"id": k, "texto": v} for k, v in itens.items()],
                },
                _Traducao,
                mock=lambda _v: {"itens": []},  # sem IA real não há tradução: o documento fica em português
            )
        except LLMError:
            return {}
        return {i.id: i.texto for i in result.output.itens}

    return traduzir


def _hash_atual(c: Consultation, fontes: documentos.Fontes | None = None) -> str:
    return documentos.hash_nota(fontes.nota if fontes else service._nota(c) or {})


def listar(db: Session, fhir: FhirSource, user: User, cid: int) -> dict:
    c = service._get(db, cid)
    estado = _estado(c)
    out = []
    try:
        fontes = _fontes(db, fhir, user, c)
        indisponivel = documentos.disponibilidade(fontes)
        atual = _hash_atual(c, fontes)
    except HTTPException as exc:
        fontes, indisponivel, atual = None, {}, ""
        motivo = exc.detail
    for tipo, spec in documentos.TIPOS.items():
        doc = estado.get(tipo)
        out.append(
            {
                "tipo": tipo,
                "titulo": spec["titulo"],
                "loinc": spec["loinc"][0],
                "gerado": doc is not None,
                "indisponivel": indisponivel.get(tipo) if fontes else motivo,
                "desatualizado": bool(doc) and doc["nota_hash"] != atual,
                "aprovado": bool(doc and doc.get("aprovado")),
                "enviado": bool(doc and doc.get("fhir_ref")),
            }
        )
    return {"consulta_id": c.id, "documentos": out}


def obter(db: Session, fhir: FhirSource, user: User, cid: int, tipo: str) -> dict:
    c = service._get(db, cid)
    doc = _estado(c).get(_tipo(tipo))
    if doc is None:
        raise HTTPException(404, "Documento ainda não gerado.")
    return _com_estado(c, doc, fhir, db, user)


def _tipo(tipo: str) -> str:
    if tipo not in documentos.TIPOS:
        raise HTTPException(404, "Tipo de documento desconhecido.")
    return tipo


def _com_estado(c: Consultation, doc: dict, fhir: FhirSource, db: Session, user: User) -> dict:
    fontes = _fontes(db, fhir, user, c)
    return {k: v for k, v in doc.items() if k != "nota_hash"} | {
        "desatualizado": doc["nota_hash"] != _hash_atual(c, fontes),
        "texto": documentos.texto_final(doc, fontes),
    }


def gerar(db: Session, fhir: FhirSource, user: User, cid: int, tipo: str, opcoes: dict) -> dict:
    c = service._get(db, cid)
    tipo = _tipo(tipo)
    fontes = _fontes(db, fhir, user, c)
    try:
        doc = documentos.gerar(tipo, fontes, opcoes)
    except documentos.DocumentoIndisponivel as exc:
        raise HTTPException(409, str(exc)) from exc
    doc = documentos.adaptar(doc, c.patient_language, _traducao(c)) if tipo == "resumo_paciente" else doc
    if tipo == "resumo_paciente":
        doc["legibilidade"] = documentos.legibilidade(" ".join(it["texto"] for s in doc["secoes"] for it in s["itens"]))
    estado = _estado(c)
    antigo = estado.get(tipo)
    if antigo and antigo.get("fhir_ref"):
        raise HTTPException(409, f"Este documento já foi enviado ao prontuário ({antigo['fhir_ref']}).")
    estado[tipo] = doc
    _salvar(c, estado)
    log_action(
        db,
        user,
        "consulta.documento_gerado",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "tipo": tipo,
            "itens": sum(len(s["itens"]) for s in doc["secoes"]),
            "descartados": len(doc["descartados"]),
            "cid_incluido": any(s["titulo"] == "Diagnóstico" for s in doc["secoes"]),
        },
    )
    return _com_estado(c, doc, fhir, db, user)


def editar(db: Session, fhir: FhirSource, user: User, cid: int, tipo: str, secoes: list[dict]) -> dict:
    c = service._get(db, cid)
    tipo = _tipo(tipo)
    estado = _estado(c)
    atual = estado.get(tipo)
    if atual is None:
        raise HTTPException(404, "Documento ainda não gerado.")
    if atual.get("fhir_ref"):
        raise HTTPException(409, "Documento já enviado ao prontuário.")
    novo, erros = documentos.editar(atual, secoes)
    if erros:
        raise HTTPException(422, {"codigo": "item_obrigatorio", "mensagem": erros[0], "itens": erros})
    estado[tipo] = novo
    _salvar(c, estado)
    mudou = sum(
        1
        for s in novo["secoes"]
        for it in s["itens"]
        if it["origem"] == "profissional" and not it["id"].startswith("n")
    )
    log_action(
        db,
        user,
        "consulta.documento_editado",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "tipo": tipo, "itens_alterados": mudou},
    )
    return _com_estado(c, novo, fhir, db, user)


def aprovar(db: Session, fhir: FhirSource, user: User, cid: int, tipo: str) -> dict:
    c = service._get(db, cid)
    tipo = _tipo(tipo)
    estado = _estado(c)
    doc = estado.get(tipo)
    if doc is None:
        raise HTTPException(404, "Documento ainda não gerado.")
    service._require_review(c)
    fontes = _fontes(db, fhir, user, c)
    if doc["nota_hash"] != _hash_atual(c, fontes):
        raise HTTPException(409, "A nota mudou depois de gerar o documento: gere de novo.")
    estado[tipo] = doc | {"aprovado": {"por": user.name, "em": clock.now().isoformat()}}
    _salvar(c, estado)
    log_action(
        db, user, "consulta.documento_aprovado", "consultation", c.id, {"patient_id": c.patient_id, "tipo": tipo}
    )
    return _com_estado(c, estado[tipo], fhir, db, user)


def _liberado(db: Session, fhir: FhirSource, user: User, c: Consultation, tipo: str):
    """Documento aprovado, atual e com a nota revisada e sem pendência crítica: (documento, fontes)."""
    doc = _estado(c).get(tipo)
    if doc is None:
        raise HTTPException(404, "Documento ainda não gerado.")
    service._require_review(c)
    service._gate_clinical_export(c)
    if not doc.get("aprovado"):
        raise HTTPException(
            409, {"codigo": "documento_nao_aprovado", "mensagem": "Aprove o documento antes de exportar ou enviar."}
        )
    fontes = _fontes(db, fhir, user, c)
    if doc["nota_hash"] != _hash_atual(c, fontes):
        raise HTTPException(409, "A nota mudou depois da aprovação do documento: gere e aprove de novo.")
    return doc, fontes


def exportar(db: Session, fhir: FhirSource, user: User, cid: int, tipo: str) -> dict:
    c = service._get(db, cid)
    tipo = _tipo(tipo)
    doc, fontes = _liberado(db, fhir, user, c, tipo)
    texto_ = documentos.texto_final(doc, fontes)
    log_action(
        db, user, "consulta.documento_exportado", "consultation", c.id, {"patient_id": c.patient_id, "tipo": tipo}
    )
    return {"texto": texto_, "recurso": documentos.document_reference(doc, fontes, texto_)}


def pdf_bytes(db: Session, fhir: FhirSource, user: User, cid: int, tipo: str) -> tuple[bytes, str]:
    from app.consult import pdf

    result = exportar(db, fhir, user, cid, tipo)
    c = service._get(db, cid)
    return pdf.render(documentos.TIPOS[tipo]["titulo"], result["texto"], rodape=f"HousIA · consulta {c.id}"), (
        f"{tipo}-{c.id}.pdf"
    )


def enviar(db: Session, fhir: FhirSource, user: User, cid: int, tipo: str) -> dict:
    """Um DocumentReference por documento (LOINC do tipo). Sem EHR_WRITEBACK, ou em sandbox, só devolve o recurso."""
    c = service._get(db, cid)
    tipo = _tipo(tipo)
    doc, fontes = _liberado(db, fhir, user, c, tipo)
    if doc.get("fhir_ref"):
        raise HTTPException(409, f"Documento já enviado ({doc['fhir_ref']}).")
    texto_ = documentos.texto_final(doc, fontes)
    recurso = documentos.document_reference(doc, fontes, texto_)
    if not get_settings().ehr_writeback or c.sandbox:
        log_action(
            db, user, "consulta.documento_exportado", "consultation", c.id, {"patient_id": c.patient_id, "tipo": tipo}
        )
        return {"enviado": False, "texto": texto_, "recurso": recurso}
    criado = fhir.create(recurso)
    ref = f"DocumentReference/{criado['id']}"
    estado = _estado(c)
    estado[tipo] = doc | {"fhir_ref": ref}
    _salvar(c, estado)
    service.clear_chart_cache()
    log_action(
        db,
        user,
        "consulta.documento_enviado",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "tipo": tipo, "fhir_ref": ref, "loinc": doc["loinc"]["codigo"]},
    )
    return {"enviado": True, "fhir_ref": ref, "recurso": criado}
