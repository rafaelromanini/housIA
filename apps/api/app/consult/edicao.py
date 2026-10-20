"""Quanto da nota gerada o profissional mudou na revisão (métrica do painel Impacto, #36).

Compara o texto da nota original (a que saiu da IA, guardada em `extra_json["nota_original"]`) com a nota revisada,
palavra a palavra. Só conta texto escrito: refs, origem, fonte e a proveniência do copiloto ficam de fora.
"""

import re
from difflib import SequenceMatcher

IGNORED_KEYS = {
    "refs",
    "origem",
    "fonte",
    "copiloto",
    "condicoes_propostas",
    "prontidao",
    "id",
    "modelo",
    "frases",  # as frases repetem o texto da seção: contar as duas contaria cada palavra duas vezes
}


def _strings(obj, out: list[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key not in IGNORED_KEYS and not (key == "tipo" and "quando" in obj):  # tipo do evento da cronologia
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


# ---------- diff por seção (revisão segura, #14) ----------
SECTION_LABELS = {
    "queixa_principal": "Queixa principal",
    "hda": "História da doença atual",
    "antecedentes": "Antecedentes",
    "medicacoes": "Medicações em uso",
    "alergias": "Alergias",
    "exame_fisico": "Exame físico",
    "exames_complementares": "Exames complementares",
    "avaliacao": "Avaliação",
    "plano": "Plano",
    "cids": "CID-10",
    "cronologia": "Resumo cronológico",
    "pendencias": "Pendências",
    "orientacoes_paciente": "Orientações ao paciente",
    "opme": "Materiais/OPME",
}


def section_texts(note: dict | None) -> dict[str, tuple[str, str]]:
    """{chave da seção: (rótulo, texto)} do que o profissional lê e pode editar, na ordem da nota."""
    note = note or {}
    out: dict[str, tuple[str, str]] = {}
    if "relatorio_cirurgico" in note:
        report = note.get("relatorio_cirurgico") or {}
        for f in report.get("campos", []):
            out[f"campo:{f['campo']}"] = (f.get("rotulo") or f["campo"], f.get("texto") or "")
        out["opme"] = (
            SECTION_LABELS["opme"],
            "\n".join(f"{o.get('descricao', '')} ×{o.get('citado', 0)}" for o in report.get("opme", [])),
        )
        out["cids"] = (
            SECTION_LABELS["cids"],
            "\n".join(
                f"{c.get('codigo', '')} {c.get('descricao', '')} ({c.get('tipo', '')})" for c in report.get("cids", [])
            ),
        )
        out["pendencias"] = (
            SECTION_LABELS["pendencias"],
            "\n".join(p.get("texto", "") for p in report.get("pendencias", [])),
        )
        return out
    for key in list(SECTION_LABELS)[:9]:
        out[key] = (SECTION_LABELS[key], (note.get(key) or {}).get("texto", ""))
    out["cids"] = (
        SECTION_LABELS["cids"],
        "\n".join(
            f"{c.get('codigo', '')} {c.get('descricao', '')} ({c.get('tipo', '')})" for c in note.get("cids", [])
        ),
    )
    out["cronologia"] = (
        SECTION_LABELS["cronologia"],
        "\n".join(f"{c.get('quando', '')} {c.get('texto', '')}" for c in note.get("cronologia", [])),
    )
    out["pendencias"] = (
        SECTION_LABELS["pendencias"],
        "\n".join(p.get("texto", "") for p in note.get("pendencias", [])),
    )
    out["orientacoes_paciente"] = (
        SECTION_LABELS["orientacoes_paciente"],
        "\n".join(note.get("orientacoes_paciente", [])),
    )
    return out


def _word_diff(a: list[str], b: list[str]) -> tuple[float, int, list[dict]]:
    changed = 0
    ops: list[dict] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            changed += max(i2 - i1, j2 - j1)
        ops.append({"op": tag, "antes": " ".join(a[i1:i2]), "depois": " ".join(b[j1:j2])})
    base = max(len(a), len(b))
    return (round(100 * changed / base, 1) if base else 0.0), changed, ops


def section_diff(original: dict | None, edited: dict | None, with_text: bool = True) -> list[dict]:
    """Por seção: % editado (palavras trocadas, acrescentadas ou removidas sobre o tamanho da seção) e, se pedido, o
    texto
    antes/depois e as operações do diff. Com `with_text=False` sai só número (é o que vai ao audit)."""
    before, after = section_texts(original), section_texts(edited)
    rows = []
    for key in {**after, **before}:
        label = (after.get(key) or before[key])[0]
        a_text, b_text = (before.get(key) or ("", ""))[1], (after.get(key) or ("", ""))[1]
        pct, changed, ops = _word_diff(re.findall(r"\w+", a_text.lower()), re.findall(r"\w+", b_text.lower()))
        row = {"secao": key, "rotulo": label, "pct_editado": pct, "palavras_alteradas": changed}
        if with_text:
            row |= {"antes": a_text, "depois": b_text, "operacoes": [o for o in ops if o["op"] != "equal"]}
        rows.append(row)
    return rows
