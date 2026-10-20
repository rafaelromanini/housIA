"""Smart App "Anamnese estruturada": migrado para o registro (#21), lê o que o engine já calculou."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="anamnese",
    nome="Anamnese estruturada",
    aba="raciocinio",
    ordem=20,
    recursos_fhir=["Condition", "AllergyIntolerance", "MedicationRequest"],
    saida=Saida(card="lista"),
    refs_obrigatorias=False,
    vazio="Anamnese ainda vazia.",
)


def run(ctx: AppContext) -> list[AppItem]:
    a = ctx.ins.anamnese
    if a is None:
        return []
    refs = a.refs or [ctx.patient_ref]
    secoes = [
        ("Queixa principal", [a.queixa_principal] if a.queixa_principal else []),
        ("História da doença atual", [a.hda] if a.hda else []),
        ("Antecedentes", a.antecedentes),
        ("Medicações em uso", a.medicacoes_em_uso),
        ("Alergias", a.alergias),
        ("Hábitos e contexto", a.habitos_e_contexto),
        ("Revisão de sistemas", a.revisao_de_sistemas),
    ]
    return [AppItem(texto=nome, detalhe="; ".join(linhas), refs=refs) for nome, linhas in secoes if linhas]
