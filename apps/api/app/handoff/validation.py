"""Validação obrigatória de citações: item sem fonte válida não chega à tela."""

from app.handoff.schemas import HandoffOutput

LIST_SECTIONS = ("mudancas_no_periodo", "riscos_proximas_horas", "pendencias")


def validate_refs(output: HandoffOutput, valid_refs: set[str]) -> tuple[HandoffOutput, list[dict]]:
    """Descarta itens sem refs ou com qualquer ref inexistente; devolve (saída limpa, descartados)."""
    dropped: list[dict] = []
    data = output.model_dump()
    for section in LIST_SECTIONS:
        kept = []
        for item in data[section]:
            invalid = [r for r in item["refs"] if r not in valid_refs]
            if not item["refs"] or invalid:
                dropped.append(
                    {
                        "secao": section,
                        "texto": item["texto"],
                        "refs_invalidas": invalid,
                        "motivo": "sem fonte" if not item["refs"] else "fonte inexistente na timeline",
                    }
                )
            else:
                kept.append(item)
        data[section] = kept
    bad_situation = [r for r in data["situacao_refs"] if r not in valid_refs]
    if bad_situation:
        dropped.append(
            {
                "secao": "situacao_refs",
                "texto": "(referências removidas da situação)",
                "refs_invalidas": bad_situation,
                "motivo": "fonte inexistente na timeline",
            }
        )
        data["situacao_refs"] = [r for r in data["situacao_refs"] if r in valid_refs]
    return HandoffOutput.model_validate(data), dropped
