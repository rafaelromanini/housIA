"""Aceitar, descartar, fixar ou marcar como feito cada insight da consulta (#3).

O motor recalcula tudo a cada fala; por isso a decisão do profissional fica em `Consultation.insight_actions_json` e é
reaplicada sobre cada análise. Cada item tem `item_key` = sha1(tipo + rótulo normalizado + refs ordenadas) e
`primeira_fala` (a análise em que apareceu, herdada da anterior). A decisão também casa pela chave estável
(tipo + rótulo): as refs de uma pergunta podem crescer com novas falas e o item descartado não pode voltar por isso.

Estrutura guardada: {"acoes": {item_key: {acao, fixado, motivo, por, em, tipo, rotulo, origem, sk}},
                     "alertas": {item_key: alert_id}}  (alertas críticos já enviados ao módulo de alertas)
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

from app.consult.schemas import ConsultNote, Frase, Insights, NoteSection
from app.consult.text import norm

ACTIONS = ("aceitar", "descartar", "feita", "nao_se_aplica", "fixar")
HIDING = ("descartar", "nao_se_aplica")
MIN_MOTIVO = 10  # como em alerts/router.py
NEW_FOR = 2  # o selo "novo" dura 2 falas

TYPE_LABEL = {
    "pergunta": "pergunta",
    "alerta": "alerta de segurança",
    "hipotese": "hipótese",
    "queixa": "queixa",
    "emocao": "emoção",
}
# listas do Insights por tipo de item: (tipo, caminho, campo do rótulo)
_LISTS = (
    ("pergunta", "perguntas"),
    ("alerta", "sugestoes_medicamentos"),
    ("alerta", "alertas"),
    ("alerta", "resumo.destaques"),
    ("hipotese", "hipoteses"),
    ("queixa", "queixas"),
    ("emocao", "emocoes"),
)


def label_of(tipo: str, item: Any) -> str:
    if tipo == "pergunta":
        return item.pergunta
    if tipo == "hipotese":
        return item.hipotese
    if tipo == "queixa":
        return f"{item.rotulo} {item.status}"
    if tipo == "emocao":
        return f"{item.rotulo} {item.quem}"
    return item.texto


def _list(ins: Insights, path: str) -> list:
    obj: Any = ins
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return []
    return obj


def _sha(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def stable_key(tipo: str, label: str) -> str:
    return _sha(tipo, norm(label))


def item_key(tipo: str, label: str, refs: list[str]) -> str:
    return _sha(tipo, norm(label), *sorted(refs))


def iter_items(ins: Insights) -> Iterator[tuple[str, str, Any]]:
    """(tipo, caminho da lista, item) de tudo que o profissional pode decidir."""
    for tipo, path in _LISTS:
        for item in _list(ins, path):
            yield tipo, path, item


def empty() -> dict:
    return {"acoes": {}, "alertas": {}}


def _previous_first(previous: dict | None) -> dict[str, int]:
    found: dict[str, int] = {}
    if not previous:
        return found
    for _, path in _LISTS:
        obj: Any = previous
        for part in path.split("."):
            obj = (obj or {}).get(part) if isinstance(obj, dict) else None
        for it in obj or []:
            if isinstance(it, dict) and it.get("item_key"):
                found.setdefault(it["item_key"], it.get("primeira_fala", 0))
    return found


def annotate(ins: Insights, previous: dict | None) -> None:
    """item_key e primeira_fala em cada item (a primeira vem de comparar com a análise anterior)."""
    before = _previous_first(previous)
    for tipo, _, item in iter_items(ins):
        item.item_key = item_key(tipo, label_of(tipo, item), item.refs)
        if item.item_key in before:
            item.primeira_fala = before[item.item_key]
        else:
            item.primeira_fala = ins.falas_analisadas if previous is not None else 0


def is_new(item: Any, falas: int) -> bool:
    return item.primeira_fala > 0 and falas - item.primeira_fala < NEW_FOR


def _entry(actions: dict, item: Any, tipo: str, label: str) -> dict | None:
    table = actions.get("acoes") or {}
    hit = table.get(item.item_key)
    if hit:
        return hit
    sk = stable_key(tipo, label)
    return next((e for e in table.values() if e.get("sk") == sk), None)


def apply(ins: Insights, actions: dict | None, previous: dict | None) -> Insights:
    """Anota as chaves e aplica as decisões: descartados saem, feitas viram abordadas, aceitos e fixados ficam."""
    annotate(ins, previous)
    table = (actions or {}).get("acoes") or {}
    hidden: set[str] = set()
    done: list = []
    for tipo, path, item in list(iter_items(ins)):
        entry = _entry(actions or {}, item, tipo, label_of(tipo, item)) if table else None
        if not entry:
            continue
        item.fixado = bool(entry.get("fixado"))
        act = entry.get("acao") or ""
        if act in HIDING:
            _list(ins, path).remove(item)
            hidden.add(item.item_key)
        elif act == "feita" and tipo == "pergunta":
            ins.perguntas.remove(item)
            done.append(item)
        elif act in ("aceitar", "feita"):
            item.acao = act
    for q in done:
        q.acao = "feita"
        if not any(a.item_key == q.item_key for a in ins.perguntas_abordadas):
            ins.perguntas_abordadas.append(q)
    for _, path in _LISTS:
        items = _list(ins, path)
        items.sort(key=lambda i: not i.fixado)  # estável: os fixados vão para o topo
    ins.ocultos = len(hidden)
    return ins


def find(ins: Insights, key: str) -> tuple[str, Any] | None:
    for tipo, _, item in iter_items(ins):
        if item.item_key == key:
            return tipo, item
    return None


def record(actions: dict | None, tipo: str, item: Any, acao: str, motivo: str, who: str, when: str, label: str) -> dict:
    """Nova estrutura de ações com a decisão do profissional (não altera a entrada)."""
    data = {"acoes": dict((actions or {}).get("acoes") or {}), "alertas": dict((actions or {}).get("alertas") or {})}
    entry = dict(data["acoes"].get(item.item_key) or {})
    entry |= {
        "tipo": tipo,
        "rotulo": label[:300],
        "origem": getattr(item, "origem", "regra"),
        "sk": stable_key(tipo, label),
    }
    if acao == "fixar":
        entry["fixado"] = not entry.get("fixado", False)
    else:
        entry |= {"acao": acao, "motivo": motivo.strip(), "por": who, "em": when}
    data["acoes"][item.item_key] = entry
    return data


# ---------- aceite por camada (regra × IA) ----------
def acceptance(actions: dict | None) -> dict:
    """{tipo: {camada: {decididas, aceitas, descartadas}}} para o painel Impacto."""
    out: dict[str, dict[str, dict[str, int]]] = {}
    for e in ((actions or {}).get("acoes") or {}).values():
        act = e.get("acao")
        if not act:
            continue
        row = out.setdefault(e["tipo"], {}).setdefault(
            e.get("origem", "regra"), {"decididas": 0, "aceitas": 0, "descartadas": 0}
        )
        row["decididas"] += 1
        row["aceitas"] += act in ("aceitar", "feita")
        row["descartadas"] += act in HIDING
    return out


# ---------- nota ----------
def _append(section: NoteSection, line: str, refs: list[str]) -> None:
    if line in section.texto:
        return
    text = section.texto.strip()
    empty_text = not text or text.lower().startswith("sem registro")
    section.texto = line if empty_text else f"{text.rstrip('.;')}; {line}"
    section.refs = list(dict.fromkeys([*section.refs, *refs]))
    if refs:  # o item aceito pelo profissional entra com a fonte e a origem "regra"
        section.frases = [*section.frases, Frase(texto=line, refs=list(dict.fromkeys(refs)), origem="regra")]


def accepted_alerts(ins: Insights) -> list:
    seen: dict[str, Any] = {}
    for tipo, _, item in iter_items(ins):
        if tipo == "alerta" and item.acao == "aceitar":
            seen.setdefault(item.item_key, item)
    return list(seen.values())


def inject_into_note(note: ConsultNote, ins: Insights) -> None:
    """Itens aceitos entram na seção certa da nota, com as refs: alerta no Plano, pergunta feita na Anamnese (HDA),
    hipótese na Avaliação. Idempotente, e vale para a nota do LLM tanto quanto para a do mock."""
    for a in accepted_alerts(ins):
        _append(note.plano, f"{a.texto} (aceito pelo profissional)", a.refs[:6])
    for q in ins.perguntas_abordadas:
        if q.acao == "feita":
            _append(note.hda, f"Pergunta feita pelo profissional: {q.pergunta}", q.refs[:4])
    for h in ins.hipoteses:
        if h.acao == "aceitar":
            _append(note.avaliacao, f"Hipótese aceita pelo profissional: {h.hipotese} ({h.cid})", h.refs[:4])
