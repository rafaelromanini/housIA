"""Ditado e adendo (#41): grava o que o profissional dita numa seção da nota, antes ou depois de finalizar.

Regras: só a seção alvo muda (as outras ficam idênticas); toda frase nova cita a `Fala/N` do adendo; o ditado nunca
tira item de segurança (alerta de medicamento, alergia) nem cria pendência crítica; o consentimento revogado barra;
aplicar tira a atestação (a nota volta à revisão); o audit guarda só contagens, nunca o texto ditado.
"""

from __future__ import annotations

import hashlib

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.consult import adendo, consentimento, reescrita, reescrita_service, service
from app.consult.schemas import AdendoIn, Insights, SegmentIn, fala_ref
from app.db.models import User
from app.fhir_client.client import FhirSource


def _hash(sec: dict) -> str:
    return hashlib.sha256((sec.get("texto", "")).encode()).hexdigest()[:16]


def _barrar(db: Session, user: User, c, key: str, codigo: str, mensagem: str, itens: list[dict]) -> None:
    db.rollback()  # nada do ditado (nem as falas) fica gravado
    log_action(
        db,
        user,
        "consulta.adendo_bloqueado",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "secao": key, "bloqueios": [codigo], "itens": len(itens)},
    )
    raise HTTPException(422, {"codigo": codigo, "mensagem": mensagem, "itens": itens})


def ditar(db: Session, fhir: FhirSource, user: User, cid: int, body: AdendoIn) -> dict:
    c = service._get(db, cid)
    key = body.secao
    if c.kind == "cirurgia":
        raise HTTPException(409, "O ditado por seção vale para consulta e visita.")
    if c.note_json is None:
        raise HTTPException(409, "Finalize a consulta antes de ditar um adendo.")
    if c.status == "enviada":
        raise HTTPException(409, "Nota já enviada ao prontuário.")
    service._require_not_purged(c)
    flags = consentimento.flags(c.extra_json)
    if not flags.captura:
        raise HTTPException(403, "Consentimento revogado: o ditado foi encerrado.")
    if body.via == "servidor" and not flags.stt:
        raise HTTPException(403, "O paciente não autorizou a transcrição no servidor: use o navegador ou digite.")
    if key == "pendencias":
        raise HTTPException(422, "As pendências não podem ser ditadas: elas vêm das regras.")
    nota = service._nota(c) or {}
    sec = reescrita.secao(c.note_json, key) or reescrita.secao(nota, key)
    if sec is None:
        raise HTTPException(404, "Seção não encontrada.")
    base_nota = c.note_json if key in reescrita.REWRITABLE else nota
    antes_texto = sec.get("texto", "")

    extra = dict(c.extra_json or {})
    pilha: list[dict] = list(extra.get("adendos") or [])
    comandos: list[dict] = []
    novos_segs = []
    segmentos = service._segments(c)
    adicionadas = removidas = 0
    quebra = False
    record, events, ctx = service._chart(fhir, c.patient_id)

    for item in body.segmentos:
        texto = adendo.limpar(item.texto)
        if not texto:
            continue
        cmd = adendo.comando(texto)
        if cmd == "proximo_campo":
            comandos.append({"comando": cmd, "efeito": "aplicado"})
        elif cmd == "nova_linha":
            quebra = True
            comandos.append({"comando": cmd, "efeito": "aplicado"})
        elif cmd == "desfazer":
            passo = next((p for p in reversed(pilha) if p["secao"] == key), None)
            if passo is None:
                comandos.append({"comando": cmd, "efeito": "ignorado", "motivo": "Nada para desfazer nesta seção."})
            elif passo["depois"] != _hash(sec):
                comandos.append(
                    {"comando": cmd, "efeito": "recusado", "motivo": "A seção foi editada depois do ditado."}
                )
            else:
                pilha.remove(passo)
                sec = passo["antes"]
                comandos.append({"comando": cmd, "efeito": "aplicado"})
        elif cmd == "apagar_ultima":
            res = adendo.sem_ultima(sec)
            if res is None:
                comandos.append({"comando": cmd, "efeito": "ignorado", "motivo": "A seção não tem frase para apagar."})
                continue
            nova, ultima = res
            ins = Insights.model_validate(c.insights_json) if c.insights_json else None
            alertas = [
                m.texto for m in (ins.sugestoes_medicamentos if ins else []) if m.severidade in ("critico", "atencao")
            ]
            if reescrita.protegidas(key, [ultima], alertas, reescrita_service._alergenos(record, ins)):
                _barrar(
                    db,
                    user,
                    c,
                    key,
                    "item_de_seguranca",
                    "O ditado foi bloqueado: apagar essa frase tiraria um item de segurança (alerta de medicamento "
                    "ou alergia). A nota não foi alterada.",
                    [{"codigo": "item_de_seguranca", "texto": ultima["texto"][:160]}],
                )
            pilha.append({"secao": key, "antes": sec, "depois": _hash(nova)})
            sec = nova
            removidas += 1
            comandos.append({"comando": cmd, "efeito": "aplicado"})
        else:
            seg_in = SegmentIn(falante="medico", texto=texto, origem="adendo", client_id=item.client_id, idioma="pt-BR")
            novos = service._add_segments(c, segmentos, [seg_in], permitir_adendo=True)
            if not novos:  # reenvio com o mesmo client_id: a fala já está na consulta
                comandos.append({"comando": "texto", "efeito": "ignorado", "motivo": "Trecho já recebido."})
                continue
            seg = novos[0]
            novos_segs.append(seg)
            frases = adendo.frases_do_ditado(texto, fala_ref(seg.n))
            if not frases:
                continue
            nova = adendo.acrescentar(sec, frases, quebra)
            quebra = False
            if len(nova["texto"]) > adendo.MAX_SECAO:
                db.rollback()
                raise HTTPException(
                    422, f"A seção passaria de {adendo.MAX_SECAO} caracteres: divida o adendo em outra seção."
                )
            pilha.append({"secao": key, "antes": sec, "depois": _hash(nova)})
            sec = nova
            adicionadas += len(frases)
    pilha = pilha[-adendo.MAX_PILHA :]

    if novos_segs:
        service._refresh_insights(c, record, events, ctx, segmentos)
    if sec.get("texto") != antes_texto:
        candidata = reescrita_service._com_secao(base_nota, key, sec, sec.get("frases") or [], sec.get("texto", ""))
        novas_pend = reescrita_service._pendencias(c, candidata) - reescrita_service._pendencias(c, c.note_json)
        if novas_pend:
            _barrar(
                db,
                user,
                c,
                key,
                "pendencia_critica",
                "O ditado foi bloqueado: a nota passaria a ter uma pendência crítica. A nota não foi alterada.",
                [{"codigo": "pendencia_critica", "texto": p} for p in sorted(novas_pend)],
            )
        c.note_json = candidata
        service._invalidate_attestation(c)
        meta = dict(c.note_meta_json or {})
        c.note_meta_json = meta | {"adendos": int(meta.get("adendos", 0)) + 1, "adendo_em": clock.now().isoformat()}
    c.extra_json = extra | {"adendos": pilha}
    resumo: dict[str, int] = {}
    for cm in comandos:
        resumo[cm["comando"]] = resumo.get(cm["comando"], 0) + 1
    log_action(
        db,
        user,
        "consulta.adendo_ditado",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "secao": key,
            "via": body.via,
            "falas": len(novos_segs),
            "frases_adicionadas": adicionadas,
            "frases_removidas": removidas,
            "comandos": resumo,
            "caracteres": sum(len(s.texto) for s in novos_segs),
            "sandbox": bool(c.sandbox),
        },
    )
    return {
        "secao": key,
        "antes": antes_texto,
        "depois": sec.get("texto", ""),
        "novos": [s.model_dump() for s in novos_segs],
        "comandos": comandos,
        "proximo_campo": any(cm["comando"] == "proximo_campo" for cm in comandos),
        "nota": service._nota(c),
    }
