"""CID-10 oficial na consulta: código que a tabela do DATASUS (data/reference/cid10.csv) não aceita não chega à nota.

Vale para as hipóteses (regras e IA), os CID da nota e os do relatório cirúrgico. O item recusado vai para
`descartados` com o motivo ("CID inexistente", "CID restrito ao sexo feminino", "CID de causa externa não é
diagnóstico"), do mesmo jeito que o item de IA sem fonte válida.
"""

from collections.abc import Callable

from app.reference import icd_entry, icd_problem, norm_icd


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
        if norm_icd(raw) in trusted:
            kept.append(it)
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
