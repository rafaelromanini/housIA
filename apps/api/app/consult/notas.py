"""Frases com fonte na nota (#15): cada frase de uma seção leva as refs e quem a escreveu (ia, regra ou profissional).

O texto da seção continua sendo o que o profissional lê e edita; `frases` é a proveniência desse texto. Na revisão o
servidor reconcilia as duas coisas: a frase que o profissional deixou como estava mantém refs e origem, a que ele
escreveu ou alterou passa a origem "profissional" (herdando as refs da frase de que ela se parece).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

SECTION_KEYS = (
    "queixa_principal",
    "hda",
    "antecedentes",
    "medicacoes",
    "alergias",
    "exame_fisico",
    "exames_complementares",
    "avaliacao",
    "plano",
)
PLACEHOLDER = "sem registro na consulta"
MIN_MATCH = 12  # frase mais curta que isso só é reconhecida quando sobra sozinha
SIMILAR = 0.6  # parecença mínima para a frase editada herdar as refs da original
_SEPARATORS = " \t\r\n;.,"
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ“\"(\d])|\s*;\s+|\n+")


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(_SEPARATORS).lower()


def is_placeholder(text: str) -> bool:
    return _key(text).startswith(PLACEHOLDER)


def split_sentences(text: str) -> list[str]:
    return [p.strip() for p in _SPLIT.split(text or "") if p and p.strip(_SEPARATORS)]


def frase(texto: str, refs: list[str], origem: str) -> dict:
    return {"texto": texto.strip(), "refs": list(dict.fromkeys(refs)), "origem": origem}


def derive(text: str, refs: list[str], origem: str = "regra") -> list[dict]:
    """Frases de um texto que veio sem elas (nota antiga, LLM que não as devolveu): cada uma leva as refs da seção.
    Seção sem fonte ou o "Sem registro na consulta" não geram frase: não há o que citar."""
    if not refs or is_placeholder(text):
        return []
    return [frase(s, refs, origem) for s in split_sentences(text)]


def _best_match(piece: str, pool: list[dict], threshold: float = SIMILAR) -> dict | None:
    best, score = None, threshold
    for f in pool:
        ratio = SequenceMatcher(None, _key(piece), _key(f["texto"]), autojunk=False).ratio()
        if ratio >= score:
            best, score = f, ratio
    return best


def _best_refs(piece: str, pool: list[dict]) -> list[str]:
    best = _best_match(piece, pool)
    return list(best["refs"]) if best else []


def reconcile(previous: list[dict], texto: str) -> list[dict]:
    """Frases do texto editado: as que o profissional não tocou ficam como estavam; o resto passa a "profissional"."""
    claimed: list[tuple[int, int, dict]] = []
    unused = list(previous)
    lowered = texto.lower()
    for f in list(previous):
        needle = f["texto"].strip().lower()
        if len(needle) < MIN_MATCH:
            continue
        start = lowered.find(needle)
        while start != -1 and any(s < start + len(needle) and start < e for s, e, _ in claimed):
            start = lowered.find(needle, start + 1)
        if start != -1:
            claimed.append((start, start + len(needle), f))
            unused.remove(f)
    claimed.sort(key=lambda c: c[0])
    out: list[dict] = []
    cursor = 0
    for start, end, f in [*claimed, (len(texto), len(texto), {})]:
        gap = texto[cursor:start]
        pieces = split_sentences(gap)
        whole = gap.strip(_SEPARATORS)
        similar = _best_match(whole, unused) if len(pieces) > 1 else None
        if similar is not None:  # uma frase só, editada por dentro (o ponto no meio não a divide em duas)
            unused.remove(similar)
            out.append(frase(whole, similar["refs"], "profissional"))
            pieces = []
        for piece in pieces:
            hit = next((u for u in unused if _key(u["texto"]) == _key(piece)), None)
            if hit is not None:
                unused.remove(hit)
                out.append(dict(hit))
            else:
                out.append(frase(piece, _best_refs(piece, unused), "profissional"))
        if f:
            out.append(dict(f))
        cursor = end
    return out


def reconcile_section(previous: dict | None, edited: dict) -> dict:
    """Seção editada: texto do profissional, frases reconciliadas e refs da seção = fontes que ainda são citadas."""
    prev = previous or {}
    texto = edited.get("texto", "")
    frases = reconcile(prev.get("frases") or derive(prev.get("texto", ""), prev.get("refs", [])), texto)
    allowed = set(prev.get("refs", [])) | {r for f in frases for r in f["refs"]}
    refs = [r for r in edited.get("refs", []) if r in allowed]
    refs += [r for f in frases for r in f["refs"] if r not in refs]
    return {"texto": texto, "refs": refs, "frases": frases}


def append_text(section: dict, texto: str, refs: list[str], origem: str, joiner: str = "\n") -> dict:
    """Acrescenta uma frase ao fim da seção (texto, refs e proveniência juntos). Idempotente para o mesmo texto."""
    sec = dict(section or {"texto": "", "refs": []})
    base = sec.get("texto", "")
    frases = list(sec.get("frases") or derive(base, sec.get("refs", [])))
    if not any(_key(f["texto"]) == _key(texto) for f in frases):
        frases.append(frase(texto, refs, origem))
    sec["texto"] = (base + joiner if base and not is_placeholder(base) else "") + texto
    sec["refs"] = list(dict.fromkeys([*sec.get("refs", []), *refs]))
    sec["frases"] = frases
    return sec


def ensure(note: dict) -> dict:
    """Garante `frases` em toda seção que tem texto e fonte (nota antiga, edição do copiloto, mock)."""
    out = dict(note)
    for key in SECTION_KEYS:
        sec = out.get(key)
        if isinstance(sec, dict) and not sec.get("frases"):
            out[key] = sec | {"frases": derive(sec.get("texto", ""), sec.get("refs", []))}
    return out


def unsourced(note: dict) -> list[dict]:
    """Frases sem fonte e que não são do profissional (a nota não deve ter nenhuma)."""
    bad = []
    for key in SECTION_KEYS:
        for f in (note.get(key) or {}).get("frases", []):
            if not f.get("refs") and f.get("origem") != "profissional":
                bad.append({"secao": key, "texto": f["texto"]})
    return bad
