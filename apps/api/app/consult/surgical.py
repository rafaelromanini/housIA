"""Centro cirúrgico: relatório estruturado a partir do ditado do cirurgião e da equipe.

Camada determinística. O modelo do relatório vem de dados (data/reference/surgical_templates: base + regras da
especialidade + procedimento), escolhido pelo procedimento ditado ou pelo agendado (ServiceRequest/Appointment).
O ditado é separado nos campos do modelo e os obrigatórios que faltaram viram pendência com o motivo e a fonte (o
que costuma gerar glosa). Os materiais citados são conciliados com os autorizados (catálogo OPME de DEMONSTRAÇÃO,
códigos fictícios), o antibiótico é cruzado com as alergias, as verificações perioperatórias rodam
(app/consult/surgical_checks.py) e o exame de imagem de controle citado vira pedido proposto (ServiceRequest).
A IA só reescreve o texto final em linguagem técnica.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.consult import knowledge
from app.consult.schemas import (
    CidSuggestion,
    Highlight,
    OpmeLine,
    ProposedOrder,
    ReportField,
    ScheduledInfo,
    Segment,
    SurgicalReport,
    TemplateInfo,
    fala_ref,
)
from app.consult.surgical_checks import Scheduled, dictated_sides, negated, run_checks, scheduled_surgery, side_of
from app.consult.templates import ResolvedTemplate, first_hit, surgical_templates
from app.consult.text import mentioned_drugs, norm
from app.fhir_client import codes
from app.reference import drugs, matches

if TYPE_CHECKING:
    from app.consult.chart import ChartView

NUMBERS = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6}
PROCEDURE_WORDS = ("procedimento realizado", "procedimento:", "procedimento proposto", "cirurgia realizada")
ORDER_SYSTEM = "http://housia.dev/fhir/NamingSystem/pedido-proposto"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;])\s+", text.strip()) if s.strip()]


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


# ---------- modelo ----------
def choose_template(
    segments: list[Segment], scheduled: Scheduled | None, specialty: str
) -> tuple[ResolvedTemplate, str, list[str], str | None]:
    """(modelo, origem, refs, procedimento ditado). Ditado > agendamento > especialidade > base."""
    ts = surgical_templates()
    best = None
    for s in segments:
        for sentence in _sentences(s.texto):
            t = norm(sentence)
            hit = ts.detect(t)
            if hit:
                # a frase "procedimento realizado: ..." vence; depois o sinônimo mais específico; depois o primeiro
                score = (first_hit(t, PROCEDURE_WORDS) is not None, len(hit[1]), -s.n)
                if best is None or score > best[0]:
                    best = (score, hit[0].id, fala_ref(s.n))
    if best:
        return ts.resolve(best[1]), "ditado", [best[2]], best[1]
    if scheduled and scheduled.template_id:
        return ts.resolve(scheduled.template_id), "agendamento", scheduled.refs, None
    if specialty in ts.base.especialidades:
        return ts.resolve(None, specialty), "especialidade", [], None
    return ts.resolve(None, specialty), "base", [], None


def extract_fields(segments: list[Segment], tpl: ResolvedTemplate) -> list[ReportField]:
    absent: dict[str, list[str]] = {}  # grupo negado no ditado ("sem radioscopia") preenche os campos do grupo
    for group, (words, _) in tpl.grupos.items():
        for s in segments:
            if any(negated(norm(c), words) for c in _clauses(s.texto)):
                absent.setdefault(group, []).append(fala_ref(s.n))
    out: list[ReportField] = []
    for f in tpl.fields:
        texts, refs = [], []
        for s in segments:
            picked = []
            for part in _clauses(s.texto) if f.granular else _sentences(s.texto):
                t = norm(part)
                if not first_hit(t, f.words):
                    continue
                if f.campo == "lateralidade" and tpl.sitio and not first_hit(t, tpl.sitio):
                    continue  # o lado vale quando dito junto do sítio ("joelho direito")
                if f.valor and not f.valor.search(t):
                    continue
                picked.append(part)
            if picked:
                texts += picked
                refs.append(fala_ref(s.n))
        if not texts and f.grupo in absent:
            texts, refs = [tpl.grupos[f.grupo][1]], absent[f.grupo]
        out.append(
            ReportField(
                campo=f.campo,
                rotulo=f.rotulo,
                texto=" ".join(texts),
                refs=refs,
                obrigatorio=f.obrigatorio,
                motivo=f.motivo,
                fonte=f.fonte,
            )
        )
    return out


# ---------- pedido de imagem de controle (RIS) ----------
def control_orders(
    segments: list[Segment], tpl: ResolvedTemplate, chart: ChartView, scheduled: Scheduled | None
) -> list[ProposedOrder]:
    """Exame de controle citado no ditado → ServiceRequest proposto (vai ao prontuário só com o relatório aprovado)."""
    for s in segments:
        for clause in _clauses(s.texto):
            t = norm(clause)
            if not first_hit(t, tpl.exame_controle_words) or negated(t, tpl.exame_controle_words):
                continue
            sides = dictated_sides(segments, tpl.sitio) if tpl.sitio else []
            side = (sides[0][0] if sides else None) or (scheduled.lado if scheduled else None) or side_of(clause)
            desc = tpl.exame_controle
            if "{lado}" in desc:
                desc = (
                    desc.replace("{lado}", side)
                    if side
                    else re.sub(r"\s*\(?\{lado\}\)?", "", desc) + " (lado a confirmar)"
                )
            procedure = tpl.rotulo if tpl.id in surgical_templates().procedures else "procedimento cirúrgico"
            ref = fala_ref(s.n)
            resource: dict = {
                "resourceType": "ServiceRequest",
                "status": "draft",
                "intent": "proposal",
                "category": [
                    {
                        "coding": [
                            {"system": codes.SNOMED, "code": codes.SR_IMAGING[0], "display": codes.SR_IMAGING[1]}
                        ],
                        "text": "Exame de imagem",
                    }
                ],
                "code": {"text": desc},
                "subject": {"reference": f"Patient/{chart.r.id}"},
                "reasonCode": [{"text": f"Controle pós-operatório: {procedure}"}],
                "note": [
                    {
                        "text": f"Proposto pelo HousIA a partir do ditado do relatório cirúrgico ({ref}); vai ao "
                        "prontuário só depois da aprovação do cirurgião."
                    }
                ],
                "identifier": [{"system": ORDER_SYSTEM, "value": f"{tpl.id}-controle"}],
            }
            if chart.r.hospitalization:
                resource["encounter"] = {"reference": f"Encounter/{chart.r.hospitalization['id']}"}
            if scheduled and scheduled.sitio:
                resource["bodySite"] = [{"text": scheduled.sitio}]
            return [
                ProposedOrder(
                    descricao=desc,
                    motivo=f"Citado no ditado: “{clause.rstrip('.')}”.",
                    refs=[ref],
                    recurso=resource,
                )
            ]
    return []


# ---------- relatório ----------
def surgical_report(
    segments: list[Segment],
    chart: ChartView,
    authorized: list[dict],
    specialty: str = "cirurgia_geral",
    marks: dict | None = None,
) -> SurgicalReport:
    scheduled = scheduled_surgery(chart.r)
    tpl, origin, origin_refs, dictated = choose_template(segments, scheduled, specialty)
    fields = extract_fields(segments, tpl)

    pending: list[Highlight] = [
        Highlight(texto=f"Campo obrigatório não ditado: {f.rotulo}. {f.motivo}", severidade="atencao", refs=[])
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
        for ing, _term in mentioned_drugs(s.texto):
            d = catalog_drugs.get(ing)
            if d is None:
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

    checks, check_alerts = run_checks(segments, tpl, chart, scheduled, marks, dictated)
    pending += check_alerts

    cids = []
    diag = " ".join(f.texto for f in fields if f.campo.startswith("diagnostico"))
    for icd, text, c in chart.conditions:
        if text and (norm(text) in norm(diag) or norm(diag).find(norm(text).split(" ")[0]) >= 0):
            cids.append(CidSuggestion(codigo=icd, descricao=text, tipo="principal", refs=[f"Condition/{c['id']}"]))
            break
    pending.sort(key=lambda h: {"critico": 0, "atencao": 1, "info": 2}[h.severidade])
    spec = tpl.especialidade
    agendado = (
        ScheduledInfo(
            procedimento=scheduled.procedimento, sitio=scheduled.sitio, lado=scheduled.lado, refs=scheduled.refs
        )
        if scheduled
        else None
    )
    return SurgicalReport(
        campos=fields,
        opme=lines,
        pendencias=pending,
        cids=cids,
        modelo=TemplateInfo(
            id=tpl.id,
            rotulo=tpl.rotulo,
            especialidade=spec,
            especialidade_rotulo=knowledge.SPECIALTIES.get(spec, "Todas as especialidades"),
            versao=tpl.versao,
            versao_base=tpl.versao_base,
            origem=origin,
            refs=origin_refs,
        ),
        agendado=agendado,
        verificacoes=checks,
        pedidos=control_orders(segments, tpl, chart, scheduled),
        perioperatorio=chart.perioperative_checks(),
    )
