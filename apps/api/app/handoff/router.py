import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_action
from app.auth.deps import get_current_user
from app.consult import stt, stt_stream, stt_vocab
from app.consult.router import MAX_FRAME_BYTES, MAX_SESSAO_BYTES, MAX_TEXTO_BYTES, _ws_autenticar
from app.db import session as db_session
from app.db.models import HandoffReport, User
from app.db.session import get_db
from app.db.tenancy import set_org
from app.fhir_client.client import FhirSource, get_fhir
from app.handoff import ao_vivo
from app.handoff.ao_vivo import MAX_TEXTO_REGRAS, SBAR, SegmentosIn
from app.handoff.schemas import Audience, HandoffCheckRequest, HandoffRequest
from app.handoff.service import (
    Conferidor,
    check_verbal_handoff,
    conferir_ao_vivo,
    generate_handoff,
    report_out,
    verbal_check_ao_vivo,
)
from app.llm.client import LLMError
from app.patients import avulso
from app.patients.service import load_record
from app.planos import service as planos
from app.planos.service import exige_modulo

router = APIRouter(tags=["passagem de plantão"], dependencies=[Depends(exige_modulo("passagem_plantao"))])


@router.post("/patients/{patient_id}/handoff")
def create_handoff(
    patient_id: str,
    body: HandoffRequest,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(get_current_user),
) -> dict:
    """Gera (ou devolve do cache) a passagem de plantão para o público pedido."""
    try:
        return generate_handoff(db, fhir, user, patient_id, body)
    except LLMError as exc:
        db.rollback()
        log_action(db, user, "handoff.erro_llm", "patient", patient_id, {"erro": str(exc)})
        raise HTTPException(502, str(exc)) from exc


@router.get("/patients/{patient_id}/handoff/latest")
def latest_handoff(
    patient_id: str,
    audience: Audience,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict | None:
    report = db.scalar(
        select(HandoffReport)
        .where(HandoffReport.patient_id == patient_id, HandoffReport.audience == audience)
        .order_by(HandoffReport.id.desc())
    )
    if report is None:
        return None
    log_action(db, user, "handoff.visualizado", "handoff_report", report.id, {"patient_id": patient_id})
    return report_out(report, cached=True)


@router.post("/patients/{patient_id}/handoff/check")
def check_handoff(
    patient_id: str,
    body: HandoffCheckRequest,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(get_current_user),
) -> dict:
    """Confere a passagem verbal: fatos relevantes mencionados, omitidos ou contraditos no relato."""
    try:
        return check_verbal_handoff(db, fhir, user, patient_id, body)
    except LLMError as exc:
        log_action(db, user, "handoff.erro_llm", "patient", patient_id, {"erro": str(exc)})
        raise HTTPException(502, str(exc)) from exc


# ---------- passagem falada com conferência ao vivo (#42) ----------
@router.post("/patients/{patient_id}/handoff/verbal-check/segments")
def verbal_check_segments(
    patient_id: str,
    body: SegmentosIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(get_current_user),
) -> dict:
    """Reroda a conferência com as falas acumuladas da passagem (a cada fala). Devolve o que foi mencionado, o que
    ainda falta e o que diverge do prontuário (com a fonte e a fala), mais o roteiro SBAR. Com `final=true` roda a
    conferência completa e audita (contagens). O relato não fica gravado."""
    try:
        return verbal_check_ao_vivo(db, fhir, user, patient_id, body)
    except LLMError as exc:
        log_action(db, user, "handoff.erro_llm", "patient", patient_id, {"erro": str(exc)})
        raise HTTPException(502, str(exc)) from exc


@router.get("/handoff/verbal-check/roteiro")
def verbal_check_roteiro(_: User = Depends(get_current_user)) -> dict:
    """Roteiro SBAR que guia quem fala (Situação, Histórico, Avaliação, Recomendação)."""
    return {"etapas": [{k: e[k] for k in ("sigla", "titulo", "guia")} for e in SBAR]}


@router.get("/handoff/demo-scripts")
def handoff_demo_scripts(_: User = Depends(get_current_user)) -> list[dict]:
    """Roteiros sintéticos de passagem falada (demonstração sem microfone)."""
    return [
        {k: s[k] for k in ("id", "patient_id", "audience", "titulo")} | {"falas": len(s["falas"])}
        for s in ao_vivo.demo_scripts().values()
    ]


@router.get("/handoff/demo-scripts/{script_id}")
def handoff_demo_script(script_id: str, _: User = Depends(get_current_user)) -> dict:
    script = ao_vivo.demo_scripts().get(script_id)
    if script is None:
        raise HTTPException(404, "Roteiro não encontrado")
    return script


def _ws_abrir(org_id: int, patient_id: str, audience: str, window_h: int):
    """Prepara a conexão: plano com o módulo, paciente visível e o contexto da conferência (lido uma vez)."""
    with db_session.SessionLocal() as db:
        set_org(db, org_id)
        if not planos.tem_modulo(db, "passagem_plantao"):
            raise HTTPException(403, "O plano não inclui a passagem de plantão.")
        avulso.ensure_visible(db, patient_id)
        record = load_record(get_fhir(), patient_id)
        vocab = stt_vocab.prompt(record, "", org_id)
        return Conferidor(db, get_fhir(), patient_id, audience, window_h), vocab


def _ws_auditar_passagem(user_id: int, org_id: int, patient_id: str, payload: dict) -> None:
    with db_session.SessionLocal() as db:
        set_org(db, org_id)
        log_action(db, db.get(User, user_id), "handoff.audio_stream", "patient", patient_id, payload)


class _RelatoGrande(Exception):
    """O texto acumulado da passagem passou do teto (MAX_TEXTO_REGRAS)."""


# o WebSocket fica fora do router com a dependência HTTP do módulo: o plano é conferido em `_ws_abrir`
ws_router = APIRouter(tags=["passagem de plantão"])


@ws_router.websocket("/ws/patients/{patient_id}/handoff/audio")
async def passagem_audio(
    ws: WebSocket, patient_id: str, audience: str = "medico", janela_h: int = 12, ciente: bool = False
) -> None:
    """Áudio PCM 16 kHz mono int16 (blocos de ~500 ms) -> {tipo: parcial|final, texto, conferencia}.

    A conferência roda a cada trecho (parcial inclusive) sobre o relato acumulado, só com as regras. O áudio e o texto
    ficam só na memória da conexão; o fim da passagem (POST .../verbal-check/segments com final=true) audita."""
    await ws.accept()

    async def erro(msg: str, code: int = 1008) -> None:
        await ws.send_json({"tipo": "erro", "mensagem": msg})
        await ws.close(code=code)

    try:
        uid, org, exp = await asyncio.to_thread(_ws_autenticar, ws)
    except PermissionError as exc:
        return await erro(str(exc))
    if not ciente:
        return await erro("Confirme que a fala será transcrita e conferida (nada fica gravado).")
    if audience not in ("medico", "enfermagem") or not 1 <= janela_h <= 72:
        return await erro("Público ou janela inválidos.")
    engine = stt_stream.motor()
    if engine is None:
        return await erro("Transcrição no servidor não configurada (STT_URL).", 1011)
    try:
        conferidor, vocab = await asyncio.to_thread(_ws_abrir, org, patient_id, audience, janela_h)
    except HTTPException as exc:
        return await erro(str(exc.detail))
    sessao = stt_stream.Sessao(engine, "pt-BR", vocab)
    falas: list[tuple[int, str]] = []
    finais = 0

    async def entregar(eventos: list[stt_stream.Evento]) -> None:
        nonlocal finais
        for ev in eventos:
            if sum(len(t) for _, t in falas) + len(ev.texto) > MAX_TEXTO_REGRAS:
                raise _RelatoGrande
            if ev.tipo == "parcial":
                parcial = [*falas, (len(falas) + 1, ev.texto)]
                conf = await asyncio.to_thread(conferir_ao_vivo, conferidor, parcial, False)
                await ws.send_json({"tipo": "parcial", "texto": ev.texto, "conferencia": conf})
                continue
            falas.append((len(falas) + 1, ev.texto))
            finais += 1
            conf = await asyncio.to_thread(conferir_ao_vivo, conferidor, list(falas), False)
            await ws.send_json({"tipo": "final", "fala": {"n": len(falas), "texto": ev.texto}, "conferencia": conf})

    auditado = False

    async def auditar() -> None:
        """Grava a trilha uma única vez, ANTES de o cliente receber o aviso final (senão ele a lê cedo demais)."""
        nonlocal auditado
        if auditado:
            return
        auditado = True
        await asyncio.to_thread(
            _ws_auditar_passagem, uid, org, patient_id, {"bytes": sessao.bytes_total, "frases": finais, "ciente": True}
        )

    async def auditar_e_erro(msg: str, code: int = 1008) -> None:
        await auditar()
        await erro(msg, code)

    await ws.send_json({"tipo": "pronto", "formato": "pcm16", "taxa": stt_stream.TAXA, "bloco_ms": 500})
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if time.time() >= exp:
                await auditar_e_erro("Sessão expirada.")
                break
            if msg.get("bytes") is not None:
                if len(msg["bytes"]) > MAX_FRAME_BYTES:
                    await auditar_e_erro("Bloco de áudio grande demais.", 1009)
                    break
                if sessao.bytes_total + len(msg["bytes"]) > MAX_SESSAO_BYTES:
                    await auditar_e_erro("Limite de duração da transmissão atingido.", 1009)
                    break
                await entregar(await asyncio.to_thread(sessao.adicionar, msg["bytes"]))
            elif msg.get("text"):
                if len(msg["text"]) > MAX_TEXTO_BYTES:
                    await auditar_e_erro("Mensagem de controle grande demais.", 1009)
                    break
                try:
                    cmd = json.loads(msg["text"])
                except ValueError:
                    continue
                if isinstance(cmd, dict) and cmd.get("tipo") == "fim":
                    await entregar(await asyncio.to_thread(sessao.encerrar))
                    await auditar()  # antes do "fim": quem recebe o fim já pode conferir a trilha
                    await ws.send_json({"tipo": "fim"})
                    await ws.close()
                    break
    except WebSocketDisconnect:
        pass
    except _RelatoGrande:
        try:
            await auditar_e_erro("O relato passou do limite: encerre a passagem.", 1009)
        except Exception:  # noqa: BLE001
            pass
    except stt.STTUnavailable as exc:
        try:
            await auditar_e_erro(str(exc), 1011)
        except Exception:  # noqa: BLE001
            pass
    finally:
        sessao.buf.clear()
        falas.clear()  # o relato só existe na memória da conexão
        await auditar()
