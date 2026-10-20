"""Nota aprovada → texto de prontuário e recurso FHIR R4 (DocumentReference) para envio com um clique."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime

from app.consult import idiomas, note_templates
from app.fhir_client import codes

BUNDLE_NS = uuid.UUID("5b3f0c4e-9a1d-4c1e-8a57-2f0d7c0b6a11")
CLAIM_SYSTEM = "http://housia.dev/fhir/NamingSystem/claim-rascunho"
SURGERY_SYSTEM = "http://housia.dev/fhir/NamingSystem/cirurgia"

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


def note_as_text(
    kind: str,
    patient_name: str,
    author: str,
    when: datetime,
    note: dict,
    language: str,
    orders: list | None = None,
    interpretacao: str | None = None,
    selo: str | None = None,
) -> str:
    title = (note.get("modelo") or {}).get("titulo") or KIND_TITLE.get(kind, "Nota clínica")
    lines = [f"{title.upper()} — {patient_name}", f"{when.strftime('%d/%m/%Y %H:%M')} · {author}"]
    if selo:
        lines.append(selo)  # ex.: "Profissional verificado: CRM 123456/SP" (conselho conferido pelo admin, #33)
    if interpretacao:
        lines.append(interpretacao)  # ex.: "Consulta com interpretação por IA (es→pt-BR)"
    lines.append("")
    if kind == "cirurgia":
        report = note.get("relatorio_cirurgico", note)
        for f in report.get("campos", []):
            lines.append(f"{f['rotulo']}: {f['texto'] or '— (não ditado)'}")
        if report.get("cids"):
            lines.append("CID-10: " + "; ".join(f"{c['codigo']} {c['descricao']}" for c in report["cids"]))
        if report.get("opme"):
            lines.append("Materiais/OPME: " + "; ".join(f"{o['descricao']} ×{o['citado']}" for o in report["opme"]))
        implants = implant_rows(report)
        if implants:
            lines.append("Implantes (rastreio):")
            lines += [f"- {i['descricao']}: {implant_label(i)}" for i in implants]
        ready = report.get("prontidao")
        if ready:
            lines.append(
                f"Prontidão para faturamento: {ready['score']}/100 ({ready['convenio_rotulo']}); "
                f"{ready['criticas']} pendência(s) crítica(s), {ready['atencoes']} de atenção."
            )
        checks = [v for v in report.get("verificacoes", []) if v.get("status") != "pendente"]
        if checks:
            lines += ["", "Verificações perioperatórias:"]
            lines += [f"- {v['rotulo']}: {CHECK_STATUS.get(v['status'], v['status'])} — {v['texto']}" for v in checks]
        if report.get("pendencias"):
            lines += ["", "Pendências:"]
            lines += [f"- {p['texto']}" for p in report["pendencias"]]
        if report.get("pedidos"):
            lines.append("Pedidos propostos: " + "; ".join(p["descricao"] for p in report["pedidos"]))
    else:
        sections = note_templates.sections_for_view(note) or [
            (key, label, note.get(key, {}).get("texto", "")) for key, label in NOTE_SECTIONS
        ]
        for _, label, text in sections:
            lines.append(f"{label}: {text}")
        if note.get("cids"):
            lines.append("CID-10: " + "; ".join(f"{c['codigo']} {c['descricao']} ({c['tipo']})" for c in note["cids"]))
        if note.get("cronologia"):
            lines += ["", "Resumo cronológico:"]
            lines += [f"- {c['quando']} {c['texto']}" for c in note["cronologia"]]
        if note.get("pendencias"):
            lines += ["", "Pendências:"]
            lines += [f"- {p['texto']}" for p in note["pendencias"]]
        if note.get("orientacoes_paciente"):
            lines += ["", f"Orientações ao paciente ({idiomas.rotulo(language)}):"]
            lines += [f"- {o}" for o in note["orientacoes_paciente"]]
    if orders:
        lines += ["", "Pedidos e prescrições assinados (rascunhos, ainda sem emissão):"]
        lines += [f"- {order_line(o)}" for o in orders]
    lines += ["", "Documento gerado com apoio de IA (HousIA) e revisado pelo profissional responsável."]
    return "\n".join(lines)


def selo_profissional(user) -> str | None:
    """Selo "verificado" só com o conselho conferido pelo administrador; sem isso, o número sai como informado."""
    if not (user.conselho and user.conselho_numero and user.uf):
        return None
    registro = f"{user.conselho} {user.conselho_numero}/{user.uf}"
    if user.conselho_verificado:
        return f"Profissional verificado: {registro}"
    return f"Registro informado, ainda não verificado: {registro}"


def order_line(o) -> str:
    """Uma linha do pedido assinado (receita ou exame) para o texto do prontuário e a impressão."""
    verb = {"iniciar": "Iniciar", "alterar_dose": "Alterar dose de", "suspender": "Suspender", "solicitar": "Solicitar"}
    parts = [o.item, o.dose, o.via, o.frequencia, o.duracao]
    text = f"{verb.get(o.acao, o.acao)} " + " · ".join(p for p in parts if p)
    if o.acao == "alterar_dose" and o.dose_anterior:
        text += f" (dose atual: {o.dose_anterior})"
    if o.decisao.justificativa:
        text += f" — justificativa: {o.decisao.justificativa}"
    return text


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
    interpretacao: str | None = None,
    loinc: tuple[str, str] | None = None,
    title: str | None = None,
    approved: bool = True,
) -> dict:
    """DocumentReference da nota. `docStatus` é "preliminary" até a aprovação (revisão + atestação), depois "final".
    `loinc` e `title` vêm do modelo da nota (visita de enfermagem: 34746-8; visita médica: 11506-3)."""
    code, display = loinc or (LOINC_OPERATIVE_NOTE if kind == "cirurgia" else LOINC_CONSULT_NOTE)
    label = f"{title or KIND_TITLE.get(kind, 'Nota clínica')} (HousIA)"
    resource = {
        "resourceType": "DocumentReference",
        "status": "current",
        "docStatus": "final" if approved else "preliminary",
        "identifier": [{"system": "http://housia.dev/fhir/NamingSystem/consulta", "value": str(consultation_id)}],
        "type": {"coding": [{"system": codes.LOINC, "code": code, "display": display}], "text": label},
        "subject": {"reference": f"Patient/{patient_id}", "display": patient_name},
        "date": when.isoformat(),
        "author": [{"display": author}],
        "description": f"{label} — {interpretacao}" if interpretacao else label,
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


# ---------- rastreio do implante, Procedure e Claim em rascunho ----------
def implant_rows(report: dict) -> list[dict]:
    """Uma linha por leitura de etiqueta de item implantável (lote, série, validade, registro ANVISA)."""
    rows = []
    for line in report.get("opme", []):
        if not line.get("implantavel"):
            continue
        for lote in line.get("lotes", []):
            rows.append(
                {
                    "leitura": lote["id"],
                    "codigo": line["codigo"],
                    "descricao": line["descricao"],
                    "gtin": lote.get("gtin"),
                    "lote": lote.get("lote"),
                    "serie": lote.get("serie"),
                    "validade": lote.get("validade"),
                    "quantidade": lote.get("quantidade", 1),
                    "registro_anvisa": lote.get("registro_anvisa") or line.get("registro_anvisa"),
                }
            )
    return rows


def implant_label(row: dict) -> str:
    parts = [f"lote {row['lote']}" if row.get("lote") else None, f"série {row['serie']}" if row.get("serie") else None]
    if row.get("validade"):
        y, m, d = row["validade"].split("-")
        parts.append(f"validade {d}/{m}/{y}")
    if row.get("registro_anvisa"):
        parts.append(f"registro ANVISA {row['registro_anvisa']}")
    return ", ".join(p for p in parts if p) or "sem etiqueta"


def device_resources(report: dict, patient_id: str) -> list[tuple[str, dict]]:
    """Device FHIR de cada implante lido na etiqueta: UDI (GTIN), lote, série e validade, ligado ao paciente."""
    out = []
    for row in implant_rows(report):
        resource: dict = {
            "resourceType": "Device",
            "status": "active",
            "deviceName": [{"name": row["descricao"], "type": "user-friendly-name"}],
            "type": {"text": row["descricao"]},
            "patient": {"reference": f"Patient/{patient_id}"},
            "note": [{"text": "Implante registrado pelo HousIA a partir da etiqueta lida na sala (rastreabilidade)."}],
        }
        if row.get("gtin"):
            resource["udiCarrier"] = [
                {
                    "deviceIdentifier": row["gtin"],
                    "issuer": codes.GS1_ISSUER,
                    "entryType": "barcode",
                }
            ]
        if row.get("lote"):
            resource["lotNumber"] = row["lote"]
        if row.get("serie"):
            resource["serialNumber"] = row["serie"]
        if row.get("validade"):
            resource["expirationDate"] = row["validade"]
        if row.get("registro_anvisa"):
            resource["identifier"] = [{"system": codes.ANVISA_REGISTRO, "value": row["registro_anvisa"]}]
        if row["quantidade"] != 1:
            resource["property"] = [{"type": {"text": "quantidade"}, "valueQuantity": {"value": row["quantidade"]}}]
        out.append((row["leitura"], resource))
    return out


def procedure_label(report: dict) -> str:
    field = next((f for f in report.get("campos", []) if f["campo"] == "procedimento" and f["texto"]), None)
    return (field["texto"] if field else (report.get("modelo") or {}).get("rotulo")) or "Procedimento cirúrgico"


def procedure_resource(
    *,
    report: dict,
    patient_id: str,
    encounter_id: str | None,
    when: datetime,
    consultation_id: int,
    device_refs: list[str],
    document_ref: str,
) -> dict:
    """Procedure da cirurgia com os implantes em `usedReference` e o relatório aprovado em `report`."""
    resource: dict = {
        "resourceType": "Procedure",
        "identifier": [{"system": SURGERY_SYSTEM, "value": str(consultation_id)}],
        "status": "completed",
        "category": {
            "coding": [{"system": codes.SNOMED, "code": codes.SR_SURGICAL[0], "display": codes.SR_SURGICAL[1]}],
            "text": "cirurgia",
        },
        "code": {"text": procedure_label(report)},
        "subject": {"reference": f"Patient/{patient_id}"},
        "performedDateTime": when.isoformat(),
        "report": [{"reference": document_ref}],
        "usedReference": [{"reference": ref} for ref in device_refs],
        "note": [{"text": "Registro gerado pelo HousIA a partir do relatório cirúrgico revisado e aprovado."}],
    }
    if encounter_id:
        resource["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    return resource


def claim_resource(
    *,
    report: dict,
    patient_id: str,
    patient_name: str,
    when: datetime,
    consultation_id: int,
    numero_guia: str | None,
    device_refs: dict[str, str],
) -> dict:
    """FHIR Claim em RASCUNHO (nada é cobrado): procedimento (código DEMO), CID principal e materiais usados."""
    from app.consult import antiglosa

    ready = report.get("prontidao") or {}
    conv = antiglosa.convenios().get(ready.get("convenio", "generico"), antiglosa.convenios()["generico"])
    proc = antiglosa.procedures().get((report.get("modelo") or {}).get("id", ""))
    system = codes.SIGTAP_DEMO if conv.codigos == "sigtap_demo" else codes.TUSS_DEMO
    insurance: dict = {"sequence": 1, "focal": True, "coverage": {"display": conv.rotulo}}
    if numero_guia:
        insurance["preAuthRef"] = [numero_guia]
    resource: dict = {
        "resourceType": "Claim",
        "identifier": [{"system": CLAIM_SYSTEM, "value": str(consultation_id)}],
        "status": "draft",
        "type": {"coding": [{"system": codes.CLAIM_TYPE, "code": "institutional"}]},
        "use": "claim" if numero_guia else "preauthorization",
        "patient": {"reference": f"Patient/{patient_id}", "display": patient_name},
        "created": when.isoformat(),
        "provider": {"display": "Hospital (HousIA — demonstração)"},
        "priority": {"coding": [{"system": codes.PROCESS_PRIORITY, "code": "normal"}]},
        "insurance": [insurance],
    }
    cids = report.get("cids", [])
    if cids:
        resource["diagnosis"] = [
            {
                "sequence": i + 1,
                "diagnosisCodeableConcept": {
                    "coding": [{"system": codes.ICD10, "code": c["codigo"], "display": c["descricao"]}]
                },
                "type": [{"coding": [{"system": codes.DIAGNOSIS_TYPE, "code": c.get("tipo", "principal")}]}]
                if c.get("tipo") == "principal"
                else [],
            }
            for i, c in enumerate(cids)
        ]
        for d in resource["diagnosis"]:
            if not d["type"]:
                del d["type"]
    items: list[dict] = []
    if proc:
        code = proc.sigtap if conv.codigos == "sigtap_demo" else proc.tuss
        coding = {"system": system, "code": code, "display": f"{proc.descricao} (código de demonstração)"}
        resource["procedure"] = [
            {"sequence": 1, "procedureCodeableConcept": {"coding": [coding]}, "date": when.isoformat()}
        ]
        items.append(
            {
                "sequence": 1,
                "productOrService": {"coding": [coding]},
                "quantity": {"value": 1},
                "procedureSequence": [1],
            }
        )
    tracking = any(line.get("consumido") for line in report.get("opme", []))
    for line in report.get("opme", []):
        qty = line.get("consumido") if tracking else line.get("citado")
        if not qty:
            continue
        item: dict = {
            "sequence": len(items) + 1,
            "productOrService": {
                "coding": [{"system": codes.OPME_DEMO, "code": line["codigo"], "display": line["descricao"]}]
            },
            "quantity": {"value": qty},
        }
        udi = [{"reference": device_refs[lote["id"]]} for lote in line.get("lotes", []) if lote["id"] in device_refs]
        if udi:
            item["udi"] = udi
        if line.get("status") not in ("conferido",) and not line.get("justificativa"):
            item["extension"] = [
                {"url": "http://housia.dev/fhir/StructureDefinition/pendencia-opme", "valueString": line["status"]}
            ]
        items.append(item)
    if items:
        resource["item"] = items
    return resource


def urn(consultation_id: int, kind: str, index: int = 0) -> str:
    return f"urn:uuid:{uuid.uuid5(BUNDLE_NS, f'{consultation_id}/{kind}/{index}')}"


def transaction_bundle(entries: list[tuple[str, dict]]) -> dict:
    """Bundle FHIR de transação: (fullUrl, recurso) → POST. Nada é enviado: serve para exportar e conferir."""
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {"fullUrl": url, "resource": r, "request": {"method": "POST", "url": r["resourceType"]}}
            for url, r in entries
        ],
    }
