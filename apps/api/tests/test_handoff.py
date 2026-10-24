from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db import session as db_session
from app.db.models import Alert, AuditLog, HandoffReport
from app.handoff.schemas import HandoffOutput
from app.handoff.validation import validate_refs
from app.llm import client as llm_client
from app.llm.pseudonymize import Pseudonymizer
from app.llm.schema import strict_schema
from app.timeline.events import build_timeline


# ---------- pseudonimização ----------
def test_pseudonymizer_removes_direct_identifiers(record):
    rec = record("cen1")
    p = Pseudonymizer(**rec.identifiers_for_pseudonymization())
    text = (
        "Sr. Antônio Carlos Ribeiro (CPF 412.587.963-20), tel (11) 98432-1177, mora na "
        "Rua das Acácias, 120, Vila Mariana, São Paulo - SP. Esposa Maria Helena Ribeiro acompanha. "
        "Antonio refere cefaleia. PA 184/102 mmHg às 19:03, ceftriaxona 2 g."
    )
    out = p.text(text)
    for leaked in ("Antônio", "Antonio", "Ribeiro", "412.587.963-20", "98432-1177", "Acácias", "Maria Helena"):
        assert leaked not in out, leaked
    assert "[PACIENTE]" in out and "[CPF]" in out and "[TELEFONE]" in out and "[ENDERECO]" in out
    assert "[FAMILIAR]" in out
    # conteúdo clínico preservado
    assert "PA 184/102 mmHg às 19:03" in out and "ceftriaxona 2 g" in out


def test_pseudonymizer_generic_patterns_and_nested_data():
    p = Pseudonymizer()
    data = {"a": ["CPF 123.456.789-00", {"b": "ligar (21) 99876-5432 ou x@y.com"}], "n": 3}
    out = p.apply(data)
    assert out["a"][0] == "CPF [CPF]"
    assert "[TELEFONE]" in out["a"][1]["b"] and "[EMAIL]" in out["a"][1]["b"]
    assert out["n"] == 3


# ---------- validação de refs ----------
def _output(**kw) -> HandoffOutput:
    base = dict(
        headline="h",
        situacao="s",
        situacao_refs=["Encounter/x", "Observation/fake"],
        mudancas_no_periodo=[],
        riscos_proximas_horas=[],
        pendencias=[],
        perguntas_para_quem_sai=[],
    )
    return HandoffOutput.model_validate(base | kw)


def test_validate_refs_drops_items_with_invalid_or_missing_refs():
    out = _output(
        mudancas_no_periodo=[{"texto": "ok", "refs": ["Observation/a"]}, {"texto": "sem fonte", "refs": []}],
        riscos_proximas_horas=[
            {"texto": "inventado", "horizonte_h": 2, "severidade": "critico", "refs": ["Observation/a", "Obs/zz"]},
            {"texto": "valido", "horizonte_h": 2, "severidade": "atencao", "refs": ["Observation/a"]},
        ],
        pendencias=[{"texto": "p", "horario": "20:00", "refs": ["MedicationRequest/m"]}],
    )
    clean, dropped = validate_refs(out, {"Observation/a", "MedicationRequest/m", "Encounter/x"})
    assert [i.texto for i in clean.mudancas_no_periodo] == ["ok"]
    assert [i.texto for i in clean.riscos_proximas_horas] == ["valido"]
    assert len(clean.pendencias) == 1
    assert clean.situacao_refs == ["Encounter/x"]
    assert {d["texto"] for d in dropped} >= {"sem fonte", "inventado"}
    assert any(d["refs_invalidas"] == ["Obs/zz"] for d in dropped)


def test_strict_schema_is_closed():
    schema = strict_schema(HandoffOutput)

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            assert "$ref" not in node and "minimum" not in node
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)

    check(schema)


# ---------- wrapper do LLM ----------
def _fake_response(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


class FakeAnthropic:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


VALID = HandoffOutput(
    headline="h",
    situacao="s",
    situacao_refs=[],
    mudancas_no_periodo=[],
    riscos_proximas_horas=[],
    pendencias=[],
    perguntas_para_quem_sai=[],
).model_dump_json()


@pytest.fixture
def real_mode(monkeypatch):
    settings = llm_client.get_settings()
    monkeypatch.setattr(settings, "llm_mode", "real")
    yield
    monkeypatch.setattr(llm_client, "_client", None)


def test_llm_repairs_invalid_json_once(monkeypatch, real_mode):
    fake = FakeAnthropic([_fake_response('{"headline": "faltando campos"}'), _fake_response(VALID)])
    monkeypatch.setattr(llm_client, "_client", fake)
    result = llm_client.generate_structured("handoff", {"publico": "medico"}, HandoffOutput, mock=lambda v: {})
    assert result.repaired and result.mode == "real"
    assert len(fake.calls) == 2
    first = fake.calls[0]
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert first["fallbacks"] == "default"
    assert "temperature" not in first


def test_llm_gives_friendly_error_after_second_invalid(monkeypatch, real_mode):
    fake = FakeAnthropic([_fake_response("{}"), _fake_response("{}")])
    monkeypatch.setattr(llm_client, "_client", fake)
    with pytest.raises(llm_client.LLMError, match="formato inválido"):
        llm_client.generate_structured("handoff", {}, HandoffOutput, mock=lambda v: {})


def test_llm_refusal_is_handled(monkeypatch, real_mode):
    fake = FakeAnthropic([_fake_response("", stop_reason="refusal")])
    monkeypatch.setattr(llm_client, "_client", fake)
    with pytest.raises(llm_client.LLMError, match="recusa"):
        llm_client.generate_structured("handoff", {}, HandoffOutput, mock=lambda v: {})


def test_prompt_has_version():
    prompt = llm_client.load_prompt("handoff")
    assert prompt.version == "1.1.0"
    assert "$eventos" in prompt.user and "refs" in prompt.system


# ---------- golden: cenário 1 em modo mock ----------
def _all_refs(content: dict) -> list[str]:
    refs = list(content["situacao_refs"])
    for section in ("mudancas_no_periodo", "riscos_proximas_horas", "pendencias"):
        for item in content[section]:
            assert item["refs"], f"item sem ref em {section}: {item}"
            refs += item["refs"]
    return refs


def test_cen1_handoff_differs_by_audience_and_cites_valid_sources(client, auth, record):
    h = auth("medico")
    med = client.post("/patients/cen1/handoff", json={"audience": "medico"}, headers=h)
    enf = client.post("/patients/cen1/handoff", json={"audience": "enfermagem"}, headers=h)
    assert med.status_code == 200 and enf.status_code == 200
    m, e = med.json()["content"], enf.json()["content"]
    assert m != e

    # médico: risco de pico hipertensivo (PA em ascensão, crítico)
    assert any(
        r["severidade"] == "critico" and "PA sistólica em ascensão" in r["texto"] for r in m["riscos_proximas_horas"]
    )
    assert any("exame" in r["texto"].lower() or "Hemoglobina" in r["texto"] for r in m["riscos_proximas_horas"]) or any(
        "Leucócitos" in c["texto"] for c in m["mudancas_no_periodo"]
    )
    # enfermagem: horário do ATB e acesso venoso
    assert any("Ceftriaxona" in p["texto"] and p["horario"] == "20:00" for p in e["pendencias"])
    assert any("Acesso venoso" in r["texto"] for r in e["riscos_proximas_horas"])
    assert not any("Acesso venoso" in r["texto"] for r in m["riscos_proximas_horas"])

    # 100% dos itens exibidos têm ref válida (existente no prontuário)
    rec = record("cen1")
    known = {ev.source_ref for ev in build_timeline(rec)}
    known |= {f"Condition/{c['id']}" for c in rec.resources["Condition"]}
    known |= {f"MedicationRequest/{c['id']}" for c in rec.resources["MedicationRequest"]}
    for content in (m, e):
        for ref in _all_refs(content):
            assert ref in known, ref
        assert content["meta"]["modo"] == "mock"
        assert "Antônio" not in str(content) and "Ribeiro" not in str(content)


def test_handoff_cache_force_alerts_and_audit(client, auth):
    h = auth("enfermagem")
    first = client.post("/patients/cen1/handoff", json={"audience": "enfermagem"}, headers=h).json()
    second = client.post("/patients/cen1/handoff", json={"audience": "enfermagem"}, headers=h).json()
    assert not first["cached"] and second["cached"] and first["id"] == second["id"]
    forced = client.post("/patients/cen1/handoff", json={"audience": "enfermagem", "force": True}, headers=h).json()
    assert not forced["cached"] and forced["id"] != first["id"]

    with db_session.SessionLocal() as db:
        assert db.scalar(select(HandoffReport).where(HandoffReport.id == first["id"])).input_hash
        actions = [a.action for a in db.scalars(select(AuditLog))]
        assert "handoff.gerado" in actions and "handoff.cache_hit" in actions
        alerts = db.scalars(select(Alert).where(Alert.patient_id == "cen1")).all()
        # regenerar não duplica alertas
        assert len(alerts) == len({a.title for a in alerts}) > 0

    risk = next(r for r in forced["content"]["riscos_proximas_horas"] if r.get("alert_id"))
    ack = client.post(f"/alerts/{risk['alert_id']}/status", json={"status": "reconhecido"}, headers=h)
    assert ack.status_code == 200 and ack.json()["status"] == "reconhecido"
    bad = client.post(f"/alerts/{risk['alert_id']}/status", json={"status": "descartado"}, headers=h)
    assert bad.status_code == 422

    latest = client.get("/patients/cen1/handoff/latest?audience=enfermagem", headers=h).json()
    assert latest["id"] == forced["id"]


def test_reseed_and_rewording_do_not_duplicate_alerts(client, auth):
    """Recarregar os cenários muda os horários no texto dos riscos, mas não as fontes: o alerta é o mesmo."""
    from datetime import timedelta

    from app.fhir_client.client import LocalFhirStore, set_fhir
    from app.seed.scenarios import build_demo_resources

    from .conftest import ANCHOR

    h = auth("medico")
    client.post("/patients/cen1/handoff", json={"audience": "medico"}, headers=h)
    with db_session.SessionLocal() as db:
        before = {(a.id, a.title) for a in db.scalars(select(Alert).where(Alert.patient_id == "cen1"))}
    assert before

    set_fhir(LocalFhirStore.from_resources(build_demo_resources(anchor=ANCHOR - timedelta(minutes=23))))
    again = client.post("/patients/cen1/handoff", json={"audience": "medico", "force": True}, headers=h).json()
    with db_session.SessionLocal() as db:
        after = {(a.id, a.title) for a in db.scalars(select(Alert).where(Alert.patient_id == "cen1"))}
    assert {i for i, _ in after} == {i for i, _ in before}  # nenhum alerta novo
    assert after != before  # texto atualizado com os horários da nova carga
    ids = [r["alert_id"] for r in again["content"]["riscos_proximas_horas"] if r.get("alert_id")]
    assert len(ids) == len(set(ids))  # riscos distintos da mesma geração não se fundem


def test_upsert_alert_matches_by_sources_and_reopens_on_escalation(client):
    from app.alerts.service import upsert_alert

    with db_session.SessionLocal() as db:
        kw = dict(patient_id="p1", source="handoff")
        a = upsert_alert(db, **kw, severity="atencao", title="Glicose 164 às 18:54", refs=["Observation/g1"])
        a.status = "reconhecido"
        b = upsert_alert(db, **kw, severity="atencao", title="Glicose 164 às 18:31", refs=["Observation/g1"])
        assert b.id == a.id and b.title.endswith("18:31") and b.status == "reconhecido"
        other = upsert_alert(db, **kw, severity="atencao", title="Hemoglobina em queda", refs=["Observation/hb"])
        assert other.id != a.id
        claimed = {a.id}
        twin = upsert_alert(db, **kw, severity="atencao", title="Outro risco", refs=["Observation/g1"], claimed=claimed)
        assert twin.id != a.id
        worse = upsert_alert(db, **kw, severity="critico", title="Glicose 320", refs=["Observation/g1"])
        assert worse.id == a.id and worse.status == "aberto" and worse.severity == "critico"
        db.flush()
        assert db.scalar(select(AuditLog).where(AuditLog.action == "alerta.reaberto"))
