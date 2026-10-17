"""Quanto da nota gerada o profissional mudou na revisão (métrica do painel Impacto, #36).

Compara o texto da nota original (a que saiu da IA, guardada em `extra_json["nota_original"]`) com a nota revisada,
palavra a palavra. Só conta texto escrito: refs, origem, fonte e a proveniência do copiloto ficam de fora.
"""

import re
from difflib import SequenceMatcher

IGNORED_KEYS = {"refs", "origem", "fonte", "copiloto", "condicoes_propostas", "prontidao", "id", "modelo"}


def _strings(obj, out: list[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key not in IGNORED_KEYS:
                _strings(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _strings(value, out)
    elif isinstance(obj, str):
        out.append(obj)


def words(note: dict | None) -> list[str]:
    parts: list[str] = []
    _strings(note or {}, parts)
    return re.findall(r"\w+", " ".join(parts).lower())


def edit_percent(original: dict | None, edited: dict | None) -> tuple[float, int]:
    """(% editado, palavras alteradas): palavras trocadas, acrescentadas ou removidas sobre o tamanho da nota."""
    a, b = words(original), words(edited)
    if not a and not b:
        return 0.0, 0
    changed = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            changed += max(i2 - i1, j2 - j1)
    return round(100 * changed / max(len(a), len(b)), 1), changed
