"""#44 — Cartão "Antes de entrar": o que mudou, pendências da última vez e resumo da última alta, com fonte."""

import copy

from sqlalchemy import select

from app import clock
from app.consult.chart import ChartView
from app.consult.pre_consulta import Sources, build
from app.db import session as db_session
from app.db.models import AuditLog
from app.fhir_client.client import LocalFhirStore
from app.seed.builder import build_all
from app.seed.scenarios import load_scenarios
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from app.timeline.record import PatientRecord
from tests.test_consult import SCRIPT, say, start
from tests.test_discharge import RISKY
from tests.test_multiorg import cadastro_completo


def card(client, h, pid="cen6", **params):
    res = client.get(f"/patients/{pid}/pre-consulta", params=params, headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def lines(c):
    return [x for part in ("motivo", "mudou", "pendencias", "ultima_alta") for x in c[part]]


# ---------- critério 1: cen6 mostra creatinina subindo e HCTZ iniciada, com as refs ----------
def test_cen6_creatinina_subindo_e_hctz_iniciada_com_refs(client, auth):
    c = card(client, auth("medico"))
    assert c["ultima_consulta"] and c["encounter_id"] == "cen6-amb-hoje"
    assert c["motivo"][0]["refs"] == ["Encounter/cen6-amb-hoje"] and "tontura" in c["motivo"][0]["texto"]
    creat = next(x for x in c["mudou"] if x["texto"].startswith("Creatinina"))
    assert "subiu de 1,3 para 1,5" in creat["texto"] and len(creat["refs"]) == 2
    assert all(r.startswith("Observation/") for r in creat["refs"])
    hctz = next(x for x in c["mudou"] if "hidroclorotiazida" in x["texto"].lower() and "iniciado" in x["texto"])
    assert hctz["refs"] and hctz["refs"][0].startswith("MedicationRequest/")
    assert any("suspenso" in x["texto"] and "nlodipino" in x["texto"] for x in c["mudou"])  # a troca vem junto


def test_toda_linha_tem_fonte_e_ref_ou_nota(client, auth):
    c = card(client, auth("medico"))
    assert lines(c) and all(x["fonte"] for x in lines(c))
    assert all(x["refs"] for x in c["motivo"] + c["mudou"])  # o que vem do prontuário sempre cita o recurso


def test_pedidos_sem_resultado_viram_pendencia_com_ref(client, auth):
    c = card(client, auth("medico"))
    pedidos = [x for x in c["pendencias"] if x["tipo"] == "pedido"]
    texto = " ".join(x["texto"] for x in pedidos)
    assert "Relação albumina/creatinina urinária" in texto and "Retinografia (fundo de olho)" in texto
    assert all(x["refs"][0].startswith("ServiceRequest/") for x in pedidos)


# ---------- critério 2: a internação aparece no histórico ----------
def _record_com_internacao():
    scen = copy.deepcopy(next(s for s in load_scenarios() if s["id"] == "cen6"))
    scen["encontros_anteriores"] = [
        {"tipo": "internacao", "urgente": True, "inicio": "-40d", "duracao": "72h", "motivo": "Desidratação"}
    ]
    store = LocalFhirStore.from_resources(build_all([scen], clock.now()))
    record = PatientRecord.load(store, "cen6")
    record.now = clock.now()
    return record


def test_internacao_aparece_no_historico_e_no_que_mudou():
    record = _record_com_internacao()
    chart = ChartView(record, build_timeline(record), build_context(record), clock.now())
    stay = [h for h in chart.history() if h.tipo == "internacao"]
    assert len(stay) == 1 and "Desidratação" in stay[0].texto and stay[0].refs[0].startswith("Encounter/")
    pre = build(chart, "cen6", Sources())
    assert any(x.tipo == "internacao" and "Desidratação" in x.texto for x in pre.mudou)


# ---------- critério 3: pendência da nota anterior aparece e some quando citada ----------
def _finish(client, h):
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200, done.text
    return cid, done.json()["nota"]


def test_pendencia_da_nota_anterior_aparece_e_some_quando_citada(client, auth):
    h = auth("medico")
    _, nota = _finish(client, h)
    assert nota["pendencias"], "o roteiro do cen6 deixa pendências na nota"
    c = card(client, h)
    da_nota = [x for x in c["pendencias"] if x["tipo"] == "nota"]
    assert len(da_nota) == len(nota["pendencias"])
    assert all("Nota da consulta de" in x["fonte"] and not x["tratada"] for x in da_nota)
    alvo = da_nota[0]
    assunto = alvo["texto"].split(": ", 1)[1]
    # nova consulta: o médico cita o assunto da pendência e ela passa a tratada, com a Fala/N
    cid2 = start(client, h)["id"]
    antes = card(client, h, consulta=cid2)
    assert not any(x["tratada"] for x in antes["pendencias"] if x["tipo"] == "nota")
    say(client, h, cid2, [{"falante": "medico", "texto": assunto, "t": 1.0}])
    depois = card(client, h, consulta=cid2)
    marcada = next(x for x in depois["pendencias"] if x["tipo"] == "nota" and x["texto"] == alvo["texto"])
    assert marcada["tratada"] and marcada["tratada_refs"] == ["Fala/1"]


def test_pedido_citado_na_conversa_some(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(
        client,
        h,
        cid,
        [{"falante": "medico", "texto": "Vamos pedir de novo a retinografia, o fundo de olho.", "t": 1.0}],
    )
    c = card(client, h, consulta=cid)
    fundo = next(x for x in c["pendencias"] if "Retinografia" in x["texto"])
    rac = next(x for x in c["pendencias"] if "albumina" in x["texto"])
    assert fundo["tratada"] and not rac["tratada"]


# ---------- resumo opcional ----------
def test_resumo_opcional_de_ate_3_frases_com_fonte_do_cartao(client, auth):
    h = auth("medico")
    assert card(client, h)["resumo_ia"] == []
    c = card(client, h, resumo="true")
    assert 1 <= len(c["resumo_ia"]) <= 3 and c["modo_resumo"] == "regras"
    valid = {r for x in lines(c) for r in x["refs"]}
    assert all(f["refs"] and set(f["refs"]) <= valid for f in c["resumo_ia"])


# ---------- alta e módulo do plano ----------
def test_resumo_da_ultima_alta_com_lace_e_plano(client, auth):
    h = auth("medico")
    a = client.post("/patients/cen3/discharge-assessment", json={"social_checklist": RISKY}, headers=h)
    assert a.status_code == 200, a.text
    c = card(client, h, "cen3")
    alta = c["ultima_alta"]
    assert alta and "risco de reinternação alto" in alta[0]["texto"] and "LACE" in alta[0]["texto"]
    assert all(x["fonte"].startswith(("Avaliação de alta", "Reconciliação")) for x in alta)


def test_outra_organizacao_nao_ve_nota_nem_alta_nem_pendencia_da_primeira(client, auth):
    h = auth("medico")
    _finish(client, h)
    client.post("/patients/cen3/discharge-assessment", json={"social_checklist": RISKY}, headers=h)
    h2, _ = cadastro_completo(client)
    c = card(client, h2)
    assert not [x for x in c["pendencias"] if x["tipo"] == "nota"]
    assert card(client, h2, "cen3")["ultima_alta"] == []  # e o plano do consultório não tem o módulo de alta
    assert client.get("/patients/cen6/pre-consulta", params={"consulta": 1}, headers=h2).status_code == 404


def test_auditoria_sem_texto_da_conversa(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, [{"falante": "paciente", "texto": "Estou com muita tontura ao levantar.", "t": 1.0}])
    card(client, h, consulta=cid, resumo="true")
    with db_session.SessionLocal() as db:
        rows = list(db.scalars(select(AuditLog).where(AuditLog.action == "consulta.pre_consulta")))
    assert rows and all("tontura" not in str(r.payload_json) for r in rows)
    assert {"linhas", "pendencias", "tratadas"} <= set(rows[-1].payload_json)


def test_exige_login(client):
    assert client.get("/patients/cen6/pre-consulta").status_code in (401, 403)
