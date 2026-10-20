"""Consentimento da consulta (#13): registro por finalidade (Consent FHIR R4), recusa sem IA e marcos de privacidade.

O consentimento fica em `extra_json["consentimento"]` (com o recurso Consent e o estado ativo/pausado/revogado). As
travas saem daqui: recusa ou `ia_externa=false` desligam a análise, a tradução e a nota por LLM (só as regras rodam);
`emocoes=false` desliga a análise de emoções; `revogado` encerra a captura. Consultas anteriores a este registro
(sem a chave) continuam funcionando como antes.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.consult.schemas import Consentimento

FINALIDADE_SYSTEM = "https://housia.dev/fhir/CodeSystem/finalidade-consulta"
SCOPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/consentscope"
CATEGORY_SYSTEM = "http://loinc.org"
PURPOSES: dict[str, str] = {
    "transcricao": "Transcrição da conversa",
    "ia_externa": "Análise por inteligência artificial externa",
    "traducao": "Tradução simultânea",
    "emocoes": "Análise de emoções e comunicação",
}
WHO_LABEL = {"paciente": "o(a) próprio(a) paciente", "acompanhante": "acompanhante", "responsavel": "responsável legal"}
MARKER_TEXT = {
    "pausa": "Pausa de privacidade: a transcrição foi interrompida.",
    "retomada": "Retomada: a transcrição voltou.",
    "revogado": "Consentimento revogado: a captura foi encerrada.",
}
MARKER_STATE = {"pausa": "pausado", "retomada": "ativo", "revogado": "revogado"}

# Aviso lido ao paciente, no idioma dele (a tela também o lê em voz alta). Texto simples e curto, sem jargão.
NOTICES: dict[str, str] = {
    "pt-BR": (
        "Esta consulta será transcrita em texto para ajudar o atendimento; o áudio não é gravado. Com a sua "
        "autorização, programas de inteligência artificial ajudam a organizar a conversa, sem o seu nome. "
        "Você pode recusar, pedir uma pausa ou retirar a autorização a qualquer momento, e o atendimento "
        "continua normalmente."
    ),
    "es": (
        "Esta consulta se transcribirá a texto para ayudar en la atención; el audio no se graba. Con su "
        "autorización, programas de inteligencia artificial ayudan a organizar la conversación, sin su "
        "nombre. Puede negarse, pedir una pausa o retirar su autorización en cualquier momento, y la atención "
        "continúa con normalidad."
    ),
    "en": (
        "This visit will be transcribed into text to support your care; the audio is not recorded. With your "
        "permission, artificial intelligence programs help organize the conversation, without your name. You "
        "may refuse, ask for a pause or withdraw your permission at any time, and your care will continue as "
        "usual."
    ),
    "fr": (
        "Cette consultation sera transcrite en texte pour aider à la prise en charge ; l'audio n'est pas "
        "enregistré. Avec votre accord, des programmes d'intelligence artificielle aident à organiser la "
        "conversation, sans votre nom. Vous pouvez refuser, demander une pause ou retirer votre accord à tout "
        "moment, et la prise en charge continue normalement."
    ),
    "it": (
        "Questa visita sarà trascritta in testo per supportare l'assistenza; l'audio non viene registrato. "
        "Con il suo consenso, programmi di intelligenza artificiale aiutano a organizzare la conversazione, "
        "senza il suo nome. Può rifiutare, chiedere una pausa o ritirare il consenso in qualsiasi momento, e "
        "l'assistenza prosegue normalmente."
    ),
    "de": (
        "Dieses Gespräch wird zur Unterstützung der Behandlung als Text mitgeschrieben; Audio wird nicht "
        "aufgezeichnet. Mit Ihrer Erlaubnis helfen Programme der künstlichen Intelligenz, das Gespräch zu "
        "ordnen, ohne Ihren Namen. Sie können ablehnen, eine Pause verlangen oder Ihre Erlaubnis jederzeit "
        "zurückziehen; die Behandlung geht normal weiter."
    ),
    "zh": (
        "本次就诊将被转写为文字以协助诊疗，不会录音。经您同意，人工智能程序会在不含您姓名的情况下帮助整理对话。您可以拒绝、要求暂停，或随时撤回同意，诊疗将照常进行。"
    ),
    "ar": (
        "سيتم تحويل هذه الزيارة إلى نص لدعم الرعاية، ولا يتم تسجيل الصوت. وبموافقتك، تساعد برامج الذكاء "
        "الاصطناعي في تنظيم المحادثة دون ذكر اسمك. يمكنك الرفض أو طلب إيقاف مؤقت أو سحب موافقتك في أي وقت، "
        "وستستمر الرعاية كالمعتاد."
    ),
    "ht": (
        "Y ap transkri konsiltasyon sa a an tèks pou ede swen ou; yo pa anrejistre vwa a. Avèk pèmisyon ou, "
        "pwogram entèlijans atifisyèl ede òganize konvèsasyon an, san non ou. Ou ka refize, mande yon poz "
        "oswa retire pèmisyon ou nenpòt lè, epi swen an kontinye nòmalman."
    ),
}


@dataclass(frozen=True)
class Flags:
    """O que o consentimento permite agora."""

    ia: bool = True  # análise, nota e copiloto por LLM
    traducao: bool = True
    stt: bool = True  # transcrição no servidor (o áudio sai do navegador)
    emocoes: bool = True
    captura: bool = True  # False: consentimento revogado
    pausado: bool = False
    registrado: bool = False  # False: consulta anterior ao registro de consentimento


def state(extra: dict | None) -> dict | None:
    return (extra or {}).get("consentimento")


def flags(extra: dict | None) -> Flags:
    cons = state(extra)
    if not cons:
        return Flags()
    fin = cons.get("finalidades", {})
    estado = cons.get("estado", "ativo")
    revoked = estado == "revogado"
    refused = bool(cons.get("recusou")) or revoked
    ia = bool(fin.get("ia_externa")) and not refused
    return Flags(
        ia=ia,
        traducao=ia and bool(fin.get("traducao")),
        stt=bool(fin.get("transcricao")) and not refused,
        emocoes=bool(fin.get("emocoes")) and not bool(cons.get("recusou")),
        captura=not revoked,
        pausado=estado == "pausado",
        registrado=True,
    )


def normalize(c: Consentimento) -> Consentimento:
    """Recusa vale para todas as finalidades: nada fica autorizado."""
    if not c.recusou:
        return c
    return c.model_copy(
        update={"finalidades": c.finalidades.model_copy(update={k: False for k in PURPOSES})},
    )


def _purpose(code: str) -> dict:
    return {"system": FINALIDADE_SYSTEM, "code": code, "display": PURPOSES[code]}


def build_consent(
    patient_id: str, c: Consentimento, who_display: str | None, when: str, status: str | None = None
) -> dict:
    """Consent R4: provision.type geral (permit/deny) e uma sub-provision por finalidade (purpose)."""
    performer = (
        {"reference": f"RelatedPerson/{c.related_person_id}", "display": who_display}
        if c.related_person_id and c.quem != "paciente"
        else {"reference": f"Patient/{patient_id}"}
    )
    permitted = [k for k in PURPOSES if getattr(c.finalidades, k)]
    return {
        "resourceType": "Consent",
        "status": status or ("rejected" if c.recusou else "active"),
        "scope": {"coding": [{"system": SCOPE_SYSTEM, "code": "patient-privacy", "display": "Privacy Consent"}]},
        "category": [{"coding": [{"system": CATEGORY_SYSTEM, "code": "59284-0", "display": "Consent Document"}]}],
        "patient": {"reference": f"Patient/{patient_id}"},
        "dateTime": when,
        "performer": [performer],
        "policyRule": {"text": "LGPD (Lei 13.709/2018) e CFM 2.454/2026"},
        "provision": {
            "type": "permit" if permitted else "deny",
            "provision": [
                {"type": "permit" if getattr(c.finalidades, k) else "deny", "purpose": [_purpose(k)]} for k in PURPOSES
            ],
        },
        "extension": [
            {"url": f"{FINALIDADE_SYSTEM}/quem-consentiu", "valueString": c.quem},
            {"url": f"{FINALIDADE_SYSTEM}/origem", "valueString": "HousIA consulta com IA"},
        ],
    }


def public(cons: dict | None) -> dict | None:
    """O que a tela recebe: sem o recurso FHIR inteiro."""
    if not cons:
        return None
    f = flags({"consentimento": cons})
    return {
        "quem": cons.get("quem"),
        "quem_nome": cons.get("quem_nome"),
        "related_person_id": cons.get("related_person_id"),
        "finalidades": cons.get("finalidades"),
        "recusou": bool(cons.get("recusou")),
        "estado": cons.get("estado", "ativo"),
        "em": cons.get("em"),
        "por": cons.get("por"),
        "fhir_ref": cons.get("fhir_ref"),
        "permite": {"ia": f.ia, "traducao": f.traducao, "transcricao_servidor": f.stt, "emocoes": f.emocoes},
    }
