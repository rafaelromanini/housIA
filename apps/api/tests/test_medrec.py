import pytest

from app.fhir_client.client import LocalFhirStore
from app.handoff.facts import compute_facts
from app.medrec.hypotheses import detect_symptoms
from app.medrec.rules import (
    allergy_findings,
    default_transition,
    duplicity_findings,
    high_alert_findings,
    interaction_findings,
    reconcile,
    run_rules,
)
from app.seed.builder import build_all
from app.timeline.events import build_timeline
from app.timeline.record import PatientRecord
from tests.conftest import ANCHOR  # noqa: E402

NOW = ANCHOR


def make_record(prescricoes: list[dict], alergias: list[dict] | None = None, **extra) -> PatientRecord:
    """Paciente mínimo com transferência UTI → Enfermaria há 10h."""
    scenario = {
        "id": "t",
        "paciente": {"nome": "Paciente Teste", "sexo": "female", "nascimento": "1950-01-01", "cpf": "000.000.000-00"},
        "internacao": {
            "inicio": "-3d",
            "motivo": "teste",
            "setores": [
                {"id": "uti", "setor": "UTI Adulto", "leito": "01", "inicio": "-3d", "fim": "-10h"},
                {"id": "enf", "setor": "Enfermaria 3B", "leito": "01", "inicio": "-10h"},
            ],
        },
        "alergias": alergias or [],
        "prescricoes": prescricoes,
        **extra,
    }
    store = LocalFhirStore.from_resources(build_all([scenario], anchor=NOW))
    rec = PatientRecord.load(store, "t")
    rec.now = NOW
    return rec


def rx(med: str, setores: list[str], **kw) -> dict:
    return {
        "id": med,
        "med": med,
        "dose": kw.pop("dose", "10 mg"),
        "via": "VO",
        "horarios": ["08:00"],
        "setores": setores,
        **kw,
    }


def statuses(rec: PatientRecord) -> dict[str, str]:
    origin, dest = default_transition(rec)
    return {r.principio_ativo: r.status for r in reconcile(rec, origin, dest)}


# ---------- regra: omissão / comparação origem x destino ----------
def test_omission_vs_kept_vs_new_vs_changed():
    rec = make_record(
        [
            rx("metoprolol", ["uti"]),
            rx("omeprazol", ["uti", "enf"]),
            rx("furosemida", ["uti"]),
            {**rx("furosemida", ["enf"]), "id": "furo2", "dose": "80 mg"},
            rx("enoxaparina", ["enf"]),
        ]
    )
    st = statuses(rec)
    assert st["metoprolol"] == "omitida"
    assert st["omeprazol"] == "mantida"
    assert st["furosemida"] == "alterada"
    assert st["enoxaparina"] == "nova"
    _, findings = run_rules(rec, *default_transition(rec))
    omission = [f for f in findings if f.tipo == "omissao"]
    assert len(omission) == 1 and omission[0].severidade == "critico"  # betabloqueador


def test_registered_stop_is_not_omission():
    rec = make_record([rx("omeprazol", ["uti"], fim="-10h", status="stopped", motivo="Sem indicação")])
    assert statuses(rec)["omeprazol"] == "suspensa"
    _, findings = run_rules(rec, *default_transition(rec))
    assert not [f for f in findings if f.tipo == "omissao"]


def test_replacement_by_prior_prescription_is_not_omission():
    rec = make_record(
        [
            rx("anlodipino", ["uti"]),
            rx("losartana", ["enf"], substitui="anlodipino"),
        ]
    )
    st = statuses(rec)
    assert st["anlodipino"] == "suspensa" and st["losartana"] == "nova"


def test_non_critical_class_omission_is_attention():
    rec = make_record([rx("omeprazol", ["uti"])])
    _, findings = run_rules(rec, *default_transition(rec))
    assert [f.severidade for f in findings if f.tipo == "omissao"] == ["atencao"]


# ---------- regra: duplicidade ----------
def test_duplicity_same_class_and_same_ingredient():
    rec = make_record(
        [rx("omeprazol", ["enf"]), rx("pantoprazol", ["enf"]), rx("dipirona", ["enf"]), rx("paracetamol", ["enf"])]
    )
    meds = [m for m in rec.medications if m.status == "active"]
    found = duplicity_findings(meds)
    assert len(found) == 1 and "omeprazol" in found[0].medicamentos  # analgésicos simples são exceção


# ---------- regra: interações ----------
@pytest.mark.parametrize(
    ("a", "b", "severity"),
    [
        ("morfina", "diazepam", "critico"),
        ("varfarina", "ibuprofeno", "critico"),
        ("enalapril", "espironolactona", "critico"),
        ("clopidogrel", "omeprazol", "atencao"),
    ],
)
def test_interactions_table(a, b, severity):
    rec = make_record([rx(a, ["enf"]), rx(b, ["enf"])])
    found = interaction_findings([m for m in rec.medications if m.status == "active"])
    assert [f.severidade for f in found] == [severity]


def test_no_interaction_for_unrelated_drugs():
    rec = make_record([rx("omeprazol", ["enf"]), rx("ceftriaxona", ["enf"])])
    assert interaction_findings(rec.medications) == []


# ---------- regra: alto alerta ----------
def test_high_alert_requires_double_check():
    rec = make_record([rx("insulina_nph", ["enf"]), rx("omeprazol", ["enf"])])
    found = high_alert_findings(rec.medications, new_ingredients={"insulina_nph"})
    assert [f.medicamentos for f in found] == [["insulina_nph"]]
    assert found[0].severidade == "atencao"


# ---------- regra: alergia ----------
def test_allergy_by_ingredient_class_and_cross_reactivity():
    rec = make_record(
        [rx("dipirona", ["enf"]), rx("amoxicilina", ["enf"]), rx("cefazolina", ["enf"])],
        alergias=[
            {"principio_ativo": "dipirona", "texto": "Dipirona", "reacao": "angioedema"},
            {"classe": "penicilina", "texto": "Penicilinas", "reacao": "urticária"},
        ],
    )
    found = allergy_findings(rec, [m for m in rec.medications if m.status == "active"])
    by_drug = {f.medicamentos[0]: (f.tipo, f.severidade) for f in found}
    assert by_drug["dipirona"] == ("alergia", "critico")
    assert by_drug["amoxicilina"] == ("alergia", "critico")
    assert by_drug["cefazolina"] == ("alergia_cruzada", "atencao")


# ---------- golden: cenários 2 e 5 ----------
def test_scenario2_metoprolol_omission_and_withdrawal_hypothesis(client, auth):
    h = auth("medico")
    res = client.post("/patients/cen2/medrec", json={}, headers=h)
    assert res.status_code == 200, res.text
    run = res.json()
    assert run["origem"]["setor"] == "UTI Adulto" and run["destino"]["setor"] == "Enfermaria 3B"
    rows = {r["principio_ativo"]: r["status"] for r in run["linhas"]}
    assert rows["metoprolol"] == "omitida"
    omission = [a for a in run["achados"] if a["tipo"] == "omissao"]
    assert omission and omission[0]["severidade"] == "critico" and omission[0]["alert_id"]
    hyp = [x for x in run["hipoteses"] if x["medicamento"] == "Metoprolol"]
    assert hyp and hyp[0]["mecanismo"] == "retirada" and hyp[0]["confidence"] in ("media", "alta")
    assert any(r.startswith("Observation/") for r in hyp[0]["refs"])


def test_scenario5_two_critical_deterministic_alerts(client, auth):
    h = auth("enfermagem")
    run = client.post(
        "/patients/cen5/medrec", json={"from": "Pronto-Socorro", "to": "Clínica Cirúrgica 4A"}, headers=h
    ).json()
    critical = [a for a in run["achados"] if a["severidade"] == "critico"]
    assert {a["tipo"] for a in critical} == {"alergia", "interacao"}
    assert len(critical) == 2
    assert any("Dipirona" in a["titulo"] for a in critical)
    assert any("Morfina" in a["titulo"] and "Diazepam" in a["titulo"] for a in critical)
    alerts = client.get("/patients/cen5/alerts?status=aberto", headers=h).json()
    assert sum(a["severity"] == "critico" and a["source"] == "medrec" for a in alerts) == 2
    latest = client.get("/patients/cen5/medrec/latest", headers=h).json()
    assert latest["id"] == run["id"]


def test_symptom_detection_ignores_allergy_history(record):
    rec = record("cen5")
    signs = {s.sinal for s in detect_symptoms(build_timeline(rec), rec.now)}
    assert {"sedacao", "bradipneia", "dessaturacao"} <= signs
    assert "prurido" not in signs  # "alergia a dipirona (urticária)" é antecedente, não sinal novo


def test_handoff_facts_include_medication_safety(record):
    rec = record("cen5")
    facts = compute_facts(rec, build_timeline(rec), rec.now, 12)
    assert {"alergia", "interacao"} <= {f.tipo for f in facts if f.severidade == "critico"}
