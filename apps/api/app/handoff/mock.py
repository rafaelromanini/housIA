"""Gerador determinístico da passagem de plantão (LLM_MODE=mock).

Monta a saída só a partir dos fatos duros, por público. Serve para rodar sem chave e para o CI.
"""

from app.handoff.schemas import Fact

RISK_PREFIX = {
    "tendencia": "Piora progressiva",
    "sinal_vital_fora_da_faixa": "Sinal vital fora da faixa",
    "diurese": "Diurese",
    "risco_queda": "Risco de queda",
    "lesao_por_pressao": "Lesão por pressão",
    "acesso": "Acesso venoso",
    "exame_alterado": "Exame alterado",
    "alergia": "Alergia x prescrição",
    "alerta_consulta": "Alerta da consulta",
    "preocupacao_nao_respondida": "Preocupação sem resposta",
    "interacao": "Interação medicamentosa",
    "omissao": "Possível omissão na transferência",
    "duplicidade": "Duplicidade terapêutica",
    "exame_pendente": "Exame pendente",
    "implante": "Implante",
}
HEADLINE_PRIORITY = ["alergia", "interacao", "omissao", "tendencia", "sinal_vital_fora_da_faixa", "diurese"]


def _relevant(facts: list[Fact], audience: str) -> list[Fact]:
    return [f for f in facts if audience in f.publico]


def _headline(facts: list[Fact]) -> str:
    serious = [f for f in facts if f.severidade in ("critico", "atencao") and f.tipo != "proxima_dose"]
    if not serious:
        return "Sem alterações críticas registradas no período; manter vigilância de rotina."
    serious.sort(
        key=lambda f: (
            f.severidade != "critico",
            HEADLINE_PRIORITY.index(f.tipo) if f.tipo in HEADLINE_PRIORITY else 99,
        )
    )
    top = serious[0]
    return f"{RISK_PREFIX.get(top.tipo, 'Atenção')}: {top.texto}"


def mock_handoff(audience: str, facts: list[Fact], patient: dict, situation_refs: list[str]) -> dict:
    rel = _relevant(facts, audience)
    sex = {"M": "masculino", "F": "feminino"}.get(patient.get("sexo", ""), "")
    motivo = patient.get("motivo") or "motivo sem registro"
    situacao = (
        f"[PACIENTE], {patient.get('idade', '—')} anos, sexo {sex}, "
        f"internado(a) há {patient.get('dias_internacao', 0)} dia(s) por {motivo}. "
        f"Atualmente em {patient.get('setor')} (leito {patient.get('leito')})."
    )
    if audience == "medico" and patient.get("diagnosticos"):
        situacao += f" Diagnósticos ativos: {', '.join(patient['diagnosticos'])}."
    if audience == "enfermagem":
        situacao += f" Alergias: {patient.get('alergias') or 'sem registro'}."

    change_types = {"transferencia", "mudanca_prescricao", "implante"} | (
        {"exame_alterado"} if audience == "medico" else set()
    )
    mudancas = [{"texto": f.texto, "refs": f.refs} for f in rel if f.tipo in change_types and f.refs]

    # sinal fora da faixa já coberto por uma tendência da mesma medida não vira risco duplicado
    trend_refs = {r for f in rel if f.tipo == "tendencia" for r in f.refs}
    riscos = [
        {
            "texto": f"{RISK_PREFIX.get(f.tipo, 'Atenção')}: {f.texto}",
            "horizonte_h": f.horizonte_h or 12,
            "severidade": f.severidade,
            "refs": f.refs,
        }
        for f in rel
        if f.severidade in ("critico", "atencao")
        and f.tipo not in ("proxima_dose", "mudanca_prescricao")
        and f.refs
        and not (f.tipo == "sinal_vital_fora_da_faixa" and set(f.refs) <= trend_refs)
    ]

    doses = sorted((f for f in rel if f.tipo == "proxima_dose"), key=lambda f: f.horizonte_h or 99)
    pendencias = [
        {"texto": f.texto.split(" — ", 1)[-1].rstrip("."), "horario": f.horario or "sem horário", "refs": f.refs}
        for f in doses
    ]
    pendencias += [
        {"texto": f"Exame pendente: {f.texto}", "horario": f.horario or "sem horário", "refs": f.refs}
        for f in rel
        if f.tipo == "exame_pendente"
    ]
    pendencias += [
        {
            "texto": f"Retomar com o paciente/família, com teach-back: {f.texto}",
            "horario": "sem horário",
            "refs": f.refs,
        }
        for f in rel
        if f.tipo == "preocupacao_nao_respondida"
    ]
    for f in rel:
        if f.severidade == "critico" and f.tipo in ("sinal_vital_fora_da_faixa", "tendencia"):
            pendencias.insert(
                0, {"texto": f"Reavaliar e registrar conduta: {f.texto}", "horario": "sem horário", "refs": f.refs}
            )
            break

    perguntas: list[str] = []
    if any(f.severidade == "critico" for f in rel):
        perguntas.append("Houve conduta para as alterações críticas do período? Qual foi a resposta?")
    if any(f.tipo == "diurese" and f.severidade == "atencao" for f in rel):
        perguntas.append("O paciente já urinou desde o último registro? Houve globo vesical?")
    if any(f.tipo == "mudanca_prescricao" for f in rel):
        perguntas.append("As mudanças de prescrição do período já foram aprazadas e conferidas?")
    if audience == "medico":
        perguntas.append("Há exames, pareceres ou metas terapêuticas pendentes que não estão registrados no sistema?")
    else:
        perguntas.append("Houve intercorrências, recusas de medicação ou doses atrasadas não registradas?")

    return {
        "headline": _headline(rel),
        "situacao": situacao,
        "situacao_refs": situation_refs,
        "mudancas_no_periodo": mudancas,
        "riscos_proximas_horas": riscos,
        "pendencias": pendencias,
        "perguntas_para_quem_sai": perguntas[:4],
    }
