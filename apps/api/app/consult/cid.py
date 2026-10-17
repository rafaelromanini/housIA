"""CID-10 oficial na consulta: código que a tabela do DATASUS (data/reference/cid10.csv) não aceita não chega à nota.

Vale para as hipóteses (regras e IA), os CID da nota e os do relatório cirúrgico. O item recusado vai para
`descartados` com o motivo ("CID inexistente", "CID restrito ao sexo feminino", "CID de causa externa não é
diagnóstico"), do mesmo jeito que o item de IA sem fonte válida.
"""

from collections.abc import Callable

from app.consult.text import norm
from app.reference import cid10, icd_entry, icd_problem, norm_icd


def screen(
    items: list,
    where: str,
    sex: str = "",
    code: str = "codigo",
    label: str = "descricao",
    primary: Callable[[object], bool] | bool = False,
    trusted: set[str] | frozenset[str] = frozenset(),
) -> tuple[list, list[dict]]:
    """(itens aceitos, itens descartados): o código aceito é normalizado ("j069" → "J06.9").

    `trusted`: códigos que o próprio profissional registrou no prontuário (Condition). Eles passam mesmo quando a tabela
    do DATASUS (revisão 2008) não os tem, como N18.3, que as revisões seguintes da OMS acrescentaram: a regra recusa o
    que a IA propõe, não o que o profissional já registrou.
    """
    kept, dropped = [], []
    for it in items:
        raw = getattr(it, code)
        normalized = norm_icd(raw)
        if normalized and normalized in trusted:  # "" (código sem formato de CID) nunca é confiável
            kept.append(it.model_copy(update={code: normalized}) if normalized != raw else it)
            continue
        is_primary = primary(it) if callable(primary) else primary
        problem = icd_problem(raw, sex, is_primary)
        if problem:
            dropped.append(
                {"onde": where, "texto": getattr(it, label, ""), "refs_invalidas": [], "motivo": problem, "cid": raw}
            )
            continue
        entry = icd_entry(raw)
        kept.append(it.model_copy(update={code: entry.code}) if entry and entry.code != raw else it)
    return kept, dropped


def search(q: str, limit: int = 15, sex: str = "") -> list[dict]:
    """Autocomplete de CID-10 oficial (cid10.csv) por código ou por palavras da descrição, sem acento e sem ordem.
    Código exato e subcategorias do código digitado vêm primeiro; causa externa (V–Y) não entra como diagnóstico."""
    text = norm(q)
    if len(text) < 2:
        return []
    code = norm_icd(q)
    words = text.split()
    hits: list[tuple[int, str, object]] = []
    for entry in cid10().values():
        if entry.code[0] in "VWXY" or (entry.sex and sex in ("F", "M") and entry.sex != sex):
            continue
        desc = norm(entry.description)
        if code and entry.code == code:
            rank = 0
        elif code and entry.code.startswith(code):
            rank = 1
        elif entry.code.lower().startswith(text.replace(" ", "")):
            rank = 2
        elif all(w in desc for w in words):
            rank = 3 + (entry.kind != "subcategoria")
        else:
            continue
        hits.append((rank, entry.code, entry))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [{"codigo": e.code, "descricao": e.description, "tipo": e.kind} for _, _, e in hits[:limit]]  # type: ignore[attr-defined]
