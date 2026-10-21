"""Smart App "Antes de encaminhar" (#29): lê o que o engine calculou (app/consult/encaminhamento.py)."""

from app.consult.apps.base import AppContext, BaseCurada, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="encaminhamento",
    nome="Antes de encaminhar",
    aba="raciocinio",
    ordem=40,
    recursos_fhir=["Condition", "Observation", "ServiceRequest"],
    base=BaseCurada(
        arquivo="referral_criteria.csv",
        versao="1.0.0",
        fonte="Protocolos de encaminhamento do Ministério da Saúde e do TelessaúdeRS (resumo de demonstração)",
    ),
    saida=Saida(card="lista"),
    refs_obrigatorias=False,
    vazio="Nenhum encaminhamento em discussão: o card abre quando o profissional diz que vai encaminhar.",
)


def run(ctx: AppContext) -> list[AppItem]:
    return [
        AppItem(
            texto=f"Encaminhar para {e.especialidade}: {e.condicao}",
            detalhe=e.recomendacao,
            severidade="atencao" if e.exames_faltantes else "info",
            refs=e.refs,
        )
        for e in ctx.ins.encaminhamentos
    ]
