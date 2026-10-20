import argparse
import os
import sys
import time
from collections import Counter

import httpx

from app.fhir_client.client import PATIENT_SCOPED
from app.seed.scenarios import build_demo_resources

BATCH = 400


def wait_for_fhir(base_url: str, timeout_s: int = 600) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            r = httpx.get(f"{base_url}/metadata", timeout=5)
            if r.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"FHIR não respondeu em {timeout_s}s: {base_url}")
        print("Aguardando HAPI FHIR...", flush=True)
        time.sleep(5)


def to_transaction(resources: list[dict]) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {"resource": r, "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}} for r in resources
        ],
    }


def load(base_url: str, resources: list[dict]) -> None:
    # Locations primeiro (são referenciadas pelos Encounters); depois em lotes.
    resources = sorted(resources, key=lambda r: r["resourceType"] != "Location")
    with httpx.Client(timeout=120, headers={"Content-Type": "application/fhir+json"}) as client:
        for i in range(0, len(resources), BATCH):
            chunk = resources[i : i + BATCH]
            r = client.post(base_url, json=to_transaction(chunk))
            if r.status_code >= 400:
                raise RuntimeError(f"Falha ao carregar lote {i // BATCH + 1}: {r.status_code} {r.text[:800]}")
            print(f"  lote {i // BATCH + 1}: {len(chunk)} recursos", flush=True)


def _existing_ids(client: httpx.Client, base_url: str, rtype: str, param: str, patient_id: str) -> set[str]:
    url: str | None = f"{base_url}/{rtype}"
    params: dict = {param: f"Patient/{patient_id}", "_elements": "id", "_count": "1000"}
    ids: set[str] = set()
    while url:
        r = client.get(url, params=params)
        r.raise_for_status()
        bundle = r.json()
        ids |= {e["resource"]["id"] for e in bundle.get("entry", []) if "resource" in e}
        url = next((link["url"] for link in bundle.get("link", []) if link.get("relation") == "next"), None)
        params = {}
    return ids


def remove_stale(base_url: str, resources: list[dict]) -> int:
    """Apaga recursos dos pacientes da demo que não existem mais na nova carga.

    Ex.: administrações geradas pelo aprazamento têm o horário no id; ao reancorar, as antigas sobrariam.
    """
    wanted = {(r["resourceType"], r["id"]) for r in resources}
    patients = [r["id"] for r in resources if r["resourceType"] == "Patient"]
    deletes = []
    with httpx.Client(timeout=60, headers={"Accept": "application/fhir+json"}) as client:
        for pid in patients:
            for rtype, param in PATIENT_SCOPED.items():
                for rid in _existing_ids(client, base_url, rtype, param, pid):
                    if (rtype, rid) not in wanted:
                        deletes.append({"request": {"method": "DELETE", "url": f"{rtype}/{rid}"}})
        for i in range(0, len(deletes), BATCH):
            bundle = {"resourceType": "Bundle", "type": "transaction", "entry": deletes[i : i + BATCH]}
            r = client.post(base_url, json=bundle, headers={"Content-Type": "application/fhir+json"})
            if r.status_code >= 400:
                raise RuntimeError(f"Falha ao remover recursos obsoletos: {r.status_code} {r.text[:500]}")
    return len(deletes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Carga dos cenários sintéticos no HAPI FHIR")
    parser.add_argument("--fhir-url", default=os.environ.get("FHIR_BASE_URL", "http://localhost:8080/fhir"))
    parser.add_argument("--wait", action="store_true", help="espera o servidor FHIR ficar disponível")
    parser.add_argument("--sem-fundo", action="store_true", help="carrega só os 5 cenários roteirizados")
    args = parser.parse_args(argv)
    if args.wait:
        wait_for_fhir(args.fhir_url)
    resources = build_demo_resources(include_background=not args.sem_fundo)
    counts = Counter(r["resourceType"] for r in resources)
    print(f"Carregando {len(resources)} recursos em {args.fhir_url}: {dict(counts)}", flush=True)
    load(args.fhir_url, resources)
    removed = remove_stale(args.fhir_url, resources)
    if removed:
        print(f"Removidos {removed} recursos obsoletos de cargas anteriores.", flush=True)
    print("Carga concluída. Horários ancorados no momento atual.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
