"""Smart App "Exames": migrado para o registro (#21); lê o que o engine já calculou, sem alterar as regras."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="exames",
    nome="Exames",
    aba="prontuario",
    ordem=20,
    recursos_fhir=["Observation", "DiagnosticReport"],
    saida=Saida(card="tabela", colunas=["Exame", "Valor", "Data", "Situação"]),
    refs_obrigatorias=False,
    vazio="Nenhum exame recente.",
)


def run(ctx: AppContext) -> list[AppItem]:
    sev = {"normal": "ok", "baixo": "atencao", "alto": "atencao"}
    return [
        AppItem(
            texto=e.exame,
            celulas=[e.exame, e.valor, e.data, e.status],
            severidade=sev[e.status],
            refs=e.refs,
            origem=e.origem,
        )
        for e in ctx.ins.exames
    ]
