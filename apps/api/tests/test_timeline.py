from app.fhir_client import codes
from app.timeline.events import KINDS, build_timeline

SCENARIOS = ["cen1", "cen2", "cen3", "cen4", "cen5"]


def test_resources_are_valid_fhir_ids(store):
    import re

    pattern = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
    for rtype, items in store.resources.items():
        for rid in items:
            assert pattern.match(rid), f"{rtype}/{rid}"


def test_every_scenario_timeline_is_ordered_and_sourced(record):
    for pid in SCENARIOS:
        rec = record(pid)
        events = build_timeline(rec)
        assert events, pid
        timestamps = [e.timestamp for e in events]
        assert timestamps == sorted(timestamps), f"{pid}: timeline fora de ordem"
        for e in events:
            assert e.source_ref and "/" in e.source_ref, e
            assert e.kind in KINDS
            rtype, rid = e.source_ref.split("/", 1)
            assert rid.split("#")[0] in {r["id"] for r in rec.resources.get(rtype, [])} or rtype == "Encounter"


def test_cen1_transfer_and_bp_trend(record):
    rec = record("cen1")
    assert rec.current_sector.ward == "Enfermaria 3B"
    assert [s.ward for s in rec.sectors] == ["Pronto-Socorro", "UTI Adulto", "Enfermaria 3B"]
    events = build_timeline(rec)
    transfers = [e for e in events if e.kind == "transfer"]
    assert transfers[-1].summary.startswith("Transferência de UTI Adulto para Enfermaria 3B")
    bp = [e for e in events if e.value and e.value.get("chave") == "pa"]
    assert bp[-1].value["sistolica"] == 184
    assert bp[-1].severity == "critico"
    stop = [e for e in events if e.id.endswith("#suspensao") and "Anlodipino" in e.summary]
    assert stop, "suspensão do anlodipino deve aparecer na timeline"


def test_cen2_metoprolol_only_in_icu(record):
    rec = record("cen2")
    meto = [m for m in rec.medications if m.ingredient == "metoprolol"]
    assert len(meto) == 1
    assert meto[0].encounter_id == "cen2-int-uti"
    assert meto[0].status == "completed"


def test_measure_classification():
    assert codes.classify(codes.VITALS["fc"], 128) == "atencao"
    assert codes.classify(codes.VITALS["fc"], 135) == "critico"
    assert codes.classify(codes.VITALS["spo2"], 91) == "atencao"
    assert codes.classify(codes.VITALS["spo2"], 88) == "critico"
    assert codes.classify(codes.LABS["potassio"], 4.2) == "normal"


def test_patients_endpoints(client, auth):
    h = auth("enfermagem")
    panel = client.get("/patients", headers=h)
    assert panel.status_code == 200
    rows = panel.json()
    ids = {r["id"] for r in rows}
    assert set(SCENARIOS) <= ids
    assert len(rows) >= 20
    cen1 = next(r for r in rows if r["id"] == "cen1")
    assert cen1["setor"] == "Enfermaria 3B" and cen1["leito"] == "12"
    assert cen1["dias_internacao"] == 3

    detail = client.get("/patients/cen5", headers=h).json()
    assert detail["contexto"]["alergias"][0]["summary"].startswith("Dipirona")
    assert detail["cenario"]["id"] == "cen5"

    tl = client.get("/patients/cen1/timeline?kinds=vital,io", headers=h).json()
    assert tl and {e["kind"] for e in tl} <= {"vital", "io"}
    assert client.get("/patients/nao-existe", headers=h).status_code == 404


def test_requires_auth(client):
    assert client.get("/patients").status_code == 401
