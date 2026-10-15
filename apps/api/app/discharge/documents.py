"""Documentos de alta: sumário SOAP (para colar no prontuário) e orientações em linguagem simples ao paciente.

Inspirado na estruturação SOAP e na tradução de ferramentas de consulta (docs/benchmark-lya.md), aplicado à
transição hospital → casa. Toda linha cita a fonte; o HousIA não escreve no prontuário.
"""

from typing import Literal

from pydantic import BaseModel

from app.timeline.events import ClinicalEvent
from app.timeline.record import MedInfo, PatientRecord

Language = Literal["pt-BR", "es", "en"]
HOME_ROUTES = {"vo", "sc", "inalatória", "inalatoria", "sl", "tópica", "topica"}


class Section(BaseModel):
    texto: str
    refs: list[str]


class Soap(BaseModel):
    subjetivo: Section
    objetivo: Section
    avaliacao: Section
    plano: Section


class MedInstruction(BaseModel):
    medicamento: str
    como_usar: str
    horarios: list[str]
    para_que: str
    refs: list[str]


class PatientInstructions(BaseModel):
    titulo: str
    resumo_internacao: Section
    medicamentos: list[MedInstruction]
    sinais_de_alerta: list[Section]
    cuidados_em_casa: list[Section]
    retorno: Section


class DischargeDocsOutput(BaseModel):
    soap: Soap
    orientacoes: PatientInstructions


PURPOSE = {
    "betabloqueador": "proteger o coração e controlar a pressão",
    "ieca": "controlar a pressão e proteger o coração e os rins",
    "bra": "controlar a pressão e proteger o coração e os rins",
    "bloqueador_canal_calcio": "controlar a pressão",
    "diuretico_alca": "eliminar o excesso de líquido (inchaço e falta de ar)",
    "diuretico_tiazidico": "controlar a pressão",
    "diuretico_poupador_potassio": "ajudar o coração e eliminar líquido",
    "estatina": "controlar o colesterol",
    "insulina_basal": "controlar o açúcar no sangue",
    "insulina_rapida": "corrigir o açúcar alto no sangue",
    "biguanida": "controlar o açúcar no sangue",
    "sulfonilureia": "controlar o açúcar no sangue",
    "analgesico_simples": "dor ou febre",
    "opioide": "dor forte",
    "aine": "dor e inflamação",
    "ibp": "proteger o estômago",
    "antiagregante": "evitar entupimento dos vasos",
    "anticoagulante_oral": "evitar coágulos (trombose)",
    "heparina": "evitar trombose",
    "beta2_agonista": "falta de ar ou chiado no peito",
    "anticolinergico_inalatorio": "falta de ar",
    "corticoide": "diminuir a inflamação",
    "benzodiazepinico": "ansiedade ou sono",
}
ROUTE_LABEL = {"VO": "pela boca", "SC": "injeção embaixo da pele", "inalatória": "inalação (bombinha)"}


def home_meds(record: PatientRecord) -> list[MedInfo]:
    """Prescrições ativas com via de uso domiciliar (exclui medicações venosas hospitalares)."""
    return [m for m in record.active_meds if m.route.lower() in HOME_ROUTES]


def mock_documents(record: PatientRecord, events: list[ClinicalEvent], assessment: dict | None) -> dict:
    hosp_ref = f"Encounter/{record.hospitalization['id']}" if record.hospitalization else None
    base_refs = [hosp_ref] if hosp_ref else []
    notes = [e for e in events if e.kind == "note" and e.text and "médica" in (e.value or {}).get("tipo", "")]
    last_note = notes[-1] if notes else None
    latest_vitals: dict[str, ClinicalEvent] = {}
    for e in events:
        if e.kind == "vital" and e.value and e.value.get("chave") in ("pa", "fc", "fr", "spo2", "temp", "glicemia"):
            latest_vitals[e.value["chave"]] = e
    abnormal_labs = {}
    for e in events:
        if e.kind == "lab" and e.value:
            abnormal_labs[e.value["chave"]] = e
    abnormal_labs = {k: e for k, e in abnormal_labs.items() if e.severity in ("atencao", "critico")}
    conditions = record.active_conditions
    cond_text = ", ".join(c.get("code", {}).get("text", "") for c in conditions) or "sem registro"
    cond_refs = [f"Condition/{c['id']}" for c in conditions]
    meds = home_meds(record)
    checklist = (assessment or {}).get("checklist") or {}
    suggestions = (assessment or {}).get("sugestoes") or []

    subjective = f"Internado(a) por {record.admission_reason or 'motivo sem registro'}."
    if last_note:
        subjective += f" Última evolução médica: {last_note.text.split('. ')[0].rstrip('.')}."
    objective = "Últimos registros: " + "; ".join(e.summary for e in latest_vitals.values()) + "."
    if abnormal_labs:
        objective += (
            " Exames alterados: "
            + "; ".join(e.summary.replace(" (fora da faixa)", "") for e in abnormal_labs.values())
            + "."
        )
    assessment_text = f"Diagnósticos ativos: {cond_text}."
    if assessment:
        assessment_text += (
            f" Risco de reinternação {assessment['final_risk']} (LACE {assessment['lace']['score']}, "
            f"score social {assessment['social']['score']})."
        )
    plan_parts = ["Medicamentos para casa: " + ("; ".join(m.text for m in meds) or "sem prescrição domiciliar") + "."]
    if suggestions:
        plan_parts.append("Plano de transição: " + "; ".join(s["texto"].rstrip(".") for s in suggestions[:4]) + ".")
    plan_parts.append(
        "Retorno: " + ("agendado." if checklist.get("consulta_retorno_agendada") else "a agendar em até 7 dias.")
    )

    has_hf = any((c.get("code", {}).get("coding") or [{}])[0].get("code", "").startswith("I50") for c in conditions)
    insulin = [m for m in meds if m.drug_class.startswith("insulina")]
    surgery = [e for e in events if e.kind == "procedure" and (e.value or {}).get("categoria") == "cirurgia"]
    anticoag = [m for m in meds if m.drug_class in ("anticoagulante_oral", "heparina")]

    alerts = [
        {
            "texto": "Febre acima de 37,8 °C, falta de ar ou dor forte que não melhora com o remédio: procure "
            "atendimento.",
            "refs": base_refs,
        }
    ]
    if has_hf:
        alerts.append(
            {
                "texto": "Inchaço nas pernas, falta de ar ao deitar ou ganho de mais de 2 kg em 3 dias.",
                "refs": [r for r in cond_refs if r],
            }
        )
    if insulin:
        alerts.append(
            {
                "texto": "Tremor, suor frio, tontura ou confusão podem ser açúcar baixo: tome um copo de suco "
                "ou água com açúcar e meça a glicemia.",
                "refs": [m.ref for m in insulin],
            }
        )
    if surgery:
        alerts.append(
            {"texto": "Vermelhidão, calor, inchaço ou secreção no corte da cirurgia.", "refs": [surgery[-1].source_ref]}
        )
    if anticoag:
        alerts.append(
            {
                "texto": "Sangramentos (gengiva, urina escura, fezes pretas) ou manchas roxas grandes.",
                "refs": [m.ref for m in anticoag],
            }
        )

    care = []
    if has_hf:
        care.append({"texto": "Pese-se todos os dias pela manhã, depois de urinar, e anote o peso.", "refs": cond_refs})
    if insulin:
        care.append(
            {
                "texto": "Guarde a insulina na geladeira (fora do congelador) e troque o local da aplicação.",
                "refs": [m.ref for m in insulin],
            }
        )
    if checklist.get("mobilidade_reduzida") or checklist.get("escadas_em_casa"):
        care.append(
            {
                "texto": "Use o apoio indicado para andar e evite escadas sem ajuda.",
                "refs": [f"Checklist/{k}" for k in ("mobilidade_reduzida", "escadas_em_casa") if checklist.get(k)],
            }
        )
    care.append({"texto": "Leve esta folha e todos os seus remédios na consulta de retorno.", "refs": base_refs})

    return {
        "soap": {
            "subjetivo": {"texto": subjective, "refs": base_refs + ([last_note.source_ref] if last_note else [])},
            "objetivo": {
                "texto": objective,
                "refs": [e.source_ref for e in latest_vitals.values()] + [e.source_ref for e in abnormal_labs.values()],
            },
            "avaliacao": {"texto": assessment_text, "refs": cond_refs or base_refs},
            "plano": {"texto": " ".join(plan_parts), "refs": [m.ref for m in meds] or base_refs},
        },
        "orientacoes": {
            "titulo": "Orientações para depois da alta",
            "resumo_internacao": {
                "texto": f"Você ficou internado(a) por: {record.admission_reason}.",
                "refs": base_refs,
            },
            "medicamentos": [
                {
                    "medicamento": m.name,
                    "como_usar": f"{m.dose} — {ROUTE_LABEL.get(m.route, m.route)}",
                    "horarios": list(m.times) or ["quando precisar"],
                    "para_que": PURPOSE.get(m.drug_class, "conforme orientação médica"),
                    "refs": [m.ref],
                }
                for m in meds
            ],
            "sinais_de_alerta": alerts,
            "cuidados_em_casa": care,
            "retorno": {
                "texto": "Seu retorno já está agendado: leve esta folha."
                if checklist.get("consulta_retorno_agendada")
                else "Agende uma consulta na sua UBS em até 7 dias.",
                "refs": ["Checklist/consulta_retorno_agendada"]
                if "consulta_retorno_agendada" in checklist
                else base_refs,
            },
        },
    }
