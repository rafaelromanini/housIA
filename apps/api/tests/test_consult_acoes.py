"""#3 — aceitar, descartar e fixar cada insight; selo 'novo'; alertas da consulta no módulo de alertas."""

import pytest
from sqlalchemy import select

from app.consult import acoes
from app.consult import service as consult_service
from app.db import session as db_session
from app.db.models import Alert, AuditLog
from tests.test_consult import SCRIPT, say, start


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def act(client, h, cid, key, acao, motivo="", expect=200):
    res = client.post(
        f"/consultations/{cid}/insight-actions", json={"item_key": key, "acao": acao, "motivo": motivo}, headers=h
    )
    assert res.status_code == expect, res.text
    return res.json()


def find(items, needle, field="texto"):
    return next(i for i in items if needle.lower() in i[field].lower())


def consult_alerts(patient="cen6", **where):
    with db_session.SessionLocal() as db:
        q = select(Alert).where(Alert.patient_id == patient, Alert.source == "consulta")
        return [a for a in db.scalars(q.order_by(Alert.id)) if all(getattr(a, k) == v for k, v in where.items())]


def test_item_key_is_stable_and_every_item_has_one(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    first = say(client, h, cid, SCRIPT[:6])
    second = say(client, h, cid, SCRIPT[6:8])
    for ins in (first, second):
        for group in (
            ins["perguntas"],
            ins["sugestoes_medicamentos"],
            ins["alertas"],
            ins["hipoteses"],
            ins["queixas"],
            ins["emocoes"],
        ):
            assert all(len(i["item_key"]) == 16 for i in group)
    assert acoes.item_key("pergunta", "Já  desmaiou?", ["Fala/4", "Fala/2"]) == acoes.item_key(
        "pergunta", "ja desmaiou?", ["Fala/2", "Fala/4"]
    )
    # um item que já existia na análise anterior mantém a primeira fala; o novo recebe a fala em que apareceu
    old = {q["item_key"]: q["primeira_fala"] for q in first["perguntas"]}
    for q in second["perguntas"]:
        if q["item_key"] in old:
            assert q["primeira_fala"] == old[q["item_key"]]
        else:
            assert q["primeira_fala"] == second["falas_analisadas"]


def test_discarded_question_does_not_come_back_after_new_utterances(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT[:8])
    victim = ins["perguntas"][0]
    act(client, h, cid, victim["item_key"], "descartar", "curto", expect=422)  # motivo de ao menos 10 caracteres
    out = act(client, h, cid, victim["item_key"], "descartar", "Já foi esclarecido na triagem de enfermagem")
    assert victim["pergunta"] not in [q["pergunta"] for q in out["insights"]["perguntas"]]
    assert out["insights"]["ocultos"] == 1
    for chunk in (SCRIPT[8:16], SCRIPT[16:]):
        ins = say(client, h, cid, chunk)
        assert victim["pergunta"] not in [q["pergunta"] for q in ins["perguntas"]]
    assert client.get(f"/consultations/{cid}", headers=h).json()["insights"]["ocultos"] >= 1


def test_pin_goes_to_the_top_and_toggles(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT[:12])
    last = ins["perguntas"][-1]
    out = act(client, h, cid, last["item_key"], "fixar")["insights"]
    assert out["perguntas"][0]["item_key"] == last["item_key"] and out["perguntas"][0]["fixado"]
    again = say(client, h, cid, SCRIPT[12:14])
    assert again["perguntas"][0]["item_key"] == last["item_key"]
    off = act(client, h, cid, last["item_key"], "fixar")["insights"]
    assert not any(q["fixado"] for q in off["perguntas"])


def test_question_done_moves_to_addressed_and_into_the_note_anamnesis(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    q = ins["perguntas"][0]
    out = act(client, h, cid, q["item_key"], "feita")["insights"]
    assert q["pergunta"] not in [x["pergunta"] for x in out["perguntas"]]
    assert any(x["pergunta"] == q["pergunta"] and x["acao"] == "feita" for x in out["perguntas_abordadas"])
    note = client.post(f"/consultations/{cid}/finish", headers=h).json()["nota"]
    assert f"Pergunta feita pelo profissional: {q['pergunta']}" in note["hda"]["texto"]
    assert set(q["refs"][:4]) <= set(note["hda"]["refs"])


def test_accepting_glibenclamide_alert_puts_it_in_the_note_plan_with_refs(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    sug = find(ins["sugestoes_medicamentos"], "Glibenclamida")
    out = act(client, h, cid, sug["item_key"], "aceitar")["insights"]
    assert find(out["sugestoes_medicamentos"], "Glibenclamida")["acao"] == "aceitar"
    note = client.post(f"/consultations/{cid}/finish", headers=h).json()["nota"]
    assert f"{sug['texto']} (aceito pelo profissional)" in note["plano"]["texto"]
    assert set(sug["refs"]) <= set(note["plano"]["refs"])
    assert "MedicationRequest/cen6-rx-glibenclamida-c2" in note["plano"]["refs"]


def test_accepted_hypothesis_goes_to_the_assessment(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    hyp = ins["hipoteses"][-1]
    act(client, h, cid, hyp["item_key"], "aceitar")
    note = client.post(f"/consultations/{cid}/finish", headers=h).json()["nota"]
    assert f"Hipótese aceita pelo profissional: {hyp['hipotese']} ({hyp['cid']})" in note["avaliacao"]["texto"]


def test_cen6_creates_consult_alerts_visible_on_patient_screen_and_handoff(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    alerts = consult_alerts()
    titles = [a.title for a in alerts]
    assert any("Glibenclamida" in t for t in titles) and any("Ibuprofeno" in t for t in titles)
    assert all(a.severity == "critico" and a.status == "aberto" for a in alerts)
    # nova fala não duplica
    before = len(alerts)
    say(client, h, cid, [{"falante": "paciente", "texto": "Obrigada, doutor.", "t": 999.0}])
    assert len(consult_alerts()) == before
    # tela do paciente
    listed = client.get("/patients/cen6/alerts", headers=h).json()
    assert {a["title"] for a in listed if a["source"] == "consulta"} == set(titles)
    panel = client.get("/patients/cen6", headers=h).json()
    assert panel["alertas_abertos"]["critico"] >= before
    # passagem de plantão
    ho = client.post("/patients/cen6/handoff", json={"audience": "medico"}, headers=h)
    assert ho.status_code == 200, ho.text
    texts = " ".join(r["texto"] for r in ho.json()["content"]["riscos_proximas_horas"])
    assert "Alerta da consulta" in texts and "Glibenclamida" in texts
    assert any(f["tipo"] == "alerta_consulta" for f in ho.json()["content"]["fatos"])


def test_action_on_critical_item_updates_alert_status_and_acceptance_metric(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    glib = find(ins["alertas"], "Glibenclamida")
    ibu = find(ins["alertas"], "Ibuprofeno")
    act(client, h, cid, glib["item_key"], "aceitar")
    act(client, h, cid, ibu["item_key"], "descartar", "Paciente já suspendeu o ibuprofeno ontem")
    by = {a.title: a for a in consult_alerts()}
    assert by[glib["texto"][:300]].status == "reconhecido"
    assert by[ibu["texto"][:300]].status == "descartado"
    with db_session.SessionLocal() as db:
        actions = [a.action for a in db.scalars(select(AuditLog).where(AuditLog.entity == "alert"))]
    assert "alerta.reconhecido" in actions and "alerta.descartado" in actions
    # o descartado não volta como alerta aberto
    ins = say(client, h, cid, [{"falante": "paciente", "texto": "Certo.", "t": 1000.0}])
    assert not any("Ibuprofeno:" in a["texto"] and a["severidade"] == "critico" for a in ins["alertas"])
    assert by[ibu["texto"][:300]].id not in [a.id for a in consult_alerts(status="aberto")]
    m = client.get("/audit/metrics", headers=auth("gestor")).json()
    assert m["alertas_criticos_por_origem"].get("consulta", 0) >= 3
    assert m["alertas_por_status"].get("reconhecido", 0) >= 1 and m["alertas_por_status"].get("descartado", 0) >= 1
    assert m["taxa_aceitacao_alertas"] == 0.5  # 1 reconhecido x 1 descartado, ambos da consulta


def test_acceptance_by_layer_is_audited_on_finish(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    act(client, h, cid, ins["perguntas"][0]["item_key"], "feita")
    act(client, h, cid, find(ins["sugestoes_medicamentos"], "Glibenclamida")["item_key"], "aceitar")
    client.post(f"/consultations/{cid}/finish", headers=h)
    with db_session.SessionLocal() as db:
        row = db.scalars(select(AuditLog).where(AuditLog.action == "consulta.finalizada")).all()[-1]
    layer = row.payload_json["aceite_insights"]
    assert layer["pergunta"]["regra"]["aceitas"] == 1 and layer["alerta"]["regra"]["aceitas"] == 1


def test_validation_and_permissions(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT[:8])
    key = ins["perguntas"][0]["item_key"]
    act(client, h, cid, "inexistente", "aceitar", expect=422)
    act(client, h, cid, key, "feita")
    act(
        client,
        h,
        cid,
        ins["alertas"][0]["item_key"] if ins["alertas"] else ins["hipoteses"][0]["item_key"],
        "feita",
        expect=422,
    )
    assert (
        client.post(
            f"/consultations/{cid}/insight-actions", json={"item_key": key, "acao": "voar"}, headers=h
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/consultations/{cid}/insight-actions", json={"item_key": key, "acao": "aceitar"}, headers=auth("gestor")
        ).status_code
        == 403
    )
    client.post(f"/consultations/{cid}/finish", headers=h)
    act(client, h, cid, key, "aceitar", expect=409)


def test_new_badge_window_and_latency_chip_data(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT[:6])
    c = client.get(f"/consultations/{cid}", headers=h).json()
    assert isinstance(c["insights"]["regras_ms"], int) and c["insights"]["regras_ms"] >= 0
    ins = say(client, h, cid, SCRIPT[6:9])
    fresh = [q for q in ins["perguntas"] if q["primeira_fala"] == ins["falas_analisadas"]]
    assert fresh  # a análise trouxe perguntas novas, e o selo vale por NEW_FOR falas
    assert acoes.NEW_FOR == 2 and all(q["primeira_fala"] > 0 for q in fresh)
