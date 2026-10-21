"""Smart App "Protocolos (atenção primária)": migrado para o registro (#21), lê o que o engine já calculou."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="protocolos",
    nome="Protocolos (atenção primária)",
    aba="prontuario",
    ordem=30,
    recursos_fhir=["Condition", "Observation"],
    saida=Saida(card="lista"),
    refs_obrigatorias=False,
    vazio="Nenhum protocolo se aplica a este paciente.",
)


def run(ctx: AppContext) -> list[AppItem]:
    sev = {
        "em_dia": "ok",
        "no_alvo": "ok",
        "abordado": "ok",
        "pendente": "atencao",
        "sem_registro": "atencao",
        "fora_do_alvo": "critico",
        "critico": "critico",
    }
    return [
        AppItem(
            texto=f"{p.eixo + ' · ' if p.eixo else ''}{p.protocolo}: {p.item}",
            detalhe=(p.ultimo or p.orientacao) + (f"\nFonte: {p.fonte}" if p.fonte else ""),
            severidade=sev[p.status],
            refs=p.refs,
        )
        for p in ctx.ins.protocolos
    ]
