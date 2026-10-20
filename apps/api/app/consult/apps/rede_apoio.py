"""Smart App "Rede de apoio e risco social": fatores sociais do prontuário × social_weights.json."""

from types import SimpleNamespace

from app.consult.apps.base import AppContext, BaseCurada, Manifest, Saida
from app.consult.schemas import AppItem
from app.discharge.scoring import prefill, social_band, social_score

MANIFEST = Manifest(
    id="rede_apoio",
    nome="Rede de apoio e risco social",
    aba="transicao",
    ordem=30,
    recursos_fhir=["RelatedPerson", "Observation"],
    base=BaseCurada(
        arquivo="social_weights.json", versao="1.0.0", fonte="Pesos provisórios de demonstração (não validados)"
    ),
    saida=Saida(card="lista"),
    vazio="O prontuário não tem dados sociais para estimar o risco.",
)

BAND = {"alto": "critico", "moderado": "atencao", "baixo": "ok"}


def run(ctx: AppContext) -> list[AppItem] | None:
    pre = prefill(ctx.record)
    known = {k: v for k, v in pre["valores"].items() if v is not None}
    if not known:
        return []
    score, factors = social_score(SimpleNamespace(model_dump=lambda: known))  # type: ignore[arg-type]  # só as respostas conhecidas
    band = social_band(score)
    items = [
        AppItem(
            texto=f"Risco social {band} ({score} pontos)",
            detalhe="Pesos de demonstração: o checklist da alta confirma cada resposta.",
            severidade=BAND[band],  # type: ignore[arg-type]
            refs=[pre["fontes"][f["campo"]]["ref"] or ctx.patient_ref for f in factors][:1] or [ctx.patient_ref],
        )
    ]
    for f in factors:
        src = pre["fontes"].get(f["campo"], {})
        items.append(
            AppItem(
                texto=f["rotulo"],
                detalhe=f"{src.get('explicacao', '')} · peso {f['peso']}".strip(" ·"),
                severidade="atencao",
                refs=[src.get("ref") or ctx.patient_ref],
            )
        )
    if not factors:
        items.append(AppItem(texto="Sem fator social de risco registrado", severidade="ok", refs=[ctx.patient_ref]))
    return items
