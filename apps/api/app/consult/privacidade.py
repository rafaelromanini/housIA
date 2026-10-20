"""Privacidade da consulta (#31): retenção por tipo, expurgo do texto das falas, exclusão pelo titular e o painel.

O painel é gerado da configuração em vigor (retenção, chave, captura de voz, LLM), não de um texto fixo.
"""

import hashlib
import logging
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.config import get_settings
from app.consult import stt
from app.db import crypto
from app.db.models import Consultation, User

logger = logging.getLogger("housia")
KINDS = ("consulta", "visita", "cirurgia")
_FALA = re.compile(r"^Fala/(\d+)$")


def retention_days() -> dict[str, int | None]:
    """TRANSCRIPT_RETENTION_DAYS por tipo de atendimento (None: sem expurgo). Valor inválido é ignorado."""
    raw = get_settings().transcript_retention_days.strip()
    out: dict[str, int | None] = dict.fromkeys(KINDS)
    if not raw:
        return out
    for part in (p.strip() for p in raw.split(",") if p.strip()):
        kind, sep, days = part.partition("=")
        if not sep:  # "90": todos os tipos
            kind, days = "*", part
        try:
            n = int(days)
        except ValueError:
            logger.warning("TRANSCRIPT_RETENTION_DAYS inválido: %r", part)
            continue
        if n < 0:
            continue
        for k in KINDS if kind.strip() == "*" else [kind.strip()]:
            if k in out:
                out[k] = n
    return out


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _cited(node, found: set[int]) -> None:
    if isinstance(node, str):
        m = _FALA.match(node)
        if m:
            found.add(int(m.group(1)))
    elif isinstance(node, dict):
        for v in node.values():
            _cited(v, found)
    elif isinstance(node, list):
        for v in node:
            _cited(v, found)


REDACTED = "[trecho expurgado]"
_QUOTED = re.compile(r"“([^”]*)”")
MIN_LITERAL = 12  # texto de fala menor que isso ("sim", "não sei") não é procurado solto, só entre aspas
# extra_json guarda cópias da nota (a nota fica); o resto pode ter citações e perguntas sobre a conversa
KEEP_IN_EXTRA = {"nota_original", "versoes_nota"}


def _redact(s: str, texts: list[str]) -> str:
    """Tira as citações literais de fala: “...” que abre uma fala (o motor corta e põe …) e a fala inteira."""

    def quoted(m: re.Match) -> str:
        inner = m.group(1).rstrip("…").strip()
        return f"“{REDACTED}”" if inner and any(t.startswith(inner) for t in texts) else m.group(0)

    s = _QUOTED.sub(quoted, s)
    for t in texts:
        if len(t) >= MIN_LITERAL and t in s:
            s = s.replace(t, REDACTED)
    return s


def _scrub_quotes(node, texts: list[str]):
    """Zera `trecho` e as citações de fala que as ideias, o resumo e os metadados guardam, em qualquer nível."""
    if isinstance(node, str):
        return _redact(node, texts)
    if isinstance(node, dict):
        return {k: ("" if k == "trecho" and isinstance(v, str) else _scrub_quotes(v, texts)) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub_quotes(v, texts) for v in node]
    return node


def _scrub_extra(extra: dict, texts: list[str]) -> dict:
    out = {k: (v if k in KEEP_IN_EXTRA else _scrub_quotes(v, texts)) for k, v in extra.items()}
    cop = out.get("copiloto")
    if isinstance(cop, dict):  # a pergunta é texto livre sobre a conversa; as respostas já passaram pelo scrub
        out["copiloto"] = cop | {
            "mensagens": [m | {"pergunta": ""} if isinstance(m, dict) else m for m in cop.get("mensagens", [])]
        }
    return out


def _reference_date(c: Consultation):
    if c.sent_at:
        return c.sent_at
    return c.finished_at if c.status in ("finalizada", "enviada") else None


def purge_consultation(db: Session, c: Consultation, days: int) -> None:
    """Apaga o texto das falas; a nota fica, com o SHA-256 de cada fala que ela cita."""
    cited: set[int] = set()
    _cited(c.note_json, cited)
    hashes: dict[str, str] = {}
    segments = []
    texts: list[str] = []
    for s in c.segments_json or []:
        texts += [t.strip() for t in (s.get("texto"), s.get("texto_original"), s.get("traducao")) if t and t.strip()]
    texts = sorted(set(texts), key=len, reverse=True)
    for s in c.segments_json or []:
        n = s.get("n")
        if n in cited and s.get("texto"):
            hashes[f"Fala/{n}"] = sha256(s["texto"])
        segments.append(
            s
            | {
                "texto": "",
                "traducao": None,
                "traducao_divergente": [],
                "texto_original": None,
                "expurgada": True,
            }
        )
    c.segments_json = segments
    c.insights_json = _scrub_quotes(c.insights_json, texts) if c.insights_json else c.insights_json
    if c.note_meta_json:  # "fora" lista falas inteiras; o rascunho da IA espelha a nota, que fica
        c.note_meta_json = {
            k: v if k == "rascunho_ia" else _scrub_quotes(v, texts) for k, v in c.note_meta_json.items()
        }
    meta = dict(c.ai_meta_json or {})
    meta.pop("saida", None)
    c.ai_meta_json = _scrub_quotes(meta, texts) or None
    c.extra_json = _scrub_extra(c.extra_json or {}, texts) | {
        "expurgada_em": clock.now().isoformat(),
        "falas_hash": hashes,
    }
    log_action(
        db,
        None,
        "consulta.expurgada",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "tipo": c.kind,
            "retencao_dias": days,
            "falas": len(segments),
            "refs_com_hash": len(hashes),
        },
        commit=False,
    )


def run(db: Session) -> int:
    """Expurga as consultas cujo prazo venceu. Devolve quantas foram expurgadas."""
    limits = retention_days()
    if not any(v is not None for v in limits.values()):
        return 0
    now = clock.now()
    done = 0
    ids = list(db.scalars(select(Consultation.id).where(Consultation.status.in_(["finalizada", "enviada"]))))
    for cid in ids:  # uma linha por vez: a que não decifra (chave errada, dado adulterado) não trava as outras
        try:
            c = db.get(Consultation, cid)
            days = limits.get(c.kind)
            ref = _reference_date(c)
            if days is None or ref is None or (c.extra_json or {}).get("expurgada_em"):
                continue
            if now - ref >= timedelta(days=days):
                purge_consultation(db, c, days)
                db.commit()
                done += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.error("Consulta %s não pôde ser expurgada (chave ausente/errada ou dado adulterado).", cid)
    return done


def delete_patient_data(db: Session, user: User, patient_id: str) -> dict:
    """Exclusão a pedido do titular (LGPD art. 18): remove as consultas do paciente e registra na auditoria."""
    rows = list(db.scalars(select(Consultation).where(Consultation.patient_id == patient_id)))
    ids = [c.id for c in rows]
    for c in rows:
        db.delete(c)
    log_action(
        db,
        user,
        "consulta.dados_excluidos",
        "patient",
        patient_id,
        {"consultas": len(ids), "ids": ids},
        commit=False,
    )
    db.commit()
    return {"patient_id": patient_id, "consultas_excluidas": len(ids)}


def panel(kind: str | None = None) -> dict:
    """'Privacidade desta consulta': o que fica guardado, por quanto tempo e quem processa, lido da configuração."""
    s = get_settings()
    limits = retention_days()
    days = limits.get(kind) if kind else None
    if kind:
        retencao = (
            f"O texto das falas é apagado {days} dia(s) após o envio da nota; "
            "a nota e o hash SHA-256 das falas citadas ficam."
            if days is not None
            else "Sem prazo definido (TRANSCRIPT_RETENTION_DAYS vazio): "
            "o texto das falas fica guardado até o gestor excluí-lo."
        )
    elif any(v is not None for v in limits.values()):
        retencao = (
            "Texto das falas apagado após o envio da nota, no prazo de cada tipo; "
            "a nota e o hash das falas citadas ficam."
        )
    else:
        retencao = (
            "Sem prazo definido (TRANSCRIPT_RETENTION_DAYS vazio): o texto das falas fica guardado até a exclusão."
        )
    if s.allow_browser_stt:
        voz = "Microfone: o reconhecimento de fala do navegador processa o áudio no fabricante do navegador."
    elif stt.enabled():
        voz = "O áudio não sai do hospital: o reconhecimento de fala roda no servidor configurado."
    else:
        voz = "O áudio não sai do hospital: o modo Microfone está desligado (ALLOW_BROWSER_STT=false); use Digitar."
    if s.llm_mode == "real":
        ia = (
            "Análise e nota por IA externa (Anthropic), só com consentimento, "
            "com o nome do paciente substituído por [PACIENTE]."
        )
    else:
        ia = "Modo demonstração: só regras locais, nenhum texto sai do servidor."
    if s.log_prompts:
        ia += " LOG_PROMPTS ligado: o texto enviado à IA (pseudonimizado) vai ao log; o expurgo não o alcança."
    cripto = (
        f"Texto da consulta cifrado no banco (AES-GCM, chave {crypto.current_key_id()})."
        if crypto.enabled()
        else "Sem criptografia de campo: defina DATA_ENCRYPTION_KEY."
    )
    return {
        "guardado": "Transcrição (só texto), insights e nota. O áudio nunca é gravado.",
        "retencao": retencao,
        "retencao_dias": days,
        "voz": voz,
        "ia": ia,
        "criptografia": cripto,
        "criptografia_ativa": crypto.enabled(),
        "stt_navegador": s.allow_browser_stt,
        "exclusao": "O titular pode pedir a exclusão (LGPD art. 18); o gestor executa e a ação fica na auditoria.",
    }
