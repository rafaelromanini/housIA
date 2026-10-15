import argparse
import os
import sys
import time
from collections import Counter

import httpx

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
    print("Carga concluída. Horários ancorados no momento atual.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
