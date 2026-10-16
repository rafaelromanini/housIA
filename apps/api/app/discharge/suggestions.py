"""Sugestões concretas para o plano de alta (LLM ou gerador determinístico no modo mock)."""

from typing import Literal

from pydantic import BaseModel

from app.timeline.record import MedInfo, PendingExam


class Suggestion(BaseModel):
    texto: str
    motivo: str
    prioridade: Literal["alta", "media", "baixa"]
    responsavel: Literal["medico", "enfermagem", "servico_social", "farmacia", "fisioterapia", "equipe"]
    refs: list[str]


class DischargeLLMOutput(BaseModel):
    sugestoes: list[Suggestion]


def mock_suggestions(
    checklist: dict,
    lace_components: dict,
    social_items: list[dict],
    meds: list[MedInfo],
    new_meds: list[MedInfo],
    pending_exams: list[PendingExam] | tuple = (),
) -> dict:
    flagged = {i["campo"] for i in social_items}
    out: list[dict] = []

    def add(texto, motivo, prioridade, responsavel, refs):
        out.append(
            {"texto": texto, "motivo": motivo, "prioridade": prioridade, "responsavel": responsavel, "refs": refs}
        )

    if "mora_sozinho" in flagged and "tem_cuidador" in flagged:
        add(
            "Acionar o serviço social para mapear rede de apoio e solicitar visita domiciliar de enfermagem "
            "(atenção domiciliar/ESF) em até 48h após a alta.",
            "Mora sozinho e não tem cuidador.",
            "alta",
            "servico_social",
            ["Checklist/mora_sozinho", "Checklist/tem_cuidador"],
        )
    elif "tem_cuidador" in flagged:
        add(
            "Identificar com a família um cuidador de referência antes da alta.",
            "Sem cuidador definido.",
            "alta",
            "servico_social",
            ["Checklist/tem_cuidador"],
        )
    if "cuidador_capacitado" in flagged:
        add(
            "Treinar o cuidador nos cuidados e na administração de medicamentos, com teach-back registrado.",
            "Cuidador ainda não capacitado.",
            "alta",
            "enfermagem",
            ["Checklist/cuidador_capacitado"],
        )
    if "teach_back_realizado" in flagged:
        add(
            "Realizar teach-back do plano de alta (medicamentos, sinais de alarme, retorno) com paciente/cuidador.",
            "Compreensão do plano não confirmada.",
            "alta",
            "enfermagem",
            ["Checklist/teach_back_realizado"],
        )
    if "consulta_retorno_agendada" in flagged:
        add(
            "Agendar consulta de retorno em até 7 dias antes da saída e entregar o agendamento por escrito.",
            "Sem retorno agendado.",
            "alta",
            "equipe",
            ["Checklist/consulta_retorno_agendada"],
        )
    if "acesso_medicamentos" in flagged:
        add(
            "Farmácia: verificar disponibilidade dos medicamentos no SUS/Farmácia Popular e entregar receitas "
            "antes da alta.",
            "Acesso aos medicamentos não garantido.",
            "alta",
            "farmacia",
            ["Checklist/acesso_medicamentos"],
        )
    if "ja_tem_dispositivos" in flagged:
        devices = ", ".join(checklist.get("dispositivos") or []) or "dispositivos"
        add(
            f"Garantir {devices} no domicílio antes da alta (solicitação à atenção domiciliar).",
            "Dispositivo necessário ainda não disponível.",
            "alta",
            "servico_social",
            ["Checklist/ja_tem_dispositivos"],
        )
    if "mobilidade_reduzida" in flagged or "escadas_em_casa" in flagged:
        refs = [f"Checklist/{f}" for f in ("mobilidade_reduzida", "escadas_em_casa") if f in flagged]
        add(
            "Avaliação da fisioterapia para marcha e prevenção de quedas; orientar adaptação do domicílio.",
            "Mobilidade reduzida e/ou escadas em casa.",
            "media",
            "fisioterapia",
            refs,
        )

    for e in pending_exams:
        origin = "proposto no relatório cirúrgico" if e.proposto else "solicitado"
        add(
            f"Exame pendente antes da alta: {e.texto} — confirmar a realização e o laudo, ou registrar no plano de"
            " alta quando e onde será feito.",
            f"Exame {origin} ainda sem resultado.",
            "alta",
            "medico",
            [e.ref],
        )
    insulin = [m for m in new_meds if m.drug_class.startswith("insulina") and not m.prn]
    if insulin:
        add(
            "Insulina iniciada nesta internação: treinar a aplicação, conferir glicosímetro e insumos, e entregar "
            "plano escrito de reconhecimento e manejo de hipoglicemia.",
            "Primeiro uso de insulina.",
            "alta",
            "enfermagem",
            [m.ref for m in insulin],
        )
    scheduled = [m for m in meds if not m.prn]
    if len(scheduled) >= 5:
        add(
            "Simplificar a posologia (ex.: 1x/dia) se clinicamente possível — avaliar com o médico.",
            f"{len(scheduled)} medicamentos de uso regular.",
            "media",
            "medico",
            [m.ref for m in scheduled[:5]],
        )
    e = lace_components.get("E", {})
    if e.get("pontos", 0) >= 2:
        add(
            "Programar contato telefônico em 48–72h após a alta para checar sintomas e adesão.",
            f"{e.get('valor')} ao PS nos últimos 6 meses.",
            "media",
            "enfermagem",
            e.get("refs", []),
        )
    c = lace_components.get("C", {})
    if c.get("pontos", 0) >= 3:
        add(
            "Garantir seguimento ambulatorial das comorbidades (clínico/especialista) com resumo de alta.",
            f"Charlson {c.get('valor')}.",
            "baixa",
            "medico",
            c.get("refs", []),
        )
    return {"sugestoes": out}
