"""Smart App "Rastreios por idade e sexo": o que a idade e o sexo pedem e o que o prontuário mostra como feito."""

from app.consult.apps.base import AppContext, BaseCurada, Gatilho, Manifest, Saida, in_age, sev_rank
from app.consult.chart import fmt_date
from app.consult.schemas import AppItem
from app.consult.text import norm
from app.reference import _read_csv

MANIFEST = Manifest(
    id="rastreios",
    nome="Rastreios por idade e sexo",
    aba="prontuario",
    ordem=40,
    gatilho=Gatilho(tipos=["consulta"], idade_min=18),
    recursos_fhir=["Observation", "Procedure", "DiagnosticReport", "Condition"],
    base=BaseCurada(
        arquivo="smart_apps_rastreios.csv",
        versao="1.0.0",
        fonte="Diretrizes do INCA e sociedades médicas: base de demonstração, não exaustiva",
    ),
    saida=Saida(card="tabela", colunas=["Rastreio", "Situação", "Último registro", "Orientação"]),
    vazio="Nenhum rastreio se aplica a esta idade e sexo.",
)


def _last(ctx: AppContext, terms: list[str]):
    """Evento mais recente que prova o rastreio: exame (lab:), sinal vital (vital:) ou palavra no texto."""
    c = ctx.chart
    best = None
    for t in terms:
        if t.startswith("lab:"):
            events = c.labs.get(t[4:], [])
        elif t.startswith("vital:"):
            events = c.vitals.get(t[6:], [])
            if t[6:] == "pa":
                events = events or c.vitals.get("pas", [])
        else:
            events = [
                e
                for e in c.events
                if e.kind in ("procedure", "note", "lab") and t in norm(f"{e.summary} {e.text or ''}")
            ]
        for e in events:
            if best is None or e.timestamp > best.timestamp:
                best = e
    return best


def run(ctx: AppContext) -> list[AppItem]:
    r = ctx.record
    icd = [i for i, _, _ in ctx.chart.conditions]
    items = []
    for row in _read_csv("smart_apps_rastreios.csv"):
        if row["sexo"] and row["sexo"] != r.sex:
            continue
        if not in_age(r.age, row["idade_min"], row["idade_max"]):
            continue
        if row["exceto_cid"] and any(i.startswith(tuple(row["exceto_cid"].split("|"))) for i in icd):
            continue  # já tem o diagnóstico: o acompanhamento é do protocolo, não do rastreio
        last = _last(ctx, row["termos"].split("|"))
        months = int(row["intervalo_meses"])
        if last is None:
            status, sev, when, refs = "Sem registro", "info", "—", [ctx.patient_ref]
        elif (ctx.now - last.timestamp).days <= months * 30.4:
            status, sev, when, refs = "Em dia", "ok", fmt_date(last.timestamp) or "—", [last.source_ref]
        else:
            status, sev, when, refs = "Vencido", "atencao", fmt_date(last.timestamp) or "—", [last.source_ref]
        items.append(
            AppItem(
                texto=row["rastreio"],
                detalhe=f"Fonte: {row['fonte']}",
                severidade=sev,  # type: ignore[arg-type]
                refs=refs,
                celulas=[row["rastreio"], status, when, row["orientacao"]],
            )
        )
    items.sort(key=lambda i: sev_rank(i.severidade))
    return items
