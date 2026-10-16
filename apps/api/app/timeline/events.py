"""Timeline Clínica Normalizada: todos os pilares consomem esta mesma estrutura."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from app.fhir_client import codes
from app.timeline.record import PatientRecord, coding, med_info, note_text, parse_dt, ref_id

EventKind = Literal["vital", "lab", "med_order", "med_admin", "note", "procedure", "transfer", "diagnosis", "io"]
KINDS: tuple[str, ...] = ("vital", "lab", "med_order", "med_admin", "note", "procedure", "transfer", "diagnosis", "io")


class ClinicalEvent(BaseModel):
    id: str
    patient_id: str
    timestamp: datetime
    kind: EventKind
    summary: str
    value: dict | None = None
    source_ref: str  # ex.: "Observation/123" — obrigatório, usado nas citações
    encounter_id: str | None = None
    location: str | None = None
    # extensões do MVP: severidade de valores fora da faixa e texto integral de evoluções
    severity: Literal["normal", "atencao", "critico"] | None = None
    text: str | None = None


def _sev_label(level: str) -> str:
    return {"atencao": " (fora da faixa)", "critico": " (CRÍTICO)"}.get(level, "")


def _worst(*levels: str) -> str:
    order = {"normal": 0, "atencao": 1, "critico": 2}
    return max(levels, key=lambda lv: order[lv]) if levels else "normal"


class TimelineBuilder:
    def __init__(self, record: PatientRecord):
        self.r = record
        self.events: list[ClinicalEvent] = []

    def _loc(self, encounter_id: str | None) -> str | None:
        sec = self.r.sector_of(encounter_id)
        return sec.ward if sec else None

    def _add(
        self,
        *,
        eid: str,
        ts: datetime | None,
        kind: str,
        summary: str,
        source_ref: str,
        encounter_id: str | None = None,
        value: dict | None = None,
        severity: str | None = None,
        text: str | None = None,
    ) -> None:
        if ts is None:
            return
        self.events.append(
            ClinicalEvent(
                id=eid,
                patient_id=self.r.id,
                timestamp=ts,
                kind=kind,
                summary=summary,
                value=value,
                source_ref=source_ref,
                encounter_id=encounter_id,
                location=self._loc(encounter_id),
                severity=severity,
                text=text,
            )
        )

    def build(self) -> list[ClinicalEvent]:
        self._transfers()
        self._conditions()
        self._med_orders()
        self._med_admins()
        self._observations()
        self._procedures()
        self._notes()
        self.events.sort(key=lambda e: (e.timestamp, e.id))
        return self.events

    def _transfers(self) -> None:
        hosp = self.r.hospitalization
        if hosp is None:
            return
        previous = None
        for i, sec in enumerate(self.r.sectors):
            bed = f" — Leito {sec.bed}" if sec.bed else ""
            if i == 0:
                via = "urgência" if self.r.admission_urgent else "eletiva"
                summary = f"Internação ({via}) em {sec.ward}{bed}: {self.r.admission_reason}"
            else:
                summary = f"Transferência de {previous.ward} para {sec.ward}{bed}"
            self._add(
                eid=f"Encounter/{sec.encounter_id}",
                ts=sec.start,
                kind="transfer",
                summary=summary,
                source_ref=f"Encounter/{sec.encounter_id}",
                encounter_id=sec.encounter_id,
                value={"de": previous.ward if previous else None, "para": sec.ward, "leito": sec.bed},
            )
            previous = sec

    def _conditions(self) -> None:
        for c in self.r.resources.get("Condition", []):
            icd = coding(c.get("code"), codes.ICD10)
            label = c.get("code", {}).get("text") or (icd or {}).get("display", "Diagnóstico")
            code_txt = f" ({icd['code']})" if icd else ""
            # diagnóstico de base (anterior à internação) aparece no momento do registro
            ts = parse_dt(c.get("recordedDate")) or parse_dt(c.get("onsetDateTime"))
            enc = ref_id(c.get("encounter"))
            first_sector = self.r.sectors[0].encounter_id if self.r.sectors else enc
            self._add(
                eid=f"Condition/{c['id']}",
                ts=ts,
                kind="diagnosis",
                summary=f"Diagnóstico: {label}{code_txt}",
                source_ref=f"Condition/{c['id']}",
                encounter_id=first_sector,
                value={"cid10": icd["code"] if icd else None, "texto": label},
            )

    def _med_orders(self) -> None:
        by_ref = {m.ref: m for m in self.r.medications}
        for mr in self.r.resources.get("MedicationRequest", []):
            m = med_info(mr)
            prefix = "Prescrição proposta (rascunho)" if m.status == "draft" else "Prescrição"
            if m.status != "draft" and m.prior_ref and m.prior_ref in by_ref:
                prefix = f"Prescrição (substitui {by_ref[m.prior_ref].name})"
            value = {
                "principio_ativo": m.ingredient,
                "classe": m.drug_class,
                "dose": m.dose,
                "via": m.route,
                "horarios": list(m.times),
                "se_necessario": m.prn,
                "status": m.status,
            }
            self._add(
                eid=m.ref,
                ts=m.authored or m.start,
                kind="med_order",
                summary=f"{prefix}: {m.text}",
                source_ref=m.ref,
                encounter_id=m.encounter_id,
                value=value,
            )
            if m.status == "stopped" and m.end:
                reason = f" — motivo: {m.reason}" if m.reason else ""
                self._add(
                    eid=f"{m.ref}#suspensao",
                    ts=m.end,
                    kind="med_order",
                    summary=f"Suspenso: {m.name} {m.dose}{reason}",
                    source_ref=m.ref,
                    encounter_id=m.encounter_id,
                    value={**value, "evento": "suspensao", "motivo": m.reason},
                )

    def _med_admins(self) -> None:
        from app.timeline.record import med_identity

        for ma in self.r.resources.get("MedicationAdministration", []):
            _, name, _ = med_identity(ma.get("medicationCodeableConcept", {}))
            dose = ma.get("dosage", {}).get("text", "")
            enc = ref_id(ma.get("context"))
            self._add(
                eid=f"MedicationAdministration/{ma['id']}",
                ts=parse_dt(ma.get("effectiveDateTime")),
                kind="med_admin",
                summary=f"Administrado: {name} {dose}".strip(),
                source_ref=f"MedicationAdministration/{ma['id']}",
                encounter_id=enc,
                value={"pedido": (ma.get("request") or {}).get("reference")},
            )

    def _observations(self) -> None:
        for o in self.r.resources.get("Observation", []):
            code = (o.get("code", {}).get("coding") or [{}])[0].get("code")
            category = ((o.get("category") or [{}])[0].get("coding") or [{}])[0].get("code")
            if category == "social-history":
                continue
            ts = parse_dt(o.get("effectiveDateTime"))
            enc = ref_id(o.get("encounter"))
            src = f"Observation/{o['id']}"
            if code == codes.BP_PANEL[0]:
                comps = {(c["code"]["coding"][0]["code"]): c["valueQuantity"]["value"] for c in o.get("component", [])}
                pas, pad = comps.get(codes.VITALS["pas"].code), comps.get(codes.VITALS["pad"].code)
                if pas is None or pad is None:
                    continue
                level = _worst(codes.classify(codes.VITALS["pas"], pas), codes.classify(codes.VITALS["pad"], pad))
                self._add(
                    eid=src,
                    ts=ts,
                    kind="vital",
                    summary=f"PA {pas:g}/{pad:g} mmHg{_sev_label(level)}",
                    source_ref=src,
                    encounter_id=enc,
                    severity=level,
                    value={"codigo": code, "chave": "pa", "sistolica": pas, "diastolica": pad, "unidade": "mmHg"},
                )
                continue
            measure = codes.BY_CODE.get(code or "")
            vq = o.get("valueQuantity")
            if measure is None or vq is None:
                continue
            value = float(vq["value"])
            level = codes.classify(measure, value)
            kind = "lab" if measure.key in codes.LABS else "io" if measure.key in codes.IO else "vital"
            unit = f" {measure.unit}" if measure.unit else ""
            note = "; ".join(n["text"] for n in o.get("note", []) if n.get("text"))
            note_txt = f" ({note})" if note else ""
            summary = f"{measure.label}: {codes.fmt_number(value, measure.decimals)}{unit}{note_txt}{_sev_label(level)}"
            self._add(
                eid=src,
                ts=ts,
                kind=kind,
                summary=summary,
                source_ref=src,
                encounter_id=enc,
                severity=level if kind != "io" else None,
                value={"codigo": code, "chave": measure.key, "valor": value, "unidade": measure.unit},
            )

    def _procedures(self) -> None:
        for p in self.r.resources.get("Procedure", []):
            cat = p.get("category", {}).get("text")
            cat_txt = f" [{cat}]" if cat else ""
            self._add(
                eid=f"Procedure/{p['id']}",
                ts=parse_dt(p.get("performedDateTime")),
                kind="procedure",
                summary=f"Procedimento{cat_txt}: {p.get('code', {}).get('text', '')}",
                source_ref=f"Procedure/{p['id']}",
                encounter_id=ref_id(p.get("encounter")),
                value={"categoria": cat},
            )

    def _notes(self) -> None:
        for d in self.r.resources.get("DocumentReference", []):
            text = note_text(d)
            label = d.get("type", {}).get("text", "Evolução")
            author = ", ".join(a.get("display", "") for a in d.get("author", []))
            enc_refs = d.get("context", {}).get("encounter") or [{}]
            preview = text if len(text) <= 160 else text[:157].rsplit(" ", 1)[0] + "…"
            self._add(
                eid=f"DocumentReference/{d['id']}",
                ts=parse_dt(d.get("date")),
                kind="note",
                summary=f"{label} — {author}: {preview}",
                source_ref=f"DocumentReference/{d['id']}",
                encounter_id=ref_id(enc_refs[0]),
                text=text,
                value={"tipo": label, "autor": author},
            )


def build_timeline(record: PatientRecord) -> list[ClinicalEvent]:
    return TimelineBuilder(record).build()


def window(events: list[ClinicalEvent], now: datetime, hours: int) -> list[ClinicalEvent]:
    start = now - timedelta(hours=hours)
    return [e for e in events if start <= e.timestamp <= now]
