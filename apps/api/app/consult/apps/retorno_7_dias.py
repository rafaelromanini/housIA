"""Smart App "Retorno em 7 dias pós-alta": a janela de revisão depois de uma internação recente."""

from datetime import timedelta

from app.consult.apps.base import AppContext, Gatilho, Manifest, Saida
from app.consult.chart import fmt_date
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="retorno_7_dias",
    nome="Retorno em 7 dias pós-alta",
    aba="transicao",
    ordem=40,
    gatilho=Gatilho(tipos=["consulta"], internado=False),
    recursos_fhir=["Encounter", "Appointment", "MedicationRequest"],
    saida=Saida(card="linha_do_tempo"),
)

JANELA_DIAS = 7
RECENTE_DIAS = 30  # depois disso a internação deixa de ser "recente"


def run(ctx: AppContext) -> list[AppItem] | None:
    r = ctx.record
    hosp = r.hospitalization
    end = r.admission_end
    if hosp is None or end is None:
        return None
    dias = (ctx.now - end).days
    if dias < 0 or dias > RECENTE_DIAS:
        return None
    ref = f"Encounter/{hosp['id']}"
    limite = end + timedelta(days=JANELA_DIAS)
    na_janela = dias <= JANELA_DIAS
    return [
        AppItem(texto="Alta da internação", quando=fmt_date(end), detalhe=r.admission_reason or "", refs=[ref]),
        AppItem(
            texto="Revisão pós-alta" + (" dentro da janela de 7 dias" if na_janela else " fora da janela de 7 dias"),
            quando=fmt_date(limite),
            detalhe=f"Alta há {dias} dia(s)."
            + (
                " Confira a reconciliação dos medicamentos e os sinais de alerta."
                if na_janela
                else " Registre o motivo do atraso."
            ),
            severidade="info" if na_janela else "atencao",
            refs=[ref],
        ),
        *(
            [AppItem(texto="Sem reconciliação medicamentosa registrada desde a alta", severidade="atencao", refs=[ref])]
            if not ctx.extra.get("medrec_runs")
            else []
        ),
    ]
