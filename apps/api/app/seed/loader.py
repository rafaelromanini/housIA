import argparse
import os
import sys
import time

import httpx


def wait_for_fhir(base_url: str, timeout_s: int = 300) -> None:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Carga de dados sintéticos no HAPI FHIR")
    parser.add_argument("--fhir-url", default=os.environ.get("FHIR_BASE_URL", "http://localhost:8080/fhir"))
    parser.add_argument("--wait", action="store_true", help="espera o servidor FHIR ficar disponível")
    args = parser.parse_args(argv)
    if args.wait:
        wait_for_fhir(args.fhir_url)
    print("Nenhum cenário para carregar ainda (fase 0).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
