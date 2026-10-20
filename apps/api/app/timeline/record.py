"""Prontuário de um paciente montado a partir dos recursos FHIR, com utilitários de leitura."""

from __future__ import annotations

import base64
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import cached_property

from app import clock
from app.fhir_client import codes
from app.fhir_client.client import FhirSource
from app.reference import drugs, drugs_by_atc


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    if len(value) == 10:  # data sem hora
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def ref_id(reference: dict | None) -> str | None:
    if not reference or "reference" not in reference:
        return None
    return reference["reference"].split("/", 1)[-1]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text.lower()


def coding(cc: dict | None, system: str) -> dict | None:
    for c in (cc or {}).get("coding", []):
        if c.get("system") == system:
            return c
    return None


@dataclass(frozen=True)
class MedInfo:
    ref: str
    ingredient: str
    name: str
    drug_class: str
    text: str
    dose: str
    route: str
    times: tuple[str, ...]
    prn: bool
    status: str
    start: datetime | None
    end: datetime | None
    reason: str | None
    encounter_id: str | None
    prior_ref: str | None
    authored: datetime | None

    @property
    def dose_signature(self) -> tuple:
        return (self.dose.lower(), self.route.lower(), self.times, self.prn)


def med_identity(cc: dict) -> tuple[str, str, str]:
    """Resolve (princípio ativo, nome, classe) de um medicationCodeableConcept."""
    catalog = drugs()
    ing = coding(cc, codes.INGREDIENT)
    if ing and ing.get("code") in catalog:
        d = catalog[ing["code"]]
        return d.ingredient, d.name, d.drug_class
    atc = coding(cc, codes.ATC)
    if atc and atc.get("code") in drugs_by_atc():
        d = drugs_by_atc()[atc["code"]]
        return d.ingredient, d.name, d.drug_class
    text = normalize(cc.get("text", ""))
    for d in catalog.values():
        if normalize(d.name) in text:
            return d.ingredient, d.name, d.drug_class
    label = cc.get("text") or "Medicamento não identificado"
    return normalize(label), label, "desconhecida"


def med_info(mr: dict) -> MedInfo:
    ingredient, name, drug_class = med_identity(mr.get("medicationCodeableConcept", {}))
    dosage = (mr.get("dosageInstruction") or [{}])[0]
    repeat = dosage.get("timing", {}).get("repeat", {})
    bounds = repeat.get("boundsPeriod", {})
    dq = (dosage.get("doseAndRate") or [{}])[0].get("doseQuantity")
    if dq:
        value = dq["value"]
        value_txt = str(int(value)) if float(value).is_integer() else str(value).replace(".", ",")
        dose = f"{value_txt} {dq.get('unit', '')}".strip()
    else:
        dose = mr.get("medicationCodeableConcept", {}).get("text", name).replace(name, "").strip() or "—"
    return MedInfo(
        ref=f"MedicationRequest/{mr['id']}",
        ingredient=ingredient,
        name=name,
        drug_class=drug_class,
        text=dosage.get("text") or mr.get("medicationCodeableConcept", {}).get("text", name),
        dose=dose,
        route=dosage.get("route", {}).get("text", ""),
        times=tuple(t[:5] for t in repeat.get("timeOfDay", [])),
        prn=bool(dosage.get("asNeededBoolean")),
        status=mr.get("status", "unknown"),
        start=parse_dt(bounds.get("start")) or parse_dt(mr.get("authoredOn")),
        end=parse_dt(bounds.get("end")),
        reason=(mr.get("statusReason") or {}).get("text"),
        encounter_id=ref_id(mr.get("encounter")),
        prior_ref=(mr.get("priorPrescription") or {}).get("reference"),
        authored=parse_dt(mr.get("authoredOn")),
    )


@dataclass(frozen=True)
class PendingExam:
    ref: str
    texto: str
    proposto: bool  # intent=proposal (sugerido pelo relatório cirúrgico), ainda não é pedido formal
    pedido_em: datetime | None
    imagem: bool


@dataclass(frozen=True)
class ImplantInfo:
    """Implante (FHIR Device com UDI) do paciente e o procedimento que o usou (Procedure.usedReference)."""

    ref: str
    descricao: str
    gtin: str | None
    lote: str | None
    serie: str | None
    validade: str | None
    registro_anvisa: str | None
    procedimento: str | None
    procedimento_ref: str | None
    colocado_em: datetime | None
    encounter_id: str | None

    @property
    def refs(self) -> list[str]:
        return [self.ref, *([self.procedimento_ref] if self.procedimento_ref else [])]

    @property
    def rotulo(self) -> str:
        parts = [self.descricao]
        if self.lote:
            parts.append(f"lote {self.lote}")
        if self.serie:
            parts.append(f"série {self.serie}")
        if self.validade:
            y, m, d = self.validade[:10].split("-")
            parts.append(f"validade {d}/{m}/{y}")
        return ", ".join(parts)


def note_text(doc: dict) -> str:
    for content in doc.get("content", []):
        att = content.get("attachment", {})
        if att.get("data"):
            return base64.b64decode(att["data"]).decode("utf-8")
    return doc.get("description", "")


@dataclass
class Sector:
    encounter_id: str
    ward: str
    bed: str
    start: datetime | None
    end: datetime | None
    status: str


class PatientRecord:
    def __init__(
        self, patient: dict, resources: dict[str, list[dict]], locations: dict[str, dict], now: datetime | None = None
    ):
        self.patient = patient
        self.resources = resources
        self.locations = locations
        self.now = now or clock.now()

    @classmethod
    def load(cls, fhir: FhirSource, patient_id: str) -> PatientRecord | None:
        patient = fhir.get("Patient", patient_id)
        if patient is None:
            return None
        return cls(patient, fhir.patient_resources(patient_id), fhir.locations())

    @property
    def id(self) -> str:
        return self.patient["id"]

    # ---------- demografia ----------
    @property
    def name(self) -> str:
        names = self.patient.get("name") or [{}]
        n = names[0]
        return n.get("text") or " ".join([*n.get("given", []), n.get("family", "")]).strip()

    @property
    def age(self) -> int | None:
        bd = self.patient.get("birthDate")
        if not bd:
            return None
        born = date.fromisoformat(bd)
        today = self.now.date()
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))

    @property
    def sex(self) -> str:
        return {"male": "M", "female": "F"}.get(self.patient.get("gender", ""), "—")

    @property
    def scenario_tag(self) -> str | None:
        for tag in self.patient.get("meta", {}).get("tag", []):
            if tag.get("system") == codes.TAGS and tag.get("code", "").startswith("cenario"):
                return tag["code"]
        return None

    # ---------- internação ----------
    @cached_property
    def encounters(self) -> dict[str, dict]:
        return {e["id"]: e for e in self.resources.get("Encounter", [])}

    @cached_property
    def hospitalization(self) -> dict | None:
        top = [e for e in self.encounters.values() if e.get("class", {}).get("code") == "IMP" and not e.get("partOf")]
        if not top:
            return None
        active = [e for e in top if e.get("status") == "in-progress"]
        pool = active or top
        return max(pool, key=lambda e: parse_dt(e.get("period", {}).get("start")) or datetime.min.replace(tzinfo=UTC))

    @cached_property
    def sectors(self) -> list[Sector]:
        hosp = self.hospitalization
        if hosp is None:
            return []
        out = []
        for e in self.encounters.values():
            if ref_id(e.get("partOf")) != hosp["id"]:
                continue
            loc = (e.get("location") or [{}])[0]
            ward, bed = self._ward_bed(ref_id(loc.get("location")), loc.get("location", {}).get("display"))
            period = e.get("period", {})
            out.append(
                Sector(
                    e["id"],
                    ward or e.get("serviceType", {}).get("text", "—"),
                    bed,
                    parse_dt(period.get("start")),
                    parse_dt(period.get("end")),
                    e.get("status", ""),
                )
            )
        out.sort(key=lambda s: s.start or datetime.min.replace(tzinfo=UTC))
        return out

    def _ward_bed(self, loc_id: str | None, display: str | None) -> tuple[str, str]:
        loc = self.locations.get(loc_id or "")
        if loc:
            parent = self.locations.get(ref_id(loc.get("partOf")) or "")
            if parent:
                return parent.get("name", "—"), loc.get("name", "").replace("Leito ", "")
            return loc.get("name", "—"), ""
        if display and " — Leito " in display:
            ward, bed = display.split(" — Leito ", 1)
            return ward, bed
        return display or "—", ""

    @property
    def current_sector(self) -> Sector | None:
        active = [s for s in self.sectors if s.end is None or s.status == "in-progress"]
        return active[-1] if active else (self.sectors[-1] if self.sectors else None)

    def sector_of(self, encounter_id: str | None) -> Sector | None:
        for s in self.sectors:
            if s.encounter_id == encounter_id:
                return s
        return None

    @property
    def admission_start(self) -> datetime | None:
        hosp = self.hospitalization
        return parse_dt(hosp.get("period", {}).get("start")) if hosp else None

    @property
    def admission_end(self) -> datetime | None:
        hosp = self.hospitalization
        return parse_dt(hosp.get("period", {}).get("end")) if hosp else None

    @property
    def length_of_stay_days(self) -> int:
        start = self.admission_start
        if start is None:
            return 0
        end = self.admission_end or self.now
        return max(0, (end - start).days)

    @property
    def admission_urgent(self) -> bool:
        hosp = self.hospitalization or {}
        priority = coding(hosp.get("priority"), "http://terminology.hl7.org/CodeSystem/v3-ActPriority")
        if priority:
            return priority.get("code") == "EM"
        source = coding(hosp.get("hospitalization", {}).get("admitSource"), codes.ADMIT_SOURCE)
        return bool(source and source.get("code") == "emd")

    @property
    def admission_reason(self) -> str:
        hosp = self.hospitalization or {}
        return ((hosp.get("reasonCode") or [{}])[0]).get("text", "")

    @property
    def previous_emergency_visits(self) -> list[dict]:
        start = self.admission_start or self.now
        out = []
        for e in self.encounters.values():
            if e.get("class", {}).get("code") != "EMER":
                continue
            when = parse_dt(e.get("period", {}).get("start"))
            if when and when < start:
                out.append(e)
        return sorted(out, key=lambda e: e["period"]["start"])

    # ---------- medicamentos ----------
    @cached_property
    def medications(self) -> list[MedInfo]:
        """Prescrições do prontuário. Rascunhos (status=draft, propostos pela consulta com IA) ficam de fora."""
        return [med_info(mr) for mr in self.resources.get("MedicationRequest", []) if mr.get("status") != "draft"]

    @cached_property
    def proposed_meds(self) -> list[MedInfo]:
        """Prescrições propostas e assinadas na consulta (MedicationRequest draft/proposal), ainda não emitidas."""
        return [
            med_info(mr)
            for mr in self.resources.get("MedicationRequest", [])
            if mr.get("status") == "draft" and mr.get("intent") == "proposal"
        ]

    def meds_of_encounter(self, encounter_id: str) -> list[MedInfo]:
        return [m for m in self.medications if m.encounter_id == encounter_id]

    @property
    def active_meds(self) -> list[MedInfo]:
        return [m for m in self.medications if m.status == "active"]

    # ---------- contexto fixo ----------
    @property
    def active_conditions(self) -> list[dict]:
        return [
            c
            for c in self.resources.get("Condition", [])
            if (coding(c.get("clinicalStatus"), "http://terminology.hl7.org/CodeSystem/condition-clinical") or {}).get(
                "code", "active"
            )
            == "active"
        ]

    @property
    def allergies(self) -> list[dict]:
        return self.resources.get("AllergyIntolerance", [])

    @property
    def real_allergies(self) -> list[dict]:
        return [
            a
            for a in self.allergies
            if (coding(a.get("code"), codes.SNOMED) or {}).get("code") != codes.NO_KNOWN_ALLERGY
        ]

    @property
    def related_persons(self) -> list[dict]:
        return self.resources.get("RelatedPerson", [])

    @property
    def household_size(self) -> int | None:
        for o in self.resources.get("Observation", []):
            if (coding(o.get("code"), codes.LOINC) or {}).get("code") == codes.LIVES_ALONE_LOINC:
                return o.get("valueInteger")
        return None

    @property
    def pending_exams(self) -> list[PendingExam]:
        """Exames pedidos ou propostos (ServiceRequest de imagem/laboratório) ainda sem resultado.

        Vale para a passagem de plantão e o Waze da alta: ex.: o RX de controle proposto no relatório cirúrgico.
        """
        wanted = {codes.SR_IMAGING[0], codes.SR_LAB[0]}
        out = []
        for sr in self.resources.get("ServiceRequest", []):
            cats = {c.get("code") for cat in sr.get("category", []) for c in cat.get("coding", [])}
            if sr.get("status") not in ("draft", "active", "on-hold") or not cats & wanted:
                continue
            out.append(
                PendingExam(
                    ref=f"ServiceRequest/{sr['id']}",
                    texto=(sr.get("code") or {}).get("text", "Exame"),
                    proposto=sr.get("intent") == "proposal",
                    pedido_em=parse_dt(sr.get("authoredOn")),
                    imagem=codes.SR_IMAGING[0] in cats,
                )
            )
        out.sort(key=lambda e: e.pedido_em or datetime.min.replace(tzinfo=UTC))
        return out

    @property
    def implants(self) -> list[ImplantInfo]:
        """Implantes registrados (Device com lote/série/UDI), do mais recente ao mais antigo."""
        used: dict[str, dict] = {}
        for proc in self.resources.get("Procedure", []):
            for ref in proc.get("usedReference", []):
                used.setdefault(ref.get("reference", ""), proc)
        out = []
        for dev in self.resources.get("Device", []):
            if not (dev.get("lotNumber") or dev.get("serialNumber") or dev.get("udiCarrier")):
                continue
            proc = used.get(f"Device/{dev['id']}")
            udi = (dev.get("udiCarrier") or [{}])[0]
            registro = next(
                (i["value"] for i in dev.get("identifier", []) if i.get("system") == codes.ANVISA_REGISTRO), None
            )
            names = dev.get("deviceName") or [{}]
            out.append(
                ImplantInfo(
                    ref=f"Device/{dev['id']}",
                    descricao=(dev.get("type") or {}).get("text") or names[0].get("name") or "Implante",
                    gtin=udi.get("deviceIdentifier"),
                    lote=dev.get("lotNumber"),
                    serie=dev.get("serialNumber"),
                    validade=dev.get("expirationDate"),
                    registro_anvisa=registro,
                    procedimento=((proc or {}).get("code") or {}).get("text"),
                    procedimento_ref=f"Procedure/{proc['id']}" if proc else None,
                    colocado_em=parse_dt((proc or {}).get("performedDateTime")),
                    encounter_id=ref_id((proc or {}).get("encounter")),
                )
            )
        out.sort(key=lambda i: i.colocado_em or datetime.min.replace(tzinfo=UTC), reverse=True)
        return out

    @property
    def caregiver(self) -> dict | None:
        for rp in self.related_persons:
            for rel in rp.get("relationship", []):
                if coding(rel, codes.TAGS) and coding(rel, codes.TAGS).get("code") == "cuidador":
                    return rp
        return None

    def identifiers_for_pseudonymization(self) -> dict[str, list[str]]:
        """Identificadores diretos que nunca podem chegar ao LLM."""
        names: list[str] = []
        for n in self.patient.get("name", []):
            names.extend(n.get("given", []))
            if n.get("family"):
                names.append(n["family"])
            if n.get("text"):
                names.append(n["text"])
        family_names: list[str] = []
        phones = [t["value"] for t in self.patient.get("telecom", []) if t.get("value")]
        for rp in self.related_persons:
            for n in rp.get("name", []):
                if n.get("text"):
                    family_names.append(n["text"])
                    family_names.extend(n["text"].split())
            phones.extend(t["value"] for t in rp.get("telecom", []) if t.get("value"))
        return {
            "patient_names": names,
            "family_names": family_names,
            "cpf": [i["value"] for i in self.patient.get("identifier", []) if i.get("system") == codes.CPF],
            "phones": phones,
            "addresses": [a.get("text", "") for a in self.patient.get("address", []) if a.get("text")],
        }
