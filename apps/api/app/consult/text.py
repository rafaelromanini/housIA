"""Utilitários de texto da transcrição: normalização, orações, negação e menção de medicamentos/alergias."""

import re

from app.reference import drugs
from app.timeline.record import normalize

NEGATION = re.compile(r"(?<!\w)(nao|nunca|nem|sem|nega|negou)(?!\w)")
CLAUSE_SPLIT = re.compile(r"[.,;:!?\n]| mas | porem ")

# nomes comerciais comuns -> princípio ativo (data/reference/drugs.csv)
BRANDS = {
    "advil": "ibuprofeno",
    "alivium": "ibuprofeno",
    "buscofem": "ibuprofeno",
    "novalgina": "dipirona",
    "anador": "dipirona",
    "tylenol": "paracetamol",
    "cataflam": "diclofenaco",
    "voltaren": "diclofenaco",
    "aspirina": "aas",
    "rivotril": "clonazepam",
    "glifage": "metformina",
    "daonil": "glibenclamida",
}
GENERIC_CLASSES = {"anti-inflamatorio": "aine", "antiinflamatorio": "aine", "anti inflamatorio": "aine"}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", normalize(text)).strip()


def clauses(text: str) -> list[str]:
    return [c.strip() for c in CLAUSE_SPLIT.split(f" {norm(text)} ") if c.strip()]


def contains(text: str, words: tuple[str, ...] | list[str]) -> str | None:
    """Primeira palavra-chave presente (texto já normalizado), ou None."""
    padded = f" {text} "
    for w in words:
        if w in padded:
            return w
    return None


def keyword_status(text: str, words: tuple[str, ...]) -> str | None:
    """'presente', 'negado' ou None para um grupo de palavras-chave numa fala."""
    found = None
    for clause in clauses(text):
        hit = contains(clause, words)
        if not hit:
            continue
        # a própria palavra-chave pode ter negação ("nao consigo urinar"): aí não é uma negativa
        negated = "nao" not in hit and bool(NEGATION.search(clause.replace(hit, " ")))
        if not negated:
            return "presente"
        found = "negado"
    return found


def mentioned_drugs(text: str) -> list[tuple[str, str]]:
    """(princípio ativo ou @classe, termo dito) para medicamentos citados na fala."""
    t = f" {norm(text)} "
    out: list[tuple[str, str]] = []
    catalog = drugs()
    for ing, d in catalog.items():
        name = norm(d.name)
        if len(name) >= 5 and f" {name}" in t:
            out.append((ing, d.name))
    for brand, ing in BRANDS.items():
        if f" {brand}" in t and ing in catalog and all(ing != o[0] for o in out):
            out.append((ing, brand.capitalize()))
    if not any(catalog[o[0]].drug_class == "aine" for o in out if o[0] in catalog):
        for term, cls in GENERIC_CLASSES.items():
            if term in t:
                out.append((f"@{cls}", "anti-inflamatório"))
                break
    return out
