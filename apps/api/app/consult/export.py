"""Nota aprovada → texto de prontuário e recurso FHIR R4 (DocumentReference) para envio com um clique."""

from __future__ import annotations

import base64
from datetime import datetime

from app.consult.schemas import LANGUAGE_LABELS
from app.fhir_client import codes

NOTE_SECTIONS = [
    ("queixa_principal", "Queixa principal"),
    ("hda", "História da doença atual"),
    ("antecedentes", "Antecedentes"),
    ("medicacoes", "Medicações em uso"),
    ("alergias", "Alergias"),
    ("exame_fisico", "Exame físico"),
    ("exames_complementares", "Exames complementares"),
    ("avaliacao", "Avaliação"),
    ("plano", "Plano"),
]
LOINC_CONSULT_NOTE = ("11488-4", "Consult note")
LOINC_OPERATIVE_NOTE = ("11504-8", "Surgical operation note")
CHECK_STATUS = {"ok": "ok", "info": "informação", "atencao": "atenção", "critico": "crítico"}
KIND_TITLE = {"consulta": "Nota de consulta", "visita": "Nota de visita médica", "cirurgia": "Relatório cirúrgico"}


def note_as_text(kind: str, patient_name: str, author: str, when: datetime, note: dict, language: str) -> str:
    title = KIND_TITLE.get(kind, "Nota clínica")
    lines = [f"{title.upper()} — {patient_name}", f"{when.strftime('%d/%m/%Y %H:%M')} · {author}", ""]
    if kind == "cirurgia":
        report = note.get("relatorio_cirurgico", note)
        for f in report.get("campos", []):
            lines.append(f"{f['rotulo']}: {f['texto'] or '— (não ditado)'}")
        if report.get("cids"):
            lines.append("CID-10: " + "; ".join(f"{c['codigo']} {c['descricao']}" for c in report["cids"]))
        if report.get("opme"):
            lines.append("Materiais/OPME: " + "; ".join(f"{o['descricao']} ×{o['citado']}" for o in report["opme"]))
        checks = [v for v in report.get("verificacoes", []) if v.get("status") != "pendente"]
        if checks:
            lines += ["", "Verificações perioperatórias:"]
            lines += [f"- {v['rotulo']}: {CHECK_STATUS.get(v['status'], v['status'])} — {v['texto']}" for v in checks]
        if report.get("pedidos"):
            lines.append("Pedidos propostos: " + "; ".join(p["descricao"] for p in report["pedidos"]))
    else:
        for key, label in NOTE_SECTIONS:
            lines.append(f"{label}: {note.get(key, {}).get('texto', '')}")
        if note.get("cids"):
            lines.append("CID-10: " + "; ".join(f"{c['codigo']} {c['descricao']} ({c['tipo']})" for c in note["cids"]))
        if note.get("orientacoes_paciente"):
            lines += ["", f"Orientações ao paciente ({LANGUAGE_LABELS.get(language, language)}):"]
            lines += [f"- {o}" for o in note["orientacoes_paciente"]]
    lines += ["", "Documento gerado com apoio de IA (HousIA) e revisado pelo profissional responsável."]
    return "\n".join(lines)


def document_reference(
    *,
    kind: str,
    patient_id: str,
    patient_name: str,
    encounter_id: str | None,
    author: str,
    when: datetime,
    text: str,
    consultation_id: int,
) -> dict:
    code, display = LOINC_OPERATIVE_NOTE if kind == "cirurgia" else LOINC_CONSULT_NOTE
    label = f"{KIND_TITLE.get(kind, 'Nota clínica')} (HousIA)"
    resource = {
        "resourceType": "DocumentReference",
        "status": "current",
        "docStatus": "final",
        "identifier": [{"system": "http://housia.dev/fhir/NamingSystem/consulta", "value": str(consultation_id)}],
        "type": {"coding": [{"system": codes.LOINC, "code": code, "display": display}], "text": label},
        "subject": {"reference": f"Patient/{patient_id}", "display": patient_name},
        "date": when.isoformat(),
        "author": [{"display": author}],
        "description": label,
        "content": [
            {
                "attachment": {
                    "contentType": "text/plain; charset=utf-8",
                    "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                    "title": label,
                }
            }
        ],
    }
    if encounter_id:
        resource["context"] = {"encounter": [{"reference": f"Encounter/{encounter_id}"}]}
    return resource
