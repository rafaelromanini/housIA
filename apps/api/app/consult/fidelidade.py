"""Fidelidade da tradução: números, unidades, doses e medicamentos têm de sobreviver à tradução.

Checagem determinística (sem LLM) entre a fala original e a tradução. Divergência vira alerta "tradução divergente":
trocar "500 mg" por "50 mg" ou perder um medicamento muda a conduta, então o profissional confere a fala original.
"""

import re
from functools import lru_cache

from app.consult.text import norm
from app.reference import drugs

UNIDADES = r"mg|mcg|ug|g|kg|ml|l|ui|u|meq|mmol|mmhg|bpm|%|graus|c|x|h"
NUMERO = re.compile(rf"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*({UNIDADES})?(?![a-z0-9])", re.IGNORECASE)


# números por extenso (pt, es, en) viram dígito para a comparação; "um/uno/one" ficam de fora (também são artigo)
_EXTENSO = {
    "dois": "2", "duas": "2", "two": "2", "tres": "3", "three": "3", "quatro": "4", "cuatro": "4", "four": "4",
    "cinco": "5", "five": "5", "seis": "6", "six": "6", "sete": "7", "siete": "7", "seven": "7", "oito": "8",
    "ocho": "8", "eight": "8", "nove": "9", "nueve": "9", "nine": "9", "dez": "10", "diez": "10", "ten": "10",
    "twice": "2", "vinte": "20", "veinte": "20", "twenty": "20", "trinta": "30", "treinta": "30", "thirty": "30",
    "quarenta": "40", "cuarenta": "40", "forty": "40", "cinquenta": "50", "cincuenta": "50", "fifty": "50",
    "quinhentos": "500", "quinientos": "500",
}  # fmt: skip
_EXTENSO_RE = re.compile(r"(?<!\w)(" + "|".join(_EXTENSO) + r")(?!\w)")
# "dos" é o 2 do espanhol, mas em português é "de os": só conta diante de unidade de tempo ou de dose
_DOS = re.compile(r"(?<!\w)dos(?= (dias|horas|semanas|meses|anos|veces|pastillas|comprimidos|tabletas|gotas|dosis))")


# "media pastilla", "meio comprimido", "half a tablet", "1/2 comprimido": metade de uma dose é 0,5 (só diante de palavra
# de dose: "en medio del pecho" / "no meio do peito" não é metade)
_DOSE = (
    r"(?:pastillas?|tabletas?|comprimidos?|pastilhas?|pilulas?|pills?|tablets?|capsulas?|dosis|doses?"
    r"|ampollas?|ampolas?)"
)
_METADE = re.compile(rf"(?<!\w)(?:media|medio|meia|meio|half(?: an?| of an?)?|1/2) (?={_DOSE}(?!\w))")
_QUARTO = re.compile(rf"(?<!\w)(?:1/4|un cuarto de|um quarto de|a quarter of an?|quarter) (?={_DOSE}(?!\w))")


def _valor(txt: str) -> str:
    # "1.000 mg" (pt/es) e "1,000 mg" (en) são mil, não um: separador de milhar = 1 a 3 dígitos (sem zero à frente) + 3
    if re.fullmatch(r"[1-9]\d{0,2}[.,]\d{3}", txt):
        return txt[:-4] + txt[-3:]
    v = txt.replace(",", ".")
    if "." in v:
        v = v.rstrip("0").rstrip(".")
    v = v.lstrip("0") or "0"
    return "0" + v if v.startswith(".") else v


def numeros(texto: str) -> set[tuple[str, str]]:
    """Pares (valor, unidade) da fala; unidade vazia quando o número vem solto."""
    t = _QUARTO.sub("0.25 ", _METADE.sub("0.5 ", norm(texto)))
    t = _EXTENSO_RE.sub(lambda m: _EXTENSO[m.group(1)], _DOS.sub("2", t))
    return {(_valor(m.group(1)), (m.group(2) or "").lower()) for m in NUMERO.finditer(t)}


# nomes em inglês (ou grafias do espanhol) que não são o radical do nome em português do drugs.csv
_ALIAS = {
    "warfarin": "varfarina", "warfarina": "varfarina", "acetaminophen": "paracetamol", "acetaminofen": "paracetamol",
    "aspirin": "aas", "aas": "aas", "dipyrone": "dipirona", "metamizol": "dipirona", "morphine": "morfina",
    "simvastatin": "sinvastatina", "levothyroxine": "levotiroxina", "dexamethasone": "dexametasona",
    "amoxicillin": "amoxicilina", "ampicillin": "ampicilina", "furosemide": "furosemida",
    "spironolactone": "espironolactona", "hydrochlorothiazide": "hidroclorotiazida", "atorvastatin": "atorvastatina",
    "rivaroxaban": "rivaroxabana", "apixaban": "apixabana", "enoxaparin": "enoxaparina", "gabapentin": "gabapentina",
}  # fmt: skip


@lru_cache
def _radicais() -> tuple[tuple[str, str], ...]:
    """(radical, princípio ativo): 'losartan' acha 'losartana' (pt) e 'losartán' (es); 'metformin' acha o inglês."""
    out = []
    for ing, d in drugs().items():
        for name in {norm(d.name.partition("+")[0]), norm(ing.partition("+")[0])}:
            stem = name.rstrip("aeo")
            if len(stem) >= 6:
                out.append((stem, ing))
    out += [(alias, ing) for alias, ing in _ALIAS.items() if ing in drugs()]
    return tuple(out)


def medicamentos(texto: str) -> set[str]:
    t = norm(texto)
    return {ing for stem, ing in _radicais() if re.search(rf"(?<!\w){re.escape(stem)}", t)}


# período/frequência: "por día" → "por semana" ou "de noche" → "de dia" muda a posologia sem mudar nenhum número
_PERIODOS = {
    "dia": r"dias?|days?|diari[oa]s?|diariamente|daily",
    "semana": r"semanas?|weeks?|semanal(?:mente)?|weekly",
    "mes": r"mes(?:es)?|months?|mensal(?:mente)?|mensual(?:mente)?|monthly",
    "hora": r"horas?|hours?|hourly",
    "noite": r"noche|noite|night|nocturn[oa]|noturn[oa]",
}
_PERIODOS_RE = {k: re.compile(rf"(?<!\w)(?:{v})(?!\w)") for k, v in _PERIODOS.items()}


def periodos(texto: str) -> set[str]:
    t = norm(texto)
    return {k for k, rx in _PERIODOS_RE.items() if rx.search(t)}


# negação: "No soy alérgica" → "Sou alérgica" inverte a alergia sem mudar número nem remédio.
# Só pt, es e en têm lista (em português "no" é "em o", por isso fica fora dela); outro idioma não é conferido.
_NEGACAO = {
    "pt": r"nao|nunca|jamais|sem|nenhum|nenhuma|nem|nada|tampouco",
    "es": r"no|nunca|jamas|sin|ningun|ninguno|ninguna|ni|nada|tampoco",
    "en": r"no|not|never|without|none|nor|neither|nothing",
}
_NEGACAO_RE = {k: re.compile(rf"(?<!\w)(?:{v})(?!\w)|n't(?!\w)") for k, v in _NEGACAO.items()}


def tem_negacao(texto: str, idioma: str) -> bool | None:
    """True/False se a fala nega algo; None quando o idioma não tem lista de negações."""
    rx = _NEGACAO_RE.get(idioma.split("-")[0].lower())
    return None if rx is None else bool(rx.search(norm(texto.replace("’", "'"))))


def divergencias(original: str, traducao: str, de: str | None = None, para: str | None = None) -> list[str]:
    """Descrições das diferenças entre a fala e a tradução (lista vazia: a tradução é fiel nesses pontos).

    `de` e `para` são os idiomas da fala e da tradução; sem eles (ou fora de pt/es/en) a negação não é conferida."""
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
    if de and para:
        na, nb = tem_negacao(original, de), tem_negacao(traducao, para)
        if na is not None and nb is not None and na != nb:
            out.append(
                "negação diferente: "
                + ("o original nega e a tradução afirma" if na else "o original afirma e a tradução nega")
            )
    pa, pb = periodos(original), periodos(traducao)
    if pa != pb:
        out.append("período ou frequência diferente (dia, semana, mês, hora, noite): " + ", ".join(sorted(pa ^ pb)))
    return out
