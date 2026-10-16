"""Acesso de leitura à fonte clínica FHIR R4.

O HousIA só LÊ do FHIR; nada é escrito de volta no prontuário (regra de ouro do MVP).
Dois backends com a mesma interface:
- HapiFhirClient: servidor FHIR real (HAPI no docker compose)
- LocalFhirStore: os mesmos cenários montados em memória (testes/dev sem Docker; ADR 0001)
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.config import get_settings

# Tipos de recurso lidos por paciente e o parâmetro de busca que aponta para o paciente
PATIENT_SCOPED: dict[str, str] = {
    "Encounter": "subject",
    "Condition": "subject",
    "AllergyIntolerance": "patient",
    "MedicationRequest": "subject",
    "MedicationAdministration": "subject",
    "Observation": "subject",
    "Procedure": "subject",
    "DocumentReference": "subject",
    "RelatedPerson": "patient",
    "ServiceRequest": "subject",
    "Appointment": "patient",
    "Consent": "patient",
}


def refers_to_patient(resource: dict, param: str, target: str) -> bool:
    """Busca local equivalente ao parâmetro FHIR (Appointment aponta o paciente em participant.actor)."""
    if (resource.get(param) or {}).get("reference") == target:
        return True
    return resource.get("resourceType") == "Appointment" and any(
        (p.get("actor") or {}).get("reference") == target for p in resource.get("participant", [])
    )


class FhirUnavailable(RuntimeError):
    pass


class FhirSource(Protocol):
    def list_patients(self) -> list[dict]: ...

    def get(self, rtype: str, rid: str) -> dict | None: ...

    def patient_resources(self, patient_id: str) -> dict[str, list[dict]]: ...

    def active_encounters(self) -> list[dict]: ...

    def ambulatory_encounters(self) -> list[dict]: ...

    def create(self, resource: dict) -> dict: ...

    def locations(self) -> dict[str, dict]: ...


class HapiFhirClient:
    def __init__(self, base_url: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, headers={"Accept": "application/fhir+json"})

    def _search(self, rtype: str, params: dict[str, str]) -> list[dict]:
        params = {"_count": "1000", **params}
        url: str | None = f"{self.base_url}/{rtype}"
        out: list[dict] = []
        try:
            while url:
                r = self._client.get(url, params=params)
                r.raise_for_status()
                bundle = r.json()
                out.extend(e["resource"] for e in bundle.get("entry", []) if "resource" in e)
                url = next((link["url"] for link in bundle.get("link", []) if link.get("relation") == "next"), None)
                params = {}
        except httpx.HTTPError as exc:
            raise FhirUnavailable(f"Servidor FHIR indisponível: {exc}") from exc
        return out

    def list_patients(self) -> list[dict]:
        return self._search("Patient", {})

    def get(self, rtype: str, rid: str) -> dict | None:
        try:
            r = self._client.get(f"{self.base_url}/{rtype}/{rid}")
        except httpx.HTTPError as exc:
            raise FhirUnavailable(f"Servidor FHIR indisponível: {exc}") from exc
        if r.status_code in (404, 410):
            return None
        r.raise_for_status()
        return r.json()

    def patient_resources(self, patient_id: str) -> dict[str, list[dict]]:
        return {rtype: self._search(rtype, {param: f"Patient/{patient_id}"}) for rtype, param in PATIENT_SCOPED.items()}

    def active_encounters(self) -> list[dict]:
        return self._search("Encounter", {"status": "in-progress"})

    def ambulatory_encounters(self) -> list[dict]:
        return self._search("Encounter", {"class": "AMB"})

    def create(self, resource: dict) -> dict:
        """Única escrita no prontuário: nota revisada e aprovada pelo profissional (EHR_WRITEBACK=true)."""
        try:
            r = self._client.post(
                f"{self.base_url}/{resource['resourceType']}",
                json=resource,
                headers={"Content-Type": "application/fhir+json"},
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise FhirUnavailable(f"Servidor FHIR indisponível: {exc}") from exc
        return r.json()

    def locations(self) -> dict[str, dict]:
        return {loc["id"]: loc for loc in self._search("Location", {})}


@dataclass
class LocalFhirStore:
    resources: dict[str, dict[str, dict]] = field(default_factory=lambda: defaultdict(dict))

    @classmethod
    def from_resources(cls, resources: list[dict]) -> LocalFhirStore:
        store = cls()
        for r in resources:
            store.resources[r["resourceType"]][r["id"]] = r
        return store

    def list_patients(self) -> list[dict]:
        return list(self.resources["Patient"].values())

    def get(self, rtype: str, rid: str) -> dict | None:
        return self.resources.get(rtype, {}).get(rid)

    def patient_resources(self, patient_id: str) -> dict[str, list[dict]]:
        target = f"Patient/{patient_id}"
        return {
            rtype: [r for r in self.resources.get(rtype, {}).values() if refers_to_patient(r, param, target)]
            for rtype, param in PATIENT_SCOPED.items()
        }

    def active_encounters(self) -> list[dict]:
        return [e for e in self.resources["Encounter"].values() if e.get("status") == "in-progress"]

    def ambulatory_encounters(self) -> list[dict]:
        return [e for e in self.resources["Encounter"].values() if e.get("class", {}).get("code") == "AMB"]

    def create(self, resource: dict) -> dict:
        rid = resource.get("id") or f"housia-{len(self.resources[resource['resourceType']]) + 1}"
        stored = resource | {"id": rid}
        self.resources[resource["resourceType"]][rid] = stored
        return stored

    def locations(self) -> dict[str, dict]:
        return dict(self.resources["Location"])


_lock = threading.Lock()
_source: FhirSource | None = None


def get_fhir() -> FhirSource:
    global _source
    with _lock:
        if _source is None:
            settings = get_settings()
            if settings.fhir_mode == "hapi":
                _source = HapiFhirClient(settings.fhir_base_url)
            else:
                from app.seed.scenarios import build_demo_resources

                _source = LocalFhirStore.from_resources(build_demo_resources())
        return _source


def set_fhir(source: FhirSource | None) -> None:
    """Permite injetar uma fonte (testes) ou forçar recarga (None)."""
    global _source
    with _lock:
        _source = source
