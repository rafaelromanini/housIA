"""Fidelidade da tradução: números, unidades, doses e medicamentos têm de sobreviver à tradução.

Checagem determinística (sem LLM) entre a fala original e a tradução. Divergência vira alerta "tradução divergente":
trocar "500 mg" por "50 mg" ou perder um medicamento muda a conduta, então o profissional confere a fala original.
"""

import re
from functools import lru_cache

from app.consult.text import norm
from app.reference import drugs

UNIDADES = r"mg|mcg|ug|g|kg|ml|l|ui|u|meq|mmol|mmhg|bpm|%|graus|c|x"
NUMERO = re.compile(rf"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*({UNIDADES})?(?![a-z0-9])", re.IGNORECASE)


# números por extenso (pt, es, en) viram dígito para a comparação; "um/uno/one" ficam de fora (também são artigo)
_EXTENSO = {
    "dois": "2", "duas": "2", "two": "2", "tres": "3", "three": "3", "quatro": "4", "cuatro": "4", "four": "4",
    "cinco": "5", "five": "5", "seis": "6", "six": "6", "sete": "7", "siete": "7", "seven": "7", "oito": "8",
    "ocho": "8", "eight": "8", "nove": "9", "nueve": "9", "nine": "9", "dez": "10", "diez": "10", "ten": "10",
}  # fmt: skip
_EXTENSO_RE = re.compile(r"(?<!\w)(" + "|".join(_EXTENSO) + r")(?!\w)")
# "dos" é o 2 do espanhol, mas em português é "de os": só conta diante de unidade de tempo ou de dose
_DOS = re.compile(r"(?<!\w)dos(?= (dias|horas|semanas|meses|anos|veces|pastillas|comprimidos|tabletas|gotas|dosis))")


def _valor(txt: str) -> str:
    v = txt.replace(",", ".")
    if "." in v:
        v = v.rstrip("0").rstrip(".")
    return v.lstrip("0") or "0"


def numeros(texto: str) -> set[tuple[str, str]]:
    """Pares (valor, unidade) da fala; unidade vazia quando o número vem solto."""
    t = _EXTENSO_RE.sub(lambda m: _EXTENSO[m.group(1)], _DOS.sub("2", norm(texto)))
    return {(_valor(m.group(1)), (m.group(2) or "").lower()) for m in NUMERO.finditer(t)}


@lru_cache
def _radicais() -> tuple[tuple[str, str], ...]:
    """(radical, princípio ativo): 'losartan' acha 'losartana' (pt) e 'losartán' (es); 'metformin' acha o inglês."""
    out = []
    for ing, d in drugs().items():
        for name in {norm(d.name.partition("+")[0]), norm(ing.partition("+")[0])}:
            stem = name.rstrip("aeo")
            if len(stem) >= 6:
                out.append((stem, ing))
    return tuple(out)


def medicamentos(texto: str) -> set[str]:
    t = norm(texto)
    return {ing for stem, ing in _radicais() if re.search(rf"(?<!\w){re.escape(stem)}", t)}


def divergencias(original: str, traducao: str) -> list[str]:
    """Descrições das diferenças entre a fala e a tradução (lista vazia: a tradução é fiel nesses pontos)."""
    out: list[str] = []
    a, b = numeros(original), numeros(traducao)
    va, vb = {v for v, _ in a}, {v for v, _ in b}
    if va - vb:
        out.append("números do original ausentes na tradução: " + ", ".join(sorted(va - vb)))
    if vb - va:
        out.append("números na tradução que não estão no original: " + ", ".join(sorted(vb - va)))
    # mesmo valor com unidade diferente ("500 mg" → "500 mcg"); a unidade omitida na tradução não conta
    ua = {v: u for v, u in a if u}
    for v, u in b:
        if u and v in ua and ua[v] != u:
            out.append(f"unidade trocada em {v}: {ua[v]} → {u}")
    da, db = medicamentos(original), medicamentos(traducao)
    if da != db:
        out.append("medicamentos diferentes entre original e tradução: " + ", ".join(sorted(da ^ db)))
    return out
