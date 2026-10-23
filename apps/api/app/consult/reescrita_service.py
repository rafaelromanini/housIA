"""Edição da nota por instrução (#46): rota de reescrita de uma seção.

A regra de decisão está em `reescrita.py`; aqui ficam o consentimento, o prompt, a trava de pendência crítica, a
gravação (que tira a atestação) e a auditoria, que guarda só contagens e códigos, nunca a instrução nem o texto.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.consult import consentimento, estilo, reescrita, service
from app.consult.schemas import Insights
from app.db.models import Consultation, User
from app.fhir_client.client import FhirSource
from app.llm.client import LLMError, generate_structured

MAX_REESCRITAS = 50  # registro de proveniência guardado por consulta (sem texto)


def _alergenos(record, ins: Insights | None) -> list[str]:
    nomes = [a.alergeno for a in ins.alergias] if ins else []
    nomes += [(a.get("code") or {}).get("text", "") for a in record.allergies]
    return [t.strip() for n in nomes for t in re.split(r"[/,()]", n) if len(t.strip()) >= 5]


def _com_secao(note: dict, key: str, sec: dict, frases: list[dict], texto: str) -> dict:
    refs = list(dict.fromkeys(r for f in frases for r in f["refs"]))
    novo = dict(note)
    if key in reescrita.REWRITABLE:
        novo[key] = sec | {"texto": texto, "refs": refs, "frases": frases}
    else:
        novo["secoes"] = [
            (s | {"texto": texto, "refs": refs}) if isinstance(s, dict) and s.get("key") == key else s
            for s in note.get("secoes") or []
        ]
    return novo


def _pendencias(c: Consultation, note: dict) -> set[str]:
    """Códigos de pendência crítica que a nota (hipotética) teria: a reescrita não pode criar nenhuma."""
    shim = SimpleNamespace(kind=c.kind, note_json=note, insights_json=c.insights_json)
    return {p["codigo"] for p in service.clinical_pending(shim)}  # type: ignore[arg-type]


def reescrever(db: Session, fhir: FhirSource, user: User, cid: int, key: str, instrucao: str, aplicar: bool) -> dict:
    c = service._get(db, cid)
    if c.kind == "cirurgia":
        raise HTTPException(409, "A reescrita por instrução vale para consulta e visita.")
    if c.note_json is None:
        raise HTTPException(409, "Finalize a consulta antes de reescrever a nota.")
    if c.status == "enviada":
        raise HTTPException(409, "Nota já enviada ao prontuário.")
    instrucao = instrucao.strip()
    if not 3 <= len(instrucao) <= reescrita.MAX_INSTRUCAO:
        raise HTTPException(422, f"A instrução deve ter de 3 a {reescrita.MAX_INSTRUCAO} caracteres.")
    if key == "pendencias":
        raise HTTPException(422, "As pendências não podem ser reescritas por instrução.")
    nota = service._nota(c) or {}
    sec = reescrita.secao(c.note_json, key) or reescrita.secao(nota, key)
    if sec is None:
        raise HTTPException(404, "Seção não encontrada.")
    record, events, ctx = service._chart(fhir, c.patient_id)
    valid = service._valid_refs(record, events, ctx, service._segments(c))
    ins = Insights.model_validate(c.insights_json) if c.insights_json else None
    antigas = reescrita.frases_de(sec)
    alertas = [m.texto for m in (ins.sugestoes_medicamentos if ins else []) if m.severidade in ("critico", "atencao")]
    protegidas = reescrita.protegidas(key, antigas, alertas, _alergenos(record, ins))
    perfil = estilo.obter(db, user)
    pseudo = service._pseudo(record)
    variables = {
        "secao": reescrita.rotulo(nota, key),
        "frases": [{"texto": f["texto"], "refs": f.get("refs", [])} for f in antigas],
        "protegidas": [f["texto"] for f in protegidas],
        "estilo": estilo.dicas(perfil) or ["sem preferência aprendida"],
        "instrucao": pseudo.apply(instrucao),
    }
    try:
        result = generate_structured(
            "consult_note_rewrite",
            variables,
            reescrita.Saida,
            mock=reescrita.mock,
            effort="medium",
            rules_only=not consentimento.flags(c.extra_json).ia,
        )
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    novas, descartadas = reescrita.montar(antigas, result.output.frases, variables["instrucao"], valid)
    antes = sec.get("texto", "")
    depois = reescrita.juntar(antes, novas)
    if not antigas and not novas:
        depois = antes  # seção sem registro que continua sem registro: o texto-padrão não vira vazio
    base_nota = c.note_json if key in reescrita.REWRITABLE else nota  # seção do modelo: nota antiga é lida com o modelo
    candidata = _com_secao(base_nota, key, sec, novas, depois)
    barrados = reescrita.bloqueios(antigas, novas, protegidas)
    novas_pendencias = _pendencias(c, candidata) - _pendencias(c, c.note_json)
    barrados += [{"codigo": "pendencia_critica", "texto": codigo} for codigo in sorted(novas_pendencias)]
    base_log = {
        "patient_id": c.patient_id,
        "secao": key,
        "modo": result.mode,
        "aplicar": aplicar,
        "palavras_antes": len(antes.split()),
        "palavras_depois": len(depois.split()),
        "descartadas": len(descartadas),
        "estilo_ativo": perfil["ativo"],
    }
    if barrados:
        log_action(
            db,
            user,
            "consulta.nota_reescrita_bloqueada",
            "consultation",
            c.id,
            base_log | {"bloqueios": sorted({b["codigo"] for b in barrados}), "itens": len(barrados)},
        )
        raise HTTPException(
            422,
            {
                "codigo": "reescrita_bloqueada",
                "mensagem": "A reescrita foi bloqueada: ela tiraria ou mudaria um item de segurança "
                "(alerta de medicamento, alergia ou pendência crítica). A nota não foi alterada.",
                "itens": barrados,
            },
        )
    saida = {
        "secao": key,
        "rotulo": reescrita.rotulo(nota, key),
        "antes": antes,
        "depois": depois,
        "frases": novas,
        "refs": list(dict.fromkeys(r for f in novas for r in f["refs"])),
        "diff": reescrita.diff(antes, depois),
        "descartadas": descartadas,
        "protegidas": len(protegidas),
        "modo": result.mode,
        "aplicada": aplicar,
        "estilo": {"ativo": perfil["ativo"], "dicas": estilo.dicas(perfil)},
        "avisos": []
        if result.mode == "real" or antes != depois
        else ["Nada mudou: a instrução não foi reconhecida pelo modo sem IA externa."],
    }
    if aplicar:
        c.note_json = candidata
        service._invalidate_attestation(c)
        extra = c.extra_json or {}
        historico = list(extra.get("reescritas") or [])
        historico.append({"secao": key, "por": user.name, "em": clock.now().isoformat(), "modo": result.mode})
        c.extra_json = extra | {"reescritas": historico[-MAX_REESCRITAS:]}
        log_action(
            db, user, "consulta.nota_reescrita", "consultation", c.id, base_log | {"pct": saida["diff"]["pct_editado"]}
        )
        saida["nota"] = service._nota(c)
    else:
        log_action(db, user, "consulta.nota_reescrita_previa", "consultation", c.id, base_log)
    return saida
