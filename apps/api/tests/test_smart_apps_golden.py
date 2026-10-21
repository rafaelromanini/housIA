"""Golden do cen6: a saída das regras sobre o roteiro inteiro não muda com a migração dos Smart Apps para o registro.

O registro (`app/consult/apps`) lê os Insights que o engine produz; ele não os altera. O campo `apps` é a única
adição do contrato (preenchido só pelo serviço, nunca pelo engine) e fica fora da comparação.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app import clock
from app.consult.engine import Engine
from app.consult.knowledge import demo_scripts
from app.consult.schemas import Segment
from app.fhir_client.client import LocalFhirStore
from app.seed.scenarios import build_demo_resources
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from app.timeline.record import PatientRecord

GOLDEN = Path(__file__).parent / "golden" / "cen6_insights.json"
# O golden guarda datas absolutas ("21/07/2026", "há 11 anos"): o cen6 é gerado sempre sobre esta âncora fixa
# (a data em que foi gravado), não sobre o dia em que o teste roda. Regravar: UPDATE_GOLDEN=1 (mantém esta âncora).
GOLDEN_ANCHOR = datetime(2026, 10, 20, 15, 0, tzinfo=ZoneInfo("America/Sao_Paulo")).astimezone(UTC)


def cen6_insights() -> dict:
    previous = clock.now()
    clock.freeze(GOLDEN_ANCHOR + timedelta(minutes=1))
    try:
        return _cen6_insights()
    finally:
        clock.freeze(previous)


def _cen6_insights() -> dict:
    # prontuário próprio: outros testes gravam no store da sessão, e o golden precisa do cen6 como o seed o cria
    rec = PatientRecord.load(LocalFhirStore.from_resources(build_demo_resources(anchor=GOLDEN_ANCHOR)), "cen6")
    assert rec is not None
    rec.now = GOLDEN_ANCHOR
    falas = demo_scripts()["cen6_consulta"]["falas"]
    segs = [
        Segment(n=i, t=float(w.get("t", i)), falante=w["falante"], texto=w["texto"]) for i, w in enumerate(falas, 1)
    ]
    engine = Engine(rec, build_timeline(rec), build_context(rec), segs, "clinica_medica", "consulta", clock.now())
    data = engine.run().model_dump(mode="json")
    data.pop("apps", None)
    data.pop("regras_ms", None)
    return data


def test_cen6_golden_does_not_change():
    data = cen6_insights()
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert json.loads(json.dumps(data, sort_keys=True)) == expected
