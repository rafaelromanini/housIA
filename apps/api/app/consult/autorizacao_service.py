"""Autorização prévia (#56): operadora da consulta, pacote de justificativa, revisão, andamento e exportação.

Regras de segurança: o pacote nasce em rascunho e nada sai (PDF/JSON, envio) antes de o médico revisar e aprovar; toda
afirmação da justificativa cita evidência desta consulta e deste paciente; a IA (opcional) só reescreve, e a reescrita é
descartada se acrescentar fato; pedido aprovado (ou enviado) não muda mais. O audit_log guarda ações, ids e contagens,
nunca o texto da justificativa. O acesso à consulta (vínculo, quebra de vidro, organização) vem do guarda da rota.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.consult import autorizacao, autorizacao_regras, consentimento, pdf, service
from app.consult.schemas import Autorizacao
from app.db.models import AutorizacaoPrevia, Consultation, User
from app.fhir_client.client import FhirSource
from app.llm.client import LLMError, generate_structured

STATUS = ("rascunho", "enviado", "aprovado", "negado")
# de -> para permitidos; aprovado é final (pedido aprovado não muda mais)
TRANSICOES = {
    "rascunho": {"enviado"},
    "enviado": {"aprovado", "negado"},
    "negado": {"rascunho"},
    "aprovado": set(),
}
MAX_OPERADORA = 60
MAX_PROTOCOLO = 60
AVISO_EXEMPLO = (
    "Pacote gerado com regras de EXEMPLO ou pendentes de conferência humana: não é o texto oficial do Rol da ANS ou de "
    "operadora. Confira a norma e a tabela da operadora antes de enviar."
)


class FraseSaida(BaseModel):
    texto: str
    refs: list[str] = []


class SaidaJustificativa(BaseModel):
    frases: list[FraseSaida]


def _mock(variables: dict) -> dict:
    """Sem IA externa (ou em LLM_MODE=mock) a reescrita devolve as próprias frases das regras."""
    return {"frases": [{"texto": f["texto"], "refs": f["refs"]} for f in variables["frases"]]}


def _limpar_texto(valor: str, maximo: int) -> str:
    sem = "".join(
        " " if ch.isspace() else ch for ch in (valor or "") if unicodedata.category(ch)[0] != "C" or ch.isspace()
    )
    return " ".join(sem.split())[:maximo]


# ---------- leitura ----------
def _pedido(db: Session, c: Consultation, aid: int) -> AutorizacaoPrevia:
    p = db.get(AutorizacaoPrevia, aid)  # a sessão já filtra pela organização: de outra org é 404
    if p is None or p.consultation_id != c.id:
        raise HTTPException(404, "Pedido de autorização não encontrado")
    return p


def _contexto(db: Session, fhir: FhirSource, c: Consultation):
    record, events, ctx = service._chart(fhir, c.patient_id)
    segments = service._segments(c)
    valid = service._valid_refs(record, events, ctx, segments, c.extra_json) | {f"Patient/{c.patient_id}"}
    return record, events, ctx, segments, valid


def _hash(p: AutorizacaoPrevia) -> str:
    pacote = p.pacote_json or {}
    corpo = {
        "regra": p.regra_id,
        "versao": p.regra_versao,
        "operadora": p.operadora,
        "frases": [[f.get("texto", ""), sorted(f.get("refs") or [])] for f in pacote.get("frases") or []],
    }
    return hashlib.sha256(json.dumps(corpo, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _publico(db: Session, p: AutorizacaoPrevia, valid: set[str] | None = None) -> dict:
    pacote = p.pacote_json or {}
    revisor = db.get(User, p.revisado_por) if p.revisado_por else None
    problemas = autorizacao.problemas_frases(pacote.get("frases") or [], valid) if valid is not None else []
    return {
        "id": p.id,
        "consulta": p.consultation_id,
        "regra_id": p.regra_id,
        "regra_versao": p.regra_versao,
        "procedimento": p.procedimento,
        "operadora": p.operadora,
        "status": p.status,
        "pacote": pacote,
        "revisado": p.revisado_em is not None and p.pacote_hash == _hash(p),
        "revisado_por": revisor.name if revisor else None,
        "revisado_em": p.revisado_em.isoformat() if p.revisado_em else None,
        "protocolo": p.protocolo,
        "enviado_em": p.enviado_em.isoformat() if p.enviado_em else None,
        "decidido_em": p.decidido_em.isoformat() if p.decidido_em else None,
        "problemas": problemas,
        "editavel": p.status == "rascunho",
    }


def listar(db: Session, fhir: FhirSource, user: User, cid: int) -> dict:
    """Checklist ao vivo (regras x consulta) + pedidos já montados. Não grava nada."""
    c = service._get(db, cid)
    base = autorizacao_regras.base_ativa()
    if c.kind == "cirurgia":
        raise HTTPException(409, "A autorização prévia vale para consulta e visita.")
    record, events, ctx, segments, valid = _contexto(db, fhir, c)
    ins = service._insights(c, record, events, ctx, segments)
    pedidos = list(
        db.scalars(
            select(AutorizacaoPrevia).where(AutorizacaoPrevia.consultation_id == c.id).order_by(AutorizacaoPrevia.id)
        )
    )
    return {
        "operadora": (c.extra_json or {}).get("autorizacao", {}).get("operadora", ""),
        "operadoras": sorted({e.nome for e in base.operadoras}),
        "autorizacoes": [a.model_dump(mode="json") for a in ins.autorizacoes],
        "pedidos": [_publico(db, p, valid) for p in pedidos],
        "base": {"aviso": base.aviso(), "regras": len(base.regras), "erros": len(base.erros)},
    }


def definir_operadora(db: Session, fhir: FhirSource, user: User, cid: int, nome: str) -> dict:
    c = service._get(db, cid)
    if c.kind == "cirurgia":
        raise HTTPException(409, "A autorização prévia vale para consulta e visita.")
    texto = _limpar_texto(nome, MAX_OPERADORA)
    extra = dict(c.extra_json or {})
    atual = dict(extra.get("autorizacao") or {})
    atual["operadora"] = texto
    c.extra_json = extra | {"autorizacao": atual}
    base = autorizacao_regras.base_ativa()
    achada = autorizacao_regras.achar_operadora(base, texto)
    log_action(
        db,
        user,
        "consulta.autorizacao_operadora",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "operadora": achada[0] if achada else ("desconhecida" if texto else "nenhuma")},
        commit=False,
    )
    db.commit()
    return listar(db, fhir, user, cid) | {"salvo": True}


# ---------- pacote ----------
def _reescrever(c: Consultation, a: Autorizacao, frases: list[dict]) -> tuple[list[dict], str, str]:
    """(frases, modo, aviso). A IA só reescreve; se acrescentar fato ou falhar, ficam as frases das regras."""
    rules_only = not consentimento.flags(c.extra_json).ia
    variables = {
        "procedimento": a.procedimento,
        "frases": [{"texto": f["texto"], "refs": f["refs"]} for f in frases],
    }
    try:
        res = generate_structured(
            "autorizacao_justificativa", variables, SaidaJustificativa, mock=_mock, effort="low", rules_only=rules_only
        )
    except LLMError:
        return frases, "regras", "A IA não respondeu: ficaram as frases das regras."
    if res.mode == "mock":
        return frases, "regras", ""
    novas = autorizacao.reescrita_segura(frases, [f.model_dump() for f in res.output.frases])
    if novas is None:
        return (
            frases,
            "regras",
            "A reescrita da IA acrescentou algo sem fonte e foi descartada: ficaram as frases das regras.",
        )
    return novas, "ia", ""


def gerar(db: Session, fhir: FhirSource, user: User, cid: int, regra_id: str) -> dict:
    """Monta (ou refaz, enquanto for rascunho) o pacote de justificativa de uma regra citada na consulta."""
    c = service._get(db, cid)
    if c.kind == "cirurgia":
        raise HTTPException(409, "A autorização prévia vale para consulta e visita.")
    record, events, ctx, segments, valid = _contexto(db, fhir, c)
    ins = service._insights(c, record, events, ctx, segments)
    a = next((x for x in ins.autorizacoes if x.id == regra_id), None)
    if a is None:
        raise HTTPException(404, "Essa autorização não aparece na análise da consulta (o pedido não foi citado).")
    if a.situacao == "regra_vencida":
        raise HTTPException(409, "A regra está fora de vigência: atualize a tabela do Rol/DUT antes de gerar o pacote.")
    existente = db.scalar(
        select(AutorizacaoPrevia).where(
            AutorizacaoPrevia.consultation_id == c.id, AutorizacaoPrevia.regra_id == regra_id
        )
    )
    if existente is not None and existente.status != "rascunho":
        raise HTTPException(409, f"Pedido já {existente.status}: não pode ser refeito.")
    frases_regra = autorizacao.frases_regras(a)
    frases, modo, aviso_ia = _reescrever(c, a, frases_regra)
    pacote = {
        "procedimento": a.procedimento,
        "tipo": a.tipo,
        "regra": a.regra.model_dump(),
        "operadora": a.operadora.model_dump(),
        "frases": frases,
        "checklist": [i.model_dump() for i in a.criterios],
        "documentos": [d.model_dump() for d in a.documentos],
        "anexos": autorizacao.anexos(a, c.extra_json or {}),
        "pendencias": [{"texto": i.texto, "pergunta": i.pergunta} for i in a.criterios if i.status == "faltando"],
        "prazo_dias": a.operadora.prazo_dias,
        "modo": modo,
        "avisos": [*a.avisos, *([aviso_ia] if aviso_ia else [])],
        "gerado_em": clock.now().isoformat(),
    }
    novo = existente is None
    p = existente or AutorizacaoPrevia(
        consultation_id=c.id, patient_id=c.patient_id, created_by=user.id, regra_id=a.id, status="rascunho"
    )
    p.regra_versao = a.regra.versao
    p.procedimento = a.procedimento
    p.operadora = a.operadora.nome or a.operadora.informada
    p.pacote_json = pacote
    p.pacote_hash, p.revisado_por, p.revisado_em = "", None, None  # refazer o pacote tira a aprovação
    if novo:
        db.add(p)
    db.flush()
    log_action(
        db,
        user,
        "consulta.autorizacao_gerada",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "pedido": p.id,
            "regra": a.id,
            "regra_versao": a.regra.versao,
            "origem_regra": a.regra.origem,
            "modo": modo,
            "frases": len(frases),
            "criterios_faltando": a.faltam,
            "refeito": not novo,
        },
        commit=False,
    )
    db.commit()
    return _publico(db, p, valid)


def _editavel(p: AutorizacaoPrevia) -> None:
    if p.status != "rascunho":
        raise HTTPException(409, f"Pedido {p.status}: não pode mais ser alterado.")


def editar(db: Session, fhir: FhirSource, user: User, cid: int, aid: int, frases: list[dict]) -> dict:
    """O profissional ajusta as frases (cada uma com a evidência). Editar tira a aprovação."""
    c = service._get(db, cid)
    p = _pedido(db, c, aid)
    _editavel(p)
    *_, valid = _contexto(db, fhir, c)
    limpas = [
        {
            "texto": _limpar_texto(f.get("texto", ""), autorizacao.MAX_FRASE + 1),
            "refs": list(dict.fromkeys(str(r) for r in (f.get("refs") or []))),
            "origem": "profissional",
        }
        for f in frases[: autorizacao.MAX_FRASES + 1]
    ]
    if len(frases) > autorizacao.MAX_FRASES:
        raise HTTPException(422, f"No máximo {autorizacao.MAX_FRASES} frases.")
    erros = autorizacao.problemas_frases(limpas, valid)
    if erros:
        raise HTTPException(
            422, {"codigo": "frases_invalidas", "mensagem": "Há frase sem evidência válida.", "problemas": erros}
        )
    pacote = dict(p.pacote_json or {})
    pacote["frases"] = limpas
    pacote["modo"] = "profissional"
    p.pacote_json = pacote
    p.pacote_hash, p.revisado_por, p.revisado_em = "", None, None
    log_action(
        db,
        user,
        "consulta.autorizacao_editada",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "pedido": p.id, "regra": p.regra_id, "frases": len(limpas)},
    )
    return _publico(db, p, valid)


def aprovar(db: Session, fhir: FhirSource, user: User, cid: int, aid: int) -> dict:
    """Revisão e aprovação do pacote pelo MÉDICO: só passa com toda afirmação citando evidência válida."""
    if user.role != "medico":
        raise HTTPException(403, "Só o médico revisa e aprova o pacote de justificativa.")
    c = service._get(db, cid)
    p = _pedido(db, c, aid)
    _editavel(p)
    *_, valid = _contexto(db, fhir, c)
    erros = autorizacao.problemas_frases((p.pacote_json or {}).get("frases") or [], valid)
    if erros:
        log_action(
            db,
            user,
            "consulta.autorizacao_bloqueada",
            "consultation",
            c.id,
            {"patient_id": c.patient_id, "pedido": p.id, "regra": p.regra_id, "problemas": len(erros)},
        )
        raise HTTPException(
            422,
            {
                "codigo": "frases_sem_fonte",
                "mensagem": "A justificativa tem afirmação sem evidência válida: corrija antes de aprovar.",
                "problemas": erros,
            },
        )
    p.revisado_por, p.revisado_em = user.id, clock.now()
    p.pacote_hash = _hash(p)
    log_action(
        db,
        user,
        "consulta.autorizacao_aprovada",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "pedido": p.id, "regra": p.regra_id, "regra_versao": p.regra_versao},
    )
    return _publico(db, p, valid)


def mudar_status(db: Session, fhir: FhirSource, user: User, cid: int, aid: int, novo: str, protocolo: str = "") -> dict:
    c = service._get(db, cid)
    p = _pedido(db, c, aid)
    if novo not in STATUS:
        raise HTTPException(422, "Status inválido.")
    if novo not in TRANSICOES[p.status]:
        raise HTTPException(409, f"Pedido {p.status}: não pode passar para {novo}.")
    if novo == "enviado" and not (p.revisado_em is not None and p.pacote_hash == _hash(p)):
        raise HTTPException(409, "O médico precisa revisar e aprovar o pacote antes de enviar.")
    antes = p.status
    agora = clock.now()
    p.status = novo
    if novo == "enviado":
        p.enviado_em = agora
        p.protocolo = _limpar_texto(protocolo, MAX_PROTOCOLO)
    elif novo in ("aprovado", "negado"):
        p.decidido_em = agora
        if protocolo:
            p.protocolo = _limpar_texto(protocolo, MAX_PROTOCOLO)
    elif novo == "rascunho":  # recurso após negativa: novo pacote, nova revisão
        p.pacote_hash, p.revisado_por, p.revisado_em, p.enviado_em, p.decidido_em = "", None, None, None, None
    log_action(
        db,
        user,
        "consulta.autorizacao_status",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "pedido": p.id, "regra": p.regra_id, "de": antes, "para": novo},
    )
    *_, valid = _contexto(db, fhir, c)
    return _publico(db, p, valid)


# ---------- exportação ----------
def _liberado(db: Session, fhir: FhirSource, c: Consultation, aid: int) -> tuple[AutorizacaoPrevia, dict]:
    p = _pedido(db, c, aid)
    *_, valid = _contexto(db, fhir, c)
    pub = _publico(db, p, valid)
    if not pub["revisado"]:
        raise HTTPException(409, "O médico precisa revisar e aprovar o pacote antes de qualquer saída.")
    if pub["problemas"]:
        raise HTTPException(409, "O pacote tem afirmação sem evidência válida: refaça e aprove de novo.")
    return p, pub


def exportar_json(db: Session, fhir: FhirSource, user: User, cid: int, aid: int) -> dict:
    c = service._get(db, cid)
    p, pub = _liberado(db, fhir, c, aid)
    bundle = autorizacao.fhir_bundle(pub, c.patient_id, c.encounter_id, author=user.name)
    log_action(
        db,
        user,
        "consulta.autorizacao_exportada",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "pedido": p.id, "regra": p.regra_id, "formato": "json"},
    )
    return {
        "aviso": AVISO_EXEMPLO if _de_exemplo(pub) else "",
        "pedido": {k: v for k, v in pub.items() if k not in ("problemas", "editavel")},
        "bundle": bundle,
    }


def _de_exemplo(pub: dict) -> bool:
    r = (pub["pacote"] or {}).get("regra") or {}
    return r.get("origem") == "exemplo" or r.get("conferencia") != "conferido"


def exportar_pdf(db: Session, fhir: FhirSource, user: User, cid: int, aid: int) -> tuple[bytes, str]:
    c = service._get(db, cid)
    p, pub = _liberado(db, fhir, c, aid)
    record, *_ = _contexto(db, fhir, c)
    texto = autorizacao.texto_pdf(pub, record.name, record.age, AVISO_EXEMPLO if _de_exemplo(pub) else "")
    log_action(
        db,
        user,
        "consulta.autorizacao_exportada",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "pedido": p.id, "regra": p.regra_id, "formato": "pdf"},
    )
    return (
        pdf.render(f"Autorização prévia: {p.procedimento}", texto, rodape=f"HousIA · consulta {c.id}"),
        f"autorizacao-{p.id}.pdf",
    )
