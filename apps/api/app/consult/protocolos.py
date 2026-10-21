"""Protocolos de APS vivos na conversa (#28): o que o profissional ou o paciente disse muda o status do item.

Determinístico: lê as falas pelas mesmas regras de negação das queixas (`keyword_status`). Um item tratado na consulta
vira "abordado — Fala/N"; gatilho crítico (ideação suicida, item 9 do PHQ-9) vira alerta crítico.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.consult import knowledge
from app.consult.schemas import Segment, fala_ref
from app.consult.text import NEG_WORDS, clauses, contains, find, keyword_status, norm

SEM_NEGACAO = (
    "!"  # prefixo de um termo que já carrega a negação ("não quero mais viver"): não se aplica a regra de negação
)
TRIMESTRE = re.compile(r"(\d{1,2}) semanas?")
GESTACAO = ("gestante", "gravida", "gestacao", "gravidez", "pre-natal", "prenatal", "barriga")


def falas_com(segments: Iterable[Segment], words: Iterable[str]) -> list[Segment]:
    """Falas que citam algum dos termos sem negar e sem ser pergunta ("o senhor examinou os pés?" não trata o item)."""
    plain = tuple(w for w in words if not w.startswith(SEM_NEGACAO))
    raw = tuple(w[len(SEM_NEGACAO) :] for w in words if w.startswith(SEM_NEGACAO))
    out = []
    for s in segments:
        if s.texto.strip().endswith("?"):
            continue
        if (plain and keyword_status(s.texto, plain, skip_questions=True) == "presente") or (
            raw and contains(norm(s.texto), raw)
        ):
            out.append(s)
    return out


def _negado_antes(clause: str, start: int) -> bool:
    """Negação nas 5 palavras que antecedem o termo ("nunca pensei em me matar"): para o risco, a janela é maior."""
    return any(w in NEG_WORDS for w in clause[:start].split()[-5:])


def falas_de_risco(segments: Iterable[Segment], words: Iterable[str]) -> list[Segment]:
    """Como `falas_com`, mas com a negação mais larga: "nunca pensei em me matar" não é ideação."""
    plain = tuple(w for w in words if not w.startswith(SEM_NEGACAO))
    raw = tuple(w[len(SEM_NEGACAO) :] for w in words if w.startswith(SEM_NEGACAO))
    out = []
    for s in falas_com(segments, words):
        text = norm(s.texto)
        if raw and contains(text, raw):
            out.append(s)
            continue
        if any((hit := find(c, plain)) and not _negado_antes(c, hit[1]) for c in clauses(s.texto)):
            out.append(s)
    return out


def refs_das_falas(falas: list[Segment], limite: int = 3) -> list[str]:
    return [fala_ref(s.n) for s in falas[:limite]]


def trimestre_pela_fala(segments: Iterable[Segment]) -> int | None:
    """Trimestre da gestação pela idade gestacional dita ("de 30 semanas"), na fala que cita a gestação."""
    for s in segments:
        text = norm(s.texto)
        m = TRIMESTRE.search(text)
        if m and contains(text, GESTACAO + ("ig ", "idade gestacional")):
            weeks = int(m.group(1))
            if 1 <= weeks <= 42:
                return 1 if weeks <= 13 else 2 if weeks <= 27 else 3
    return None


def meses_de_vida(birth: str | None, today) -> int | None:
    if not birth:
        return None
    y, m, d = (int(x) for x in birth[:10].split("-"))
    months = (today.year - y) * 12 + today.month - m - (1 if today.day < d else 0)
    return max(months, 0)


def criticos(segments: Iterable[Segment]) -> dict[str, list[Segment]]:
    """Itens críticos (por id) cujo gatilho foi dito na consulta."""
    out = {}
    for d in knowledge.protocols():
        if d.critical and d.critical_triggers:
            hits = falas_de_risco(list(segments), d.critical_triggers)
            if hits:
                out[d.id] = hits
    return out
