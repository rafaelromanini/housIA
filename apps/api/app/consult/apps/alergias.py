"""Smart App "Alergias": migrado para o registro (#21); lê o que o engine já calculou, sem alterar as regras."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="alergias",
    nome="Alergias",
    aba="medicamentos",
    ordem=20,
    recursos_fhir=["AllergyIntolerance"],
    saida=Saida(card="lista"),
    refs_obrigatorias=False,
    vazio="Nenhuma alergia registrada.",
)


def run(ctx: AppContext) -> list[AppItem]:
    return [
        AppItem(
            texto=a.alergeno,
            detalhe=a.reacao,
            severidade="critico" if a.criticidade == "alta" else "atencao",
            refs=a.refs,
            origem=a.origem,
        )
        for a in ctx.ins.alergias
    ]
