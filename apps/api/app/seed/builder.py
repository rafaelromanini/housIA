"""Converte cenários escritos numa DSL compacta (PT-BR) em recursos FHIR R4.

Todos os horários da DSL são relativos a uma âncora ("agora" no momento da carga),
por exemplo "-3d", "-4h30m", "+2h". Assim a demo sempre tem dados "recentes".
Ver data/scenarios/README.md para o formato.
"""

import base64
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app import clock
from app.config import get_settings
from app.fhir_client import codes
from app.reference import drugs

_OFFSET_RE = re.compile(r"^([+-])?((?:\d+[dhm])+)$")
_PART_RE = re.compile(r"(\d+)([dhm])")

WARDS: dict[str, str] = {
    "UTI Adulto": "uti",
    "Enfermaria 3B": "enf3b",
    "Clínica Cirúrgica 4A": "cc4a",
    "Pronto-Socorro": "ps",
}

FREQ_TEXT = {1: "1x/dia", 2: "12/12h", 3: "8/8h", 4: "6/6h", 6: "4/4h"}


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def parse_offset(spec: str) -> timedelta:
    spec = spec.strip()
    if spec in ("0", "agora"):
        return timedelta(0)
    m = _OFFSET_RE.match(spec)
    if not m:
        raise ValueError(f"Offset inválido: {spec!r} (use algo como -3d4h, -30m, +2h)")
    sign = -1 if m.group(1) == "-" else 1
    total = timedelta(0)
    for qty, unit in _PART_RE.findall(m.group(2)):
        n = int(qty)
        total += {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
    return sign * total


def iso(dt: datetime) -> str:
    return dt.astimezone(tz()).isoformat(timespec="seconds")


def ref(rtype: str, rid: str, display: str | None = None) -> dict:
    out = {"reference": f"{rtype}/{rid}"}
    if display:
        out["display"] = display
    return out


def fhir_id(text: str) -> str:
    """IDs FHIR aceitam só [A-Za-z0-9-.] (até 64 caracteres)."""
    return re.sub(r"[^A-Za-z0-9.-]", "-", text)[:64]


def slug(text: str) -> str:
    table = str.maketrans("áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ", "aaaaeeiooouc" + "AAAAEEIOOOUC")
    return re.sub(r"[^a-z0-9]+", "-", text.translate(table).lower()).strip("-")


@dataclass
class LocationRegistry:
    """Setores (Location ward) e leitos (Location bed, partOf setor), compartilhados entre pacientes."""

    resources: dict[str, dict] = field(default_factory=dict)

    def ward(self, name: str) -> str:
        wid = f"loc-{WARDS.get(name, slug(name))}"
        if wid not in self.resources:
            self.resources[wid] = {
                "resourceType": "Location",
                "id": wid,
                "status": "active",
                "name": name,
                "mode": "instance",
                "physicalType": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/location-physical-type",
                            "code": "wa",
                            "display": "Ala",
                        }
                    ]
                },
            }
        return wid

    def bed(self, ward_name: str, bed: str) -> str:
        wid = self.ward(ward_name)
        bid = f"{wid}-{slug(bed)}"
        if bid not in self.resources:
            self.resources[bid] = {
                "resourceType": "Location",
                "id": bid,
                "status": "active",
                "name": f"Leito {bed}",
                "mode": "instance",
                "physicalType": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/location-physical-type",
                            "code": "bd",
                            "display": "Leito",
                        }
                    ]
                },
                "partOf": ref("Location", wid, ward_name),
            }
        return bid


@dataclass
class _Sector:
    key: str
    enc_id: str
    ward: str
    bed: str
    bed_id: str
    start: datetime
    end: datetime | None


class ScenarioBuilder:
    def __init__(self, scenario: dict, anchor: datetime, locations: LocationRegistry):
        self.s = scenario
        self.sid: str = scenario["id"]
        self.anchor = anchor
        self.locations = locations
        self.out: list[dict] = []
        self.sectors: list[_Sector] = []
        self.patient_ref = ref("Patient", self.sid, scenario["paciente"]["nome"])
        self._rx_last_id: dict[str, str] = {}
        # Paciente ambulatorial (agenda de consultas) em vez de internado
        self.ambulatory = "ambulatorio" in scenario
        if self.ambulatory:
            visits = scenario["ambulatorio"]["consultas"]
            today = next((c for c in visits if c.get("status", "finished") != "finished"), visits[-1])
            self.start_spec: str = today["inicio"]
            self.main_enc_id = f"{self.sid}-amb-{today['id']}"
        else:
            self.start_spec = scenario["internacao"]["inicio"]
            self.main_enc_id = f"{self.sid}-int"

    # ---------- utilidades de tempo ----------
    def t(self, spec: str) -> datetime:
        return self.anchor + parse_offset(spec)

    def sector_at(self, when: datetime) -> _Sector:
        for sec in self.sectors:
            if sec.start <= when and (sec.end is None or when < sec.end):
                return sec
        # antes da admissão no primeiro setor ou depois da alta: usa o mais próximo
        return self.sectors[0] if when < self.sectors[0].start else self.sectors[-1]

    def sector(self, key: str) -> _Sector:
        for sec in self.sectors:
            if sec.key == key:
                return sec
        raise KeyError(f"[{self.sid}] setor desconhecido: {key}")

    def enc_for(self, item: dict, when: datetime) -> dict:
        sec = self.sector(item["setor"]) if "setor" in item else self.sector_at(when)
        return ref("Encounter", sec.enc_id)

    # ---------- construção ----------
    def build(self) -> list[dict]:
        self._patient()
        self._encounters()
        self._previous_encounters()
        self._conditions()
        self._allergies()
        self._related()
        self._social()
        self._prescriptions()
        self._vitals()
        self._labs()
        self._io()
        self._scales()
        self._procedures()
        self._surgeries()
        self._notes()
        return self.out

    def _patient(self) -> None:
        p = self.s["paciente"]
        parts = p["nome"].split()
        resource = {
            "resourceType": "Patient",
            "id": self.sid,
            "meta": {
                "tag": [
                    {
                        "system": codes.TAGS,
                        "code": self.s.get("tag", "populacao-fundo"),
                        "display": self.s.get("titulo", "População de fundo"),
                    }
                ]
            },
            "identifier": [{"system": codes.CPF, "value": p["cpf"]}],
            "name": [{"use": "official", "text": p["nome"], "family": parts[-1], "given": parts[:-1]}],
            "gender": p["sexo"],
            "birthDate": p["nascimento"],
            "telecom": [{"system": "phone", "value": p["telefone"]}] if p.get("telefone") else [],
            "address": [{"text": p["endereco"]}] if p.get("endereco") else [],
        }
        self.out.append(resource)

    def _encounters(self) -> None:
        if self.ambulatory:
            self._ambulatory_encounters()
            return
        it = self.s["internacao"]
        hosp_id = f"{self.sid}-int"
        start = self.t(it["inicio"])
        end = self.t(it["fim"]) if it.get("fim") else None
        urgent = it.get("tipo", "urgencia") == "urgencia"
        locs = []
        for sec_def in it["setores"]:
            sec_start = self.t(sec_def.get("inicio", it["inicio"]))
            sec_end = self.t(sec_def["fim"]) if sec_def.get("fim") else end
            bed_id = self.locations.bed(sec_def["setor"], sec_def["leito"])
            sec = _Sector(
                sec_def["id"],
                f"{hosp_id}-{sec_def['id']}",
                sec_def["setor"],
                sec_def["leito"],
                bed_id,
                sec_start,
                sec_end,
            )
            self.sectors.append(sec)
            period = {"start": iso(sec_start)} | ({"end": iso(sec_end)} if sec_end else {})
            loc_entry = {
                "location": ref("Location", bed_id, f"{sec.ward} — Leito {sec.bed}"),
                "status": "completed" if sec_end else "active",
                "period": period,
            }
            locs.append(loc_entry)
            self.out.append(
                {
                    "resourceType": "Encounter",
                    "id": sec.enc_id,
                    "status": "finished" if sec_end else "in-progress",
                    "class": {"system": codes.ACT_CODE, "code": "IMP", "display": "internação"},
                    "serviceType": {"text": sec.ward},
                    "subject": self.patient_ref,
                    "partOf": ref("Encounter", hosp_id),
                    "period": period,
                    "location": [loc_entry],
                }
            )
        self.out.append(
            {
                "resourceType": "Encounter",
                "id": hosp_id,
                "status": "finished" if end else "in-progress",
                "class": {"system": codes.ACT_CODE, "code": "IMP", "display": "internação"},
                "priority": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v3-ActPriority",
                            "code": "EM" if urgent else "R",
                            "display": "urgência" if urgent else "eletiva",
                        }
                    ]
                },
                "subject": self.patient_ref,
                "period": {"start": iso(start)} | ({"end": iso(end)} if end else {}),
                "reasonCode": [{"text": it["motivo"]}],
                "hospitalization": {
                    "admitSource": {
                        "coding": [
                            {
                                "system": codes.ADMIT_SOURCE,
                                "code": "emd" if urgent else "outp",
                                "display": "pronto-socorro" if urgent else "ambulatório (eletiva)",
                            }
                        ]
                    }
                },
                "location": locs,
            }
        )

    def _ambulatory_encounters(self) -> None:
        """Consultas ambulatoriais (Encounter class AMB): histórico + a consulta de hoje (agenda)."""
        amb = self.s["ambulatorio"]
        ward_id = self.locations.ward(amb["servico"])
        visits = sorted(amb["consultas"], key=lambda c: self.t(c["inicio"]))
        for n, c in enumerate(visits):
            start = self.t(c["inicio"])
            status = c.get("status", "finished")
            end = start + parse_offset(c.get("duracao", "30m")) if status == "finished" else None
            window_end = self.t(visits[n + 1]["inicio"]) if n + 1 < len(visits) else None
            enc_id = f"{self.sid}-amb-{c['id']}"
            self.sectors.append(
                _Sector(c["id"], enc_id, amb["servico"], amb.get("sala", ""), ward_id, start, window_end)
            )
            self.out.append(
                {
                    "resourceType": "Encounter",
                    "id": enc_id,
                    "status": status,
                    "class": {"system": codes.ACT_CODE, "code": "AMB", "display": "ambulatorial"},
                    "serviceType": {"text": amb["servico"]},
                    "subject": self.patient_ref,
                    "participant": [{"individual": {"display": c.get("profissional", amb.get("profissional", ""))}}],
                    "period": {"start": iso(start)} | ({"end": iso(end)} if end else {}),
                    "reasonCode": [{"text": c["motivo"]}],
                    "location": [
                        {
                            "location": ref("Location", ward_id, f"{amb['servico']} — {amb.get('sala', '')}".strip()),
                            "status": "completed" if end else "active",
                        }
                    ],
                }
            )

    def _previous_encounters(self) -> None:
        ps_ward = self.locations.ward("Pronto-Socorro")
        for i, enc in enumerate(self.s.get("encontros_anteriores", [])):
            start = self.t(enc["inicio"])
            end = start + parse_offset(enc.get("duracao", "6h"))
            emer = enc.get("tipo", "urgencia") == "urgencia"
            self.out.append(
                {
                    "resourceType": "Encounter",
                    "id": f"{self.sid}-ant-{i}",
                    "status": "finished",
                    "class": {
                        "system": codes.ACT_CODE,
                        "code": "EMER" if emer else "IMP",
                        "display": "emergência" if emer else "internação",
                    },
                    "subject": self.patient_ref,
                    "period": {"start": iso(start), "end": iso(end)},
                    "reasonCode": [{"text": enc["motivo"]}],
                    "location": [{"location": ref("Location", ps_ward, "Pronto-Socorro"), "status": "completed"}],
                }
            )

    def _conditions(self) -> None:
        hosp = ref("Encounter", self.main_enc_id)
        for i, dx in enumerate(self.s.get("diagnosticos", [])):
            when = self.t(dx.get("inicio", self.start_spec))
            self.out.append(
                {
                    "resourceType": "Condition",
                    "id": f"{self.sid}-dx-{i}",
                    "clinicalStatus": {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                "code": dx.get("status", "active"),
                            }
                        ]
                    },
                    "verificationStatus": {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                                "code": "confirmed",
                            }
                        ]
                    },
                    "category": [
                        {
                            "coding": [
                                {
                                    "system": "http://terminology.hl7.org/CodeSystem/condition-category",
                                    "code": "encounter-diagnosis" if dx.get("principal") else "problem-list-item",
                                }
                            ]
                        }
                    ],
                    "code": {
                        "coding": [{"system": codes.ICD10, "code": dx["cid"], "display": dx["texto"]}],
                        "text": dx["texto"],
                    },
                    "subject": self.patient_ref,
                    "encounter": hosp,
                    "onsetDateTime": iso(when),
                    "recordedDate": iso(self.t(dx.get("registro", dx.get("inicio", self.start_spec)))),
                }
            )

    def _allergies(self) -> None:
        allergies = self.s.get("alergias", [])
        recorded = iso(self.t(self.start_spec))
        if not allergies:
            self.out.append(
                {
                    "resourceType": "AllergyIntolerance",
                    "id": f"{self.sid}-alg-nka",
                    "clinicalStatus": {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                                "code": "active",
                            }
                        ]
                    },
                    "verificationStatus": {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification",
                                "code": "confirmed",
                            }
                        ]
                    },
                    "code": {
                        "coding": [
                            {
                                "system": codes.SNOMED,
                                "code": codes.NO_KNOWN_ALLERGY,
                                "display": "Sem alergias conhecidas",
                            }
                        ],
                        "text": "Nega alergias conhecidas",
                    },
                    "patient": self.patient_ref,
                    "recordedDate": recorded,
                }
            )
            return
        catalog = drugs()
        for i, alg in enumerate(allergies):
            if "principio_ativo" in alg:
                drug = catalog[alg["principio_ativo"]]
                coding = {"system": codes.INGREDIENT, "code": drug.ingredient, "display": drug.name}
            else:
                coding = {"system": codes.DRUG_CLASS, "code": alg["classe"], "display": alg["texto"]}
            self.out.append(
                {
                    "resourceType": "AllergyIntolerance",
                    "id": f"{self.sid}-alg-{i}",
                    "clinicalStatus": {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                                "code": "active",
                            }
                        ]
                    },
                    "verificationStatus": {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification",
                                "code": "confirmed",
                            }
                        ]
                    },
                    "type": "allergy",
                    "category": ["medication"],
                    "criticality": alg.get("criticidade", "high"),
                    "code": {"coding": [coding], "text": alg["texto"]},
                    "patient": self.patient_ref,
                    "recordedDate": recorded,
                    "reaction": [
                        {"manifestation": [{"text": alg["reacao"]}], "severity": alg.get("gravidade", "severe")}
                    ],
                }
            )

    def _related(self) -> None:
        for i, rp in enumerate(self.s.get("familiares", [])):
            coding = [{"system": codes.ROLE_CODE, "code": rp.get("codigo", "FAMMEMB"), "display": rp["parentesco"]}]
            if rp.get("cuidador"):
                coding.append({"system": codes.TAGS, "code": "cuidador", "display": "cuidador principal"})
            self.out.append(
                {
                    "resourceType": "RelatedPerson",
                    "id": f"{self.sid}-fam-{i}",
                    "patient": self.patient_ref,
                    "relationship": [{"coding": coding, "text": rp.get("descricao", rp["parentesco"])}],
                    "name": [{"text": rp["nome"]}],
                    "telecom": [{"system": "phone", "value": rp["telefone"]}] if rp.get("telefone") else [],
                }
            )

    def _social(self) -> None:
        social = self.s.get("social")
        if not social or "moradores" not in social:
            return
        self.out.append(
            {
                "resourceType": "Observation",
                "id": f"{self.sid}-soc-moradores",
                "status": "final",
                "category": [
                    {
                        "coding": [
                            {"system": codes.OBS_CATEGORY, "code": "social-history", "display": "Histórico social"}
                        ]
                    }
                ],
                "code": {
                    "coding": [
                        {
                            "system": codes.LOINC,
                            "code": codes.LIVES_ALONE_LOINC,
                            "display": "How many people are living or staying at this address?",
                        }
                    ],
                    "text": "Pessoas que moram no domicílio (incluindo o paciente)",
                },
                "subject": self.patient_ref,
                "effectiveDateTime": iso(self.t(self.start_spec)),
                "valueInteger": social["moradores"],
            }
        )

    # ---------- medicamentos ----------
    def _prescriptions(self) -> None:
        catalog = drugs()
        for rx in self.s.get("prescricoes", []):
            drug = catalog[rx["med"]]
            med_start = self.t(rx["inicio"]) if "inicio" in rx else None
            med_end = self.t(rx["fim"]) if "fim" in rx else None
            sector_keys = rx.get("setores") or ([rx["setor"]] if "setor" in rx else [s.key for s in self.sectors])
            for n, key in enumerate(sector_keys):
                sec = self.sector(key)
                if self.ambulatory:  # receita de uso contínuo: vale até o fim indicado, não até a próxima consulta
                    start, end_candidates = med_start or sec.start, [med_end] if med_end else []
                else:
                    start = max(med_start, sec.start) if med_start else sec.start
                    end_candidates = [d for d in (med_end, sec.end) if d is not None]
                end = min(end_candidates) if end_candidates else None
                rid = fhir_id(f"{self.sid}-rx-{rx['id']}-{key}")
                self._medication_request(rid, rx, drug, sec, start, end, is_last=n == len(sector_keys) - 1)
                self._rx_last_id[rx["id"]] = rid

    def _medication_request(self, rid, rx, drug, sec: _Sector, start, end, is_last: bool) -> None:
        horarios: list[str] = rx.get("horarios", [])
        prn = rx.get("se_necessario", False)
        freq_text = rx.get("frequencia") or ("se necessário" if prn else FREQ_TEXT.get(len(horarios), ""))
        text = f"{drug.name} {rx['dose']} {rx['via']} {freq_text}".strip()
        if horarios and not prn:
            text += f" ({', '.join(horarios)})"
        if prn and rx.get("condicao"):
            text += f" — {rx['condicao']}"

        stopped = rx.get("status") == "stopped" and is_last
        if stopped:
            status = "stopped"
        elif end is not None and end <= self.anchor:
            status = "completed"
        else:
            status = "active"

        bounds = {"start": iso(start)} | ({"end": iso(end)} if end else {})
        repeat: dict = {"boundsPeriod": bounds}
        if horarios and not prn:
            repeat |= {
                "frequency": len(horarios),
                "period": 1,
                "periodUnit": "d",
                "timeOfDay": [f"{h}:00" for h in horarios],
            }
        dose_value, _, dose_unit = rx["dose"].partition(" ")
        dosage = {
            "text": text,
            "timing": {"repeat": repeat},
            "asNeededBoolean": prn,
            "route": {"text": rx["via"]},
        }
        try:
            dosage["doseAndRate"] = [
                {"doseQuantity": {"value": float(dose_value.replace(",", ".")), "unit": dose_unit}}
            ]
        except ValueError:
            pass
        med_cc = {
            "coding": [
                {"system": codes.INGREDIENT, "code": drug.ingredient, "display": drug.name},
                {"system": codes.ATC, "code": drug.atc},
            ],
            "text": f"{drug.name} {rx['dose']}",
        }
        resource = {
            "resourceType": "MedicationRequest",
            "id": rid,
            "status": status,
            "intent": "order",
            "medicationCodeableConcept": med_cc,
            "subject": self.patient_ref,
            "encounter": ref("Encounter", sec.enc_id),
            "authoredOn": iso(start),
            "requester": {"display": rx.get("prescritor", "Equipe médica")},
            "dosageInstruction": [dosage],
        }
        if stopped:
            resource["statusReason"] = {"text": rx.get("motivo", "Suspenso pela equipe médica")}
        if rx.get("substitui") and rx["substitui"] in self._rx_last_id:
            resource["priorPrescription"] = ref("MedicationRequest", self._rx_last_id[rx["substitui"]])
        if rx.get("observacao"):
            resource["note"] = [{"text": rx["observacao"]}]
        self.out.append(resource)

        if self.ambulatory:
            return  # uso domiciliar: sem registro de administração pela enfermagem
        if prn or not horarios or rx.get("administrar") is False:
            for i, spec in enumerate(rx.get("administracoes", [])):
                when = self.t(spec)
                if start <= when < (end or self.anchor) and when <= self.anchor:
                    self._administration(f"{rid}-adm{i}", resource, sec, when, rx)
            return
        for when in self._scheduled_times(horarios, start, min(end, self.anchor) if end else self.anchor):
            self._administration(f"{rid}-adm-{when.astimezone(tz()).strftime('%m%d%H%M')}", resource, sec, when, rx)

    def _scheduled_times(self, horarios: list[str], start: datetime, until: datetime) -> list[datetime]:
        local_tz = tz()
        out = []
        day: date = start.astimezone(local_tz).date()
        last_day: date = until.astimezone(local_tz).date()
        while day <= last_day:
            for h in horarios:
                hh, mm = (int(x) for x in h.split(":"))
                when = datetime.combine(day, time(hh, mm), tzinfo=local_tz).astimezone(UTC)
                if start <= when < until:
                    out.append(when)
            day += timedelta(days=1)
        return out

    def _administration(self, aid: str, request: dict, sec: _Sector, when: datetime, rx: dict) -> None:
        self.out.append(
            {
                "resourceType": "MedicationAdministration",
                "id": aid,
                "status": "completed",
                "medicationCodeableConcept": request["medicationCodeableConcept"],
                "subject": self.patient_ref,
                "context": ref("Encounter", sec.enc_id),
                "effectiveDateTime": iso(when),
                "request": ref("MedicationRequest", request["id"]),
                "performer": [{"actor": {"display": "Equipe de enfermagem"}}],
                "dosage": {"text": f"{rx['dose']} {rx['via']}"},
            }
        )

    # ---------- observações ----------
    def _obs(
        self,
        oid: str,
        category: str,
        measure: codes.Measure,
        when: datetime,
        enc: dict,
        value=None,
        components=None,
        note: str | None = None,
        cat_display: str | None = None,
    ) -> dict:
        resource: dict = {
            "resourceType": "Observation",
            "id": oid,
            "status": "final",
            "category": [
                {"coding": [{"system": codes.OBS_CATEGORY, "code": category, "display": cat_display or category}]}
            ],
            "code": {
                "coding": [{"system": measure.system, "code": measure.code, "display": measure.label}],
                "text": measure.label,
            },
            "subject": self.patient_ref,
            "encounter": enc,
            "effectiveDateTime": iso(when),
        }
        if value is not None:
            resource["valueQuantity"] = {"value": value, "unit": measure.unit}
            level = codes.classify(measure, float(value))
            if level != "normal":
                high = (measure.high is not None and value > measure.high) or (
                    measure.crit_high is not None and value > measure.crit_high
                )
                code = ("HH" if high else "LL") if level == "critico" else ("H" if high else "L")
                resource["interpretation"] = [{"coding": [{"system": codes.INTERPRETATION, "code": code}]}]
            if measure.low is not None or measure.high is not None:
                rr: dict = {}
                if measure.low is not None:
                    rr["low"] = {"value": measure.low, "unit": measure.unit}
                if measure.high is not None:
                    rr["high"] = {"value": measure.high, "unit": measure.unit}
                resource["referenceRange"] = [rr]
        if components:
            resource["component"] = components
        if note:
            resource["note"] = [{"text": note}]
        return resource

    def _vitals(self) -> None:
        for i, sv in enumerate(self.s.get("sinais", [])):
            when = self.t(sv["t"])
            enc = self.enc_for(sv, when)
            if "pa" in sv:
                pas, pad = sv["pa"]
                comps = []
                for key, val in (("pas", pas), ("pad", pad)):
                    m = codes.VITALS[key]
                    comps.append(
                        {
                            "code": {"coding": [{"system": m.system, "code": m.code, "display": m.label}]},
                            "valueQuantity": {"value": val, "unit": m.unit},
                        }
                    )
                panel = codes.Measure("pa", codes.LOINC, codes.BP_PANEL[0], codes.BP_PANEL[1], "mmHg")
                self.out.append(
                    self._obs(
                        f"{self.sid}-sv{i}-pa",
                        "vital-signs",
                        panel,
                        when,
                        enc,
                        components=comps,
                        cat_display="Sinais vitais",
                    )
                )
            for key, measure in codes.VITALS.items():
                if key in ("pas", "pad") or key not in sv:
                    continue
                self.out.append(
                    self._obs(
                        f"{self.sid}-sv{i}-{key}",
                        "vital-signs",
                        measure,
                        when,
                        enc,
                        sv[key],
                        cat_display="Sinais vitais",
                    )
                )

    def _labs(self) -> None:
        for i, lab in enumerate(self.s.get("exames", [])):
            when = self.t(lab["t"])
            enc = self.enc_for(lab, when)
            for key, measure in codes.LABS.items():
                if key in lab:
                    self.out.append(
                        self._obs(
                            f"{self.sid}-lab{i}-{key}",
                            "laboratory",
                            measure,
                            when,
                            enc,
                            lab[key],
                            cat_display="Laboratório",
                        )
                    )

    def _io(self) -> None:
        for i, item in enumerate(self.s.get("balanco", [])):
            when = self.t(item["t"])
            self.out.append(
                self._obs(
                    f"{self.sid}-io{i}",
                    "exam",
                    codes.IO["diurese"],
                    when,
                    self.enc_for(item, when),
                    item["diurese"],
                    note=item.get("obs"),
                    cat_display="Balanço hídrico",
                )
            )

    def _scales(self) -> None:
        for i, item in enumerate(self.s.get("escalas", [])):
            when = self.t(item["t"])
            for key, measure in codes.SCALES.items():
                if key in item:
                    self.out.append(
                        self._obs(
                            f"{self.sid}-esc{i}-{key}",
                            "survey",
                            measure,
                            when,
                            self.enc_for(item, when),
                            item[key],
                            cat_display="Escala",
                        )
                    )

    def _procedures(self) -> None:
        for i, proc in enumerate(self.s.get("procedimentos", [])):
            when = self.t(proc["t"])
            resource = {
                "resourceType": "Procedure",
                "id": f"{self.sid}-proc{i}",
                "status": "completed",
                "category": {"text": proc.get("categoria", "procedimento")},
                "code": {"text": proc["texto"]},
                "subject": self.patient_ref,
                "encounter": self.enc_for(proc, when),
                "performedDateTime": iso(when),
            }
            if proc.get("obs"):
                resource["note"] = [{"text": proc["obs"]}]
            self.out.append(resource)

    def _surgeries(self) -> None:
        """Cirurgia agendada: ServiceRequest (com bodySite), Appointment da sala e Consent (termo assinado)."""
        for cx in self.s.get("cirurgias", []):
            when = self.t(cx["t"])
            end = when + parse_offset(cx.get("duracao", "2h"))
            sr_id = f"{self.sid}-sr-{cx['id']}"
            sr_ref = ref("ServiceRequest", sr_id, cx["procedimento"])
            dx = self.s.get("diagnosticos", [])
            sr: dict = {
                "resourceType": "ServiceRequest",
                "id": sr_id,
                "status": cx.get("status", "active"),
                "intent": "order",
                "category": [
                    {
                        "coding": [
                            {"system": codes.SNOMED, "code": codes.SR_SURGICAL[0], "display": codes.SR_SURGICAL[1]}
                        ],
                        "text": "Procedimento cirúrgico",
                    }
                ],
                "code": {"text": cx["procedimento"]},
                "subject": self.patient_ref,
                "encounter": ref("Encounter", self.main_enc_id),
                "occurrencePeriod": {"start": iso(when), "end": iso(end)},
                "authoredOn": iso(self.t(cx.get("solicitado", cx["t"]))),
                "requester": {"display": cx["cirurgiao"]},
                "bodySite": [{"text": cx["sitio"]}],
            }
            if dx:
                sr["reasonReference"] = [ref("Condition", f"{self.sid}-dx-0", dx[0]["texto"])]
            self.out.append(sr)
            if cx.get("sala"):
                in_room = when <= self.anchor < end
                status = "arrived" if in_room else ("booked" if self.anchor < when else "fulfilled")
                self.out.append(
                    {
                        "resourceType": "Appointment",
                        "id": f"{self.sid}-ag-{cx['id']}",
                        "status": status,
                        "serviceType": [{"text": cx.get("especialidade", "Centro cirúrgico")}],
                        "description": f"{cx['procedimento']} — {cx['sitio']}",
                        "start": iso(when),
                        "end": iso(end),
                        "basedOn": [sr_ref],
                        "participant": [
                            {"actor": self.patient_ref, "status": "accepted"},
                            {"actor": {"display": cx["cirurgiao"]}, "status": "accepted"},
                            {"actor": {"display": cx["sala"]}, "status": "accepted"},
                        ],
                    }
                )
            consent = cx.get("consentimento")
            if consent:
                self.out.append(
                    {
                        "resourceType": "Consent",
                        "id": f"{self.sid}-tcle-{cx['id']}",
                        "status": "active",
                        "scope": {"coding": [{"system": codes.CONSENT_SCOPE, "code": "treatment"}]},
                        "category": [
                            {
                                "coding": [
                                    {
                                        "system": codes.LOINC,
                                        "code": codes.CONSENT_LOINC[0],
                                        "display": codes.CONSENT_LOINC[1],
                                    }
                                ],
                                "text": "Termo de consentimento livre e esclarecido",
                            }
                        ],
                        "patient": self.patient_ref,
                        "dateTime": iso(self.t(consent["t"])),
                        "performer": [self.patient_ref],
                        "policyRule": {"text": "Termo de consentimento cirúrgico institucional"},
                        "sourceAttachment": {
                            "contentType": "text/plain; charset=utf-8",
                            "title": f"TCLE — {cx['procedimento']}",
                            "data": base64.b64encode(consent["texto"].encode("utf-8")).decode("ascii"),
                        },
                        "provision": {"type": "permit", "data": [{"meaning": "related", "reference": sr_ref}]},
                    }
                )

    def _notes(self) -> None:
        for i, ev in enumerate(self.s.get("evolucoes", [])):
            when = self.t(ev["t"])
            medical = ev.get("tipo", "medica") == "medica"
            code = codes.NOTE_MEDICAL if medical else codes.NOTE_NURSING
            label = "Evolução médica" if medical else "Evolução de enfermagem"
            self.out.append(
                {
                    "resourceType": "DocumentReference",
                    "id": f"{self.sid}-doc{i}",
                    "status": "current",
                    "type": {"coding": [{"system": codes.LOINC, "code": code, "display": label}], "text": label},
                    "subject": self.patient_ref,
                    "date": iso(when),
                    "author": [{"display": ev["autor"]}],
                    "description": label,
                    "content": [
                        {
                            "attachment": {
                                "contentType": "text/plain; charset=utf-8",
                                "data": base64.b64encode(ev["texto"].encode("utf-8")).decode("ascii"),
                                "title": label,
                            }
                        }
                    ],
                    "context": {"encounter": [self.enc_for(ev, when)]},
                }
            )


def build_all(scenarios: list[dict], anchor: datetime | None = None) -> list[dict]:
    """Constrói todos os recursos (Locations compartilhadas primeiro)."""
    anchor = (anchor or clock.now()).replace(second=0, microsecond=0)
    registry = LocationRegistry()
    resources: list[dict] = []
    for sc in scenarios:
        resources.extend(ScenarioBuilder(sc, anchor, registry).build())
    return list(registry.resources.values()) + resources
