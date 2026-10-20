"""Carrega os cenários de demonstração (data/scenarios) e a população de fundo no HAPI FHIR.

Uso:
    python data/seed/load.py [--wait] [--fhir-url http://localhost:8080/fhir]

Idempotente: cada recurso é gravado com PUT no seu id fixo, então rodar de novo
apenas reancora os horários no "agora" (útil antes de cada demo).
"""
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[2] / "apps" / "api"
if API_DIR.exists():
    sys.path.insert(0, str(API_DIR))

from app.seed.loader import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
