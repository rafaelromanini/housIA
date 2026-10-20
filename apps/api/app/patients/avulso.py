"""Paciente avulso (#34): cadastro no próprio HousIA para usar a consulta sem prontuário integrado.

O paciente nasce no FHIR (HAPI ou LocalFhirStore) como Patient, AllergyIntolerance e MedicationStatement. O id
carrega a organização (`av<org>-<hex>`): outra organização enxerga 404, nunca 403, como nas demais tabelas com dono.
"""

from __future__ import annotations

import re
import uuid
from datetime import date

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.auth.deps import _bearer, get_current_user
from app.db.models import User
from app.db.session import get_db
from app.fhir_client import codes
from app.fhir_client.client import FhirSource

ID_RE = re.compile(r"^av(\d+)-[0-9a-f]{12}$")
TAG = "avulso"
CLINICAL = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
VERIFICATION = "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification"
GENDER = {"M": "male", "F": "female", "O": "other"}


class AlergiaIn(BaseModel):
    substancia: str = Field(min_length=2, max_length=80)
    reacao: str = Field("", max_length=120)
    criticidade: str = Field("indeterminada", pattern="^(alta|baixa|indeterminada)$")

    @field_validator("substancia", "reacao")
    @classmethod
    def _limpa(cls, v: str) -> str:
        return " ".join(v.split())


class MedicamentoIn(BaseModel):
    nome: str = Field(min_length=2, max_length=80)
    posologia: str = Field("", max_length=120)  # texto livre: "500 mg, 8/8 h"
    via: str = Field("", max_length=40)

    @field_validator("nome", "posologia", "via")
    @classmethod
    def _limpa(cls, v: str) -> str:
        return " ".join(v.split())


class PacienteIn(BaseModel):
    nome: str = Field(min_length=3, max_length=120)
    nascimento: date
    sexo: str = Field(pattern="^[MFO]$")
    telefone: str = Field("", max_length=30)
    idioma: str = Field("", max_length=10)
    alergias: list[AlergiaIn] = Field(default_factory=list, max_length=30)
    medicamentos: list[MedicamentoIn] = Field(default_factory=list, max_length=60)
    sem_alergias: bool = False  # "nega alergias conhecidas" (diferente de nunca ter perguntado)

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v.split()) < 2:
            raise ValueError("Informe nome e sobrenome")
        return v

    @field_validator("nascimento")
    @classmethod
    def _nascimento(cls, v: date) -> date:
        if v > clock.now().date():
            raise ValueError("Data de nascimento no futuro")
        if v.year < 1900:
            raise ValueError("Data de nascimento inválida")
        return v


def is_avulso(patient_id: str) -> bool:
    return ID_RE.match(patient_id) is not None


def owner_org(patient_id: str) -> int | None:
    m = ID_RE.match(patient_id)
    return int(m.group(1)) if m else None


def visible(patient_id: str, org_id: int | None) -> bool:
    """Paciente avulso de outra organização não aparece em varreduras do FHIR (jornada, gestão)."""
    owner = owner_org(patient_id)
    return owner is None or org_id is None or owner == org_id


def guard_path_patient(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> None:
    """Dependência global: rota com `{patient_id}` de paciente avulso só abre para a organização dona dele."""
    pid = request.path_params.get("patient_id")
    org = owner_org(pid) if isinstance(pid, str) else None
    if org is None:
        return
    user = get_current_user(request, creds, db)
    if user.organization_id != org:
        raise HTTPException(404, "Paciente não encontrado")


def _ref(rtype: str, rid: str) -> dict:
    return {"reference": f"{rtype}/{rid}"}


def _allergy(pid: str, i: int, a: AlergiaIn, when: str) -> dict:
    resource = {
        "resourceType": "AllergyIntolerance",
        "id": f"{pid}-alg-{i}",
        "clinicalStatus": {"coding": [{"system": CLINICAL, "code": "active"}]},
        "verificationStatus": {"coding": [{"system": VERIFICATION, "code": "unconfirmed"}]},
        "code": {"text": a.substancia},
        "patient": _ref("Patient", pid),
        "recordedDate": when,
    }
    if a.criticidade != "indeterminada":
        resource["criticality"] = "high" if a.criticidade == "alta" else "low"
    if a.reacao:
        resource["reaction"] = [{"manifestation": [{"text": a.reacao}]}]
    return resource


def _statement(pid: str, i: int, m: MedicamentoIn, when: str) -> dict:
    resource = {
        "resourceType": "MedicationStatement",
        "id": f"{pid}-med-{i}",
        "status": "active",
        "medicationCodeableConcept": {"text": m.nome},
        "subject": _ref("Patient", pid),
        "dateAsserted": when,
        "informationSource": _ref("Patient", pid),
    }
    if m.posologia or m.via:
        dose = {"text": f"{m.nome} {m.posologia}".strip()}
        if m.via:
            dose["route"] = {"text": m.via}
        resource["dosage"] = [dose]
    return resource


def create(db: Session, fhir: FhirSource, user: User, body: PacienteIn) -> dict:
    """Grava com PUT: o id é do HousIA (carrega a organização) e o HAPI aceita id definido pelo cliente."""
    pid = f"av{user.organization_id}-{uuid.uuid4().hex[:12]}"
    now = clock.now().isoformat()
    parts = body.nome.split()
    patient: dict = {
        "resourceType": "Patient",
        "id": pid,
        "meta": {"tag": [{"system": codes.TAGS, "code": TAG, "display": "Paciente avulso (cadastro no HousIA)"}]},
        "name": [{"use": "official", "text": body.nome, "family": parts[-1], "given": parts[:-1]}],
        "gender": GENDER[body.sexo],
        "birthDate": body.nascimento.isoformat(),
        "telecom": [{"system": "phone", "value": body.telefone}] if body.telefone else [],
    }
    if body.idioma:
        patient["communication"] = [
            {"language": {"coding": [{"system": "urn:ietf:bcp:47", "code": body.idioma}]}, "preferred": True}
        ]
    created = [fhir.update(patient)]
    for i, a in enumerate(body.alergias):
        created.append(fhir.update(_allergy(pid, i, a, now)))
    if body.sem_alergias and not body.alergias:
        created.append(
            fhir.update(
                {
                    "resourceType": "AllergyIntolerance",
                    "id": f"{pid}-alg-nka",
                    "clinicalStatus": {"coding": [{"system": CLINICAL, "code": "active"}]},
                    "verificationStatus": {"coding": [{"system": VERIFICATION, "code": "confirmed"}]},
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
                    "patient": _ref("Patient", pid),
                    "recordedDate": now,
                }
            )
        )
    for i, m in enumerate(body.medicamentos):
        created.append(fhir.update(_statement(pid, i, m, now)))
    log_action(
        db,
        user,
        "paciente.avulso_criado",
        "patient",
        pid,
        {"alergias": len(body.alergias), "medicamentos": len(body.medicamentos)},  # sem nome nem dado clínico
    )
    return summary(created[0], alergias=len(body.alergias), medicamentos=len(body.medicamentos))


def name_of(patient: dict) -> str:
    n = (patient.get("name") or [{}])[0]
    return n.get("text") or " ".join([*n.get("given", []), n.get("family", "")]).strip()


def summary(patient: dict, **extra) -> dict:
    return {
        "id": patient["id"],
        "nome": name_of(patient),
        "nascimento": patient.get("birthDate"),
        "sexo": {"male": "M", "female": "F"}.get(patient.get("gender", ""), "O"),
        "avulso": True,
    } | extra


def listing(fhir: FhirSource, org_id: int, q: str = "") -> list[dict]:
    """Pacientes avulsos da organização (mais recentes primeiro), com busca por nome."""
    needle = q.strip().lower()
    rows = [
        summary(p)
        for p in fhir.list_patients()
        if owner_org(p["id"]) == org_id and (not needle or needle in name_of(p).lower())
    ]
    return sorted(rows, key=lambda r: r["nome"].lower())
