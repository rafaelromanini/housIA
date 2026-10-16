"""Centro cirúrgico: relatório estruturado a partir do ditado do cirurgião.

Camada determinística: separa o ditado nos campos do relatório, aponta campos obrigatórios que faltaram (o que
costuma gerar glosa), concilia os materiais citados com os autorizados (catálogo OPME de DEMONSTRAÇÃO, códigos
fictícios) e cruza o antibiótico profilático com as alergias. A IA reescreve o texto final em linguagem técnica.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.consult import knowledge
from app.consult.schemas import CidSuggestion, Highlight, OpmeLine, ReportField, Segment, SurgicalReport, fala_ref
from app.consult.text import contains, mentioned_drugs, norm
from app.reference import drugs, matches

if TYPE_CHECKING:
    from app.consult.chart import ChartView

# campo, rótulo, palavras, obrigatório
FIELDS: list[tuple[str, str, tuple[str, ...], bool]] = [
    ("diagnostico_pre", "Diagnóstico pré-operatório", ("diagnostico pre",), True),
    ("diagnostico_pos", "Diagnóstico pós-operatório", ("diagnostico pos",), False),
    ("procedimento", "Procedimento realizado", ("procedimento realizado", "procedimento:"), True),
    (
        "anestesia",
        "Anestesia",
        ("anestesia geral", "raquianestesia", "raqui ", "peridural", "sedacao", "anestesia local", "bloqueio"),
        True,
    ),
    ("posicionamento", "Posicionamento", ("decubito", "litotomia", "posicao de"), False),
    (
        "antibioticoprofilaxia",
        "Antibioticoprofilaxia",
        ("antibioticoprofilaxia", "profilaxia antibiotica", "profilaxia com"),
        True,
    ),
    (
        "equipe",
        "Equipe (cirurgião, auxiliar, anestesista)",
        ("auxiliar", "anestesista", "instrumentador", "equipe", "cirurgiao:"),
        True,
    ),
    (
        "horario",
        "Horário de início e término",
        ("inicio da cirurgia", "termino", "duracao", "tempo cirurgico", "minutos de cirurgia", "iniciad"),
        True,
    ),
    ("achados", "Achados operatórios", ("achados",), True),
    (
        "tecnica",
        "Descrição da técnica",
        (
            "pneumoperitonio",
            "trocarte",
            "incisao",
            "seccionad",
            "ligad",
            "dissec",
            "anastomose",
            "retirad",
            "aspiracao",
        ),
        True,
    ),
    ("intercorrencias", "Intercorrências", ("intercorrenc",), True),
    ("sangramento", "Sangramento estimado", ("sangramento", "perda sanguinea"), True),
    ("contagem", "Contagem de compressas e instrumentais", ("contagem", "compressas", "gazes"), True),
    ("peca", "Peça para anatomopatológico", ("anatomopatolog", "peca enviada"), False),
    ("drenos", "Drenos", ("dreno",), False),
    ("fechamento", "Fechamento", ("fechamento", "sutura", "fio "), False),
    ("destino", "Destino do paciente", ("encaminhad", "recuperacao pos-anestesica", "rpa", "uti"), False),
]
NUMBERS = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;])\s+", text.strip()) if s.strip()]


GRANULAR = {"anestesia", "posicionamento", "antibioticoprofilaxia", "intercorrencias", "sangramento", "drenos"}


def _clauses(text: str) -> list[str]:
    return [c.strip().rstrip(".") + "." for c in re.split(r"[,.;]\s+|\.$", text.strip()) if c.strip()]


def _quantity_before(text: str, idx: int) -> int:
    tokens = text[:idx].split()[-2:]
    for tok in reversed(tokens):
        if tok.isdigit():
            return int(tok)
        if tok in NUMBERS:
            return NUMBERS[tok]
    return 1


def surgical_report(segments: list[Segment], chart: ChartView, authorized: list[dict]) -> SurgicalReport:
    fields: list[ReportField] = []
    for key, label, words, required in FIELDS:
        texts, refs = [], []
        granular = key in GRANULAR
        for s in segments:
            parts = _clauses(s.texto) if granular else _sentences(s.texto)
            picked = [part for part in parts if contains(norm(part), words)]
            if picked:
                texts += picked
                refs.append(fala_ref(s.n))
        fields.append(ReportField(campo=key, rotulo=label, texto=" ".join(texts), refs=refs, obrigatorio=required))

    pending: list[Highlight] = [
        Highlight(
            texto=f"Campo obrigatório não ditado: {f.rotulo}. Sem ele o relatório pode ser glosado ou devolvido.",
            severidade="atencao",
            refs=[],
        )
        for f in fields
        if f.obrigatorio and not f.texto
    ]

    cited: dict[str, tuple[int, list[str]]] = {}
    for s in segments:
        t = norm(s.texto)
        for d in knowledge.opme_catalog():
            for w in d.words:
                idx = t.find(w)
                if idx >= 0:
                    qty, refs = cited.get(d.code, (0, []))
                    cited[d.code] = (qty + _quantity_before(t, idx), [*refs, fala_ref(s.n)])
                    break
    auth = {a["codigo"]: int(a.get("quantidade", 1)) for a in authorized}
    catalog = {d.code: d for d in knowledge.opme_catalog()}
    lines: list[OpmeLine] = []
    for code in list(dict.fromkeys([*auth, *cited])):
        qty, refs = cited.get(code, (0, []))
        allowed = auth.get(code, 0)
        if allowed and qty == allowed:
            status = "conferido"
        elif not allowed:
            status = "nao_autorizado"
        elif qty == 0:
            status = "nao_citado"
        else:
            status = "quantidade_divergente"
        desc = catalog[code].description if code in catalog else code
        lines.append(OpmeLine(codigo=code, descricao=desc, autorizado=allowed, citado=qty, status=status, refs=refs))
        if status == "nao_autorizado":
            pending.append(
                Highlight(
                    texto=f"{desc} citado no ditado sem autorização na guia: risco de glosa — anexar justificativa "
                    "ou solicitar autorização complementar.",
                    severidade="critico",
                    refs=refs,
                )
            )
        elif status == "nao_citado":
            pending.append(
                Highlight(
                    texto=f"{desc} autorizado mas não citado no relatório: confirmar se foi usado.",
                    severidade="atencao",
                    refs=[],
                )
            )
        elif status == "quantidade_divergente":
            pending.append(
                Highlight(
                    texto=f"{desc}: {qty} citado(s) × {allowed} autorizado(s). Conferir a quantidade.",
                    severidade="atencao",
                    refs=refs,
                )
            )

    catalog_drugs = drugs()
    for s in segments:
        for ing, _term, status in mentioned_drugs(s.texto):
            d = catalog_drugs.get(ing)
            if d is None or status == "negado":  # "não foi usada cefazolina" não conflita com a alergia
                continue
            for a in chart.ctx.alergias:
                detail = a.detail or {}
                code = detail.get("codigo") or ""
                key = f"@{code}" if "classe" in (detail.get("sistema") or "") else code
                if key and matches(key, d.ingredient, d.drug_class):
                    pending.append(
                        Highlight(
                            texto=f"{d.name} citado no ditado e paciente com alergia registrada ({a.summary}).",
                            severidade="critico",
                            refs=[fala_ref(s.n), a.source_ref],
                        )
                    )

    cids = []
    diag = " ".join(f.texto for f in fields if f.campo.startswith("diagnostico"))
    for icd, text, c in chart.conditions:
        if text and (norm(text) in norm(diag) or norm(diag).find(norm(text).split(" ")[0]) >= 0):
            cids.append(CidSuggestion(codigo=icd, descricao=text, tipo="principal", refs=[f"Condition/{c['id']}"]))
            break
    pending.sort(key=lambda h: {"critico": 0, "atencao": 1, "info": 2}[h.severidade])
    return SurgicalReport(campos=fields, opme=lines, pendencias=pending, cids=cids)
