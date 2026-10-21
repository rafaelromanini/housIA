"""Waze da jornada (#30): lista de pacientes em risco de abandono, tarefas, contato pós-alta e métricas."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fhir.resources.R4B.documentreference import DocumentReference
from fhir.resources.R4B.task import Task
from sqlalchemy import select

from app import clock
from app.db import session as db_session
from app.db.models import AuditLog, JourneyTask
from app.fhir_client.client import LocalFhirStore, set_fhir
from app.journey import metrics, reference
from app.journey.rules import RULES
from app.seed.scenarios import build_demo_resources
from tests.conftest import ANCHOR, CONSENT
from tests.test_discharge import RISKY
from tests.test_surgical import dictate

SAFE = {
    "sinal_febre": "nao",
    "sinal_dispneia": "nao",
    "sinal_dor": "nao",
    "sinal_sangramento": "nao",
    "sinal_confusao": "nao",
    "sinal_queda": "nao",
}


@pytest.fixture
def jstore(client):
    """FHIR local próprio de cada teste: a jornada escreve Task e DocumentReference e não pode vazar para os outros."""
    store = LocalFhirStore.from_resources(build_demo_resources(anchor=ANCHOR))
    set_fhir(store)
    return store


@pytest.fixture
def at():
    """Move o relógio clínico a partir do 'agora' do teste e restaura no fim."""
    base = clock.now()

    def _go(**delta):
        clock.freeze(base + timedelta(**delta))

    yield _go
    clock.freeze(base)


def worklist(client, h, **params):
    res = client.get("/journey/worklist", params=params, headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def find(wl: dict, rule: str, pid: str) -> dict | None:
    return next((i for i in wl["itens"] if i["regra"] == rule and i["paciente"]["id"] == pid), None)


def answers(script: list[dict], **overrides) -> dict:
    """Respostas 'tudo bem' para todas as perguntas do roteiro do paciente, com ajustes."""
    base = {q["id"]: ("nao" if q["tipo"] == "sinal_alarme" else "sim") for q in script}
    return base | overrides


def audit_rows(action: str) -> list[dict]:
    with db_session.SessionLocal() as db:
        return [r.payload_json for r in db.scalars(select(AuditLog).where(AuditLog.action == action))]


# ======================= dados =======================
def test_reference_files_are_consistent():
    assert reference.validate() == []
    assert set(reference.config()["regras"]) == set(RULES)
    ids = {q["id"] for q in reference.script()}
    assert {"tb_medicamentos", "retirou_medicamentos", "sinal_febre"} <= ids


def test_seed_has_appointments_and_journey_cases(store):
    appts = {a["id"]: a for a in store.appointments()}
    assert appts["cen7b-ag-ret"]["status"] == "noshow" and appts["cen7-ag-ret"]["status"] == "booked"
    assert "cen7e-ag-ret" not in appts and "cen7c-ag-ret" not in appts  # sem agendamento: a regra dispara
    ref = appts["cen7b-ag-ret"]["participant"][0]["actor"]["reference"]
    assert ref == "Patient/cen7b"
    imp = [e for e in store.encounters_by_class("IMP") if not e.get("partOf")]
    assert sum(1 for e in imp if e["status"] == "finished") >= 20


def test_worklist_requires_login_and_lists_each_seed_rule_once(client, auth, jstore):
    assert client.get("/journey/worklist").status_code == 401
    wl = worklist(client, auth("enfermagem"))
    got = {(i["regra"], i["paciente"]["id"]) for i in wl["itens"]}
    assert got == {
        ("alta_sem_contato_72h", "cen7"),
        ("falta_ao_retorno", "cen7b"),
        ("retorno_nao_agendado", "cen7c"),
        ("troca_sem_reconciliacao", "cen7d"),
        ("protocolo_aps_vencido", "cen7e"),
    }
    assert wl["resumo"]["itens"] == 5 and wl["resumo"]["criticos"] == 1
    # quem está internado (cen1..cen5 e o fundo) nunca entra: a jornada começa na alta
    assert not {i["paciente"]["id"] for i in wl["itens"]} & {"cen1", "cen2", "cen3", "cen4", "bg01", "bg05"}
    for i in wl["itens"]:
        assert i["fontes"] and i["fonte_regra"] and i["papel_sugerido"] and i["prazo_tarefa_horas"] > 0
        assert i["paciente"]["nome"] and i["tarefa"] is None
    # filtros
    assert [i["regra"] for i in worklist(client, auth(), regra="falta_ao_retorno")["itens"]] == ["falta_ao_retorno"]
    assert {i["papel_sugerido"] for i in worklist(client, auth(), papel="medico")["itens"]} == {"medico"}
    assert client.get("/journey/worklist?regra=inventada", headers=auth()).status_code == 422


# ======================= critério 1 e 2: alta de alto risco sem contato =======================
def test_high_risk_discharge_four_days_later_is_listed_with_refs_and_contact_removes_it(client, auth, jstore, at):
    h = auth("medico")
    a = client.post("/patients/cen3/discharge-assessment", json={"social_checklist": RISKY}, headers=h).json()
    assert a["final_risk"] == "alto"
    text = "Filha virá morar com o paciente por 2 semanas; visita do Melhor em Casa agendada para amanhã."
    client.post(f"/discharge-assessments/{a['id']}/override", json={"justification": text}, headers=h)
    assert client.post(f"/discharge-assessments/{a['id']}/confirm", headers=h).status_code == 200

    # logo após a alta ainda não venceu: aparece como "a vencer" (atenção)
    soon = find(worklist(client, h), "alta_sem_contato_72h", "cen3")
    assert soon and soon["situacao"] == "a_vencer" and soon["severidade"] == "atencao"

    at(days=4)
    item = find(worklist(client, h), "alta_sem_contato_72h", "cen3")
    assert item is not None
    assert item["situacao"] == "vencido" and item["severidade"] == "critico"
    assert "há 4 dias" in item["titulo"]
    refs = set(item["fontes"])
    assert {"Encounter/cen3-int", f"DischargeAssessment/{a['id']}", "Condition/cen3-dx-0"} <= refs
    assert item["contexto"]["risco"] == "alto" and item["contexto"]["risco_origem"] == "avaliacao"
    assert "Checklist/mora_sozinho" in refs  # itens sociais da avaliação entram como fonte
    assert item["paciente"]["familiar"]["nome"] == "Cláudia Moreira"

    # a tentativa sem sucesso não tira da lista; o contato efetivo tira
    script = client.get("/journey/patients/cen3/contact-script", headers=h).json()["perguntas"]
    miss = client.post("/journey/patients/cen3/contacts", json={"canal": "telefone", "atendeu": False}, headers=h)
    assert miss.status_code == 201 and miss.json()["fhir_ref"] is None
    again = find(worklist(client, h), "alta_sem_contato_72h", "cen3")
    assert again and again["contexto"]["tentativas"] == 1 and "1 tentativa(s) sem sucesso" in again["detalhe"]
    done = client.post(
        "/journey/patients/cen3/contacts",
        json={"canal": "telefone", "atendeu": True, "respostas": answers(script), "observacoes": "Tomando insulina."},
        headers=h,
    )
    assert done.status_code == 201, done.text
    assert find(worklist(client, h), "alta_sem_contato_72h", "cen3") is None


def test_seed_discharge_without_contact_then_contact_removes_and_three_misses_escalate(client, auth, jstore):
    h = auth("enfermagem")
    wl = worklist(client, h)
    item = find(wl, "alta_sem_contato_72h", "cen7")
    assert item and item["situacao"] == "vencido" and item["contexto"]["risco_origem"] == "estimado"
    assert "Encounter/cen7-int" in item["fontes"] and "Condition/cen7-dx-0" in item["fontes"]
    assert "Marta Brandão" in item["detalhe"]  # familiar de contato quando o paciente não atende
    for _ in range(3):
        assert client.post("/journey/patients/cen7/contacts", json={"atendeu": False}, headers=h).status_code == 201
    esc = find(worklist(client, h), "alta_sem_contato_72h", "cen7")
    assert esc["contexto"]["tentativas"] == 3 and "visita domiciliar" in esc["detalhe"]
    script = client.get("/journey/patients/cen7/contact-script", headers=h).json()["perguntas"]
    ok = client.post("/journey/patients/cen7/contacts", json={"atendeu": True, "respostas": answers(script)}, headers=h)
    assert ok.status_code == 201
    assert find(worklist(client, h), "alta_sem_contato_72h", "cen7") is None


# ======================= critério 3: sinal de alarme cria alerta =======================
def test_alarm_sign_creates_critical_alert_and_document_reference(client, auth, jstore):
    h = auth("enfermagem")
    script = client.get("/journey/patients/cen7/contact-script", headers=h).json()["perguntas"]
    ids = {q["id"] for q in script}
    # roteiro por paciente: insuficiência cardíaca e insulina trazem os sinais específicos
    assert {"sinal_peso_ic", "sinal_hipoglicemia", "tb_medicamentos"} <= ids and "sinal_ferida" not in ids

    incomplete = {k: v for k, v in answers(script).items() if k != "sinal_febre"}
    bad = client.post("/journey/patients/cen7/contacts", json={"atendeu": True, "respostas": incomplete}, headers=h)
    assert bad.status_code == 422 and "sinal_febre" in bad.text  # não dá para pular a triagem de alarme
    assert (
        client.post(
            "/journey/patients/cen7/contacts", json={"atendeu": True, "respostas": {"inventada": "sim"}}, headers=h
        ).status_code
        == 422
    )

    res = client.post(
        "/journey/patients/cen7/contacts",
        json={
            "canal": "telefone",
            "atendeu": True,
            "respostas": answers(script, sinal_peso_ic="sim", retirou_medicamentos="nao"),
            "observacoes": "Refere pernas inchadas há dois dias.",
        },
        headers=h,
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["sinais_de_alarme"] == ["sinal_peso_ic"]
    assert body["alerta"]["severidade"] == "critico" and "ganho de peso" in body["alerta"]["titulo"].lower()
    assert [p["pergunta"] for p in body["pendencias"]] == ["retirou_medicamentos"]
    assert "SINAIS DE ALARME" in body["texto"]

    alerts = client.get("/patients/cen7/alerts?status=aberto", headers=h).json()
    jornada = [a for a in alerts if a["source"] == "jornada"]
    assert len(jornada) == 1 and jornada[0]["severity"] == "critico"
    assert body["fhir_ref"] in jornada[0]["source_refs"] and "Encounter/cen7-int" in jornada[0]["source_refs"]

    # DocumentReference no prontuário, válido em FHIR R4 e visível na timeline do paciente
    doc = jstore.get("DocumentReference", body["fhir_ref"].split("/")[1])
    DocumentReference.model_validate(doc)
    assert doc["subject"]["reference"] == "Patient/cen7" and doc["context"]["encounter"][0]["reference"] == (
        "Encounter/cen7-int"
    )
    timeline = client.get("/patients/cen7/timeline", headers=h).json()
    assert any(e["source_ref"] == body["fhir_ref"] and e["kind"] == "note" for e in timeline)

    # sem alarme não há alerta novo
    ok = client.post(
        "/journey/patients/cen7/contacts", json={"atendeu": True, "respostas": answers(script)}, headers=h
    ).json()
    assert ok["alerta"] is None
    # o audit guarda ids, nunca o texto livre das observações
    logged = audit_rows("jornada.contato_registrado")
    assert logged[0]["alarmes"] == ["sinal_peso_ic"] and logged[0]["alerta"] == body["alerta"]["id"]
    assert "pernas" not in str(logged)


def test_contact_for_inpatient_is_refused(client, auth, jstore):
    res = client.post("/journey/patients/cen1/contacts", json={"atendeu": False}, headers=auth())
    assert res.status_code == 409


# ======================= critério 4: tarefa FHIR =======================
def test_create_task_writes_fhir_task_with_role_and_deadline(client, auth, jstore):
    h = auth("enfermagem")
    res = client.post(
        "/journey/tasks", json={"patient_id": "cen7", "regra": "alta_sem_contato_72h", "prazo_horas": 12}, headers=h
    )
    assert res.status_code == 201, res.text
    task = res.json()
    assert task["papel"] == "enfermagem" and task["status"] == "aberta" and task["fhir_ref"].startswith("Task/")
    assert task["fontes"] and "Alta de alto risco" in task["titulo"]
    due = clock.now() + timedelta(hours=12)
    assert abs((datetime.fromisoformat(task["prazo"]) - due).total_seconds()) < 5

    fhir = jstore.get("Task", task["fhir_ref"].split("/")[1])
    Task.model_validate(fhir)
    assert fhir["for"] == {"reference": "Patient/cen7"} and fhir["status"] == "requested" and fhir["intent"] == "order"
    assert fhir["priority"] == "urgent"
    assert fhir["performerType"][0]["coding"][0]["code"] == "enfermagem"
    assert fhir["restriction"]["period"]["end"] == task["prazo"]
    assert {"reference": "Encounter/cen7-int"} in [i["valueReference"] for i in fhir["input"]]
    assert not any(i["valueReference"]["reference"].startswith("Checklist/") for i in fhir["input"])

    # o item mostra a tarefa e não duplica
    item = find(worklist(client, h), "alta_sem_contato_72h", "cen7")
    assert item["tarefa"]["id"] == task["id"]
    dup = client.post("/journey/tasks", json={"patient_id": "cen7", "regra": "alta_sem_contato_72h"}, headers=h)
    assert dup.status_code == 409
    gone = client.post("/journey/tasks", json={"patient_id": "cen3", "regra": "falta_ao_retorno"}, headers=h)
    assert gone.status_code == 409  # o servidor só cria tarefa de item que está na lista
    assert client.post("/journey/tasks", json={"patient_id": "cen7", "regra": "x"}, headers=h).status_code == 422

    # papel escolhido pela equipe vira o performerType
    other = client.post(
        "/journey/tasks", json={"patient_id": "cen7d", "regra": "troca_sem_reconciliacao", "papel": "gestor"}, headers=h
    ).json()
    assert jstore.get("Task", other["fhir_ref"].split("/")[1])["performerType"][0]["coding"][0]["code"] == "gestor"
    assert other["papel"] == "gestor"

    listed = client.get("/journey/tasks?status=aberta&papel=enfermagem", headers=h).json()
    assert [t["id"] for t in listed] == [task["id"]] and listed[0]["paciente"]["nome"] == "Osvaldo Teixeira Brandão"
    assert audit_rows("jornada.tarefa_criada")[0]["fhir"] == task["fhir_ref"]


def test_task_close_cancel_and_contact_closes_open_contact_task(client, auth, jstore):
    h = auth("enfermagem")
    t1 = client.post("/journey/tasks", json={"patient_id": "cen7", "regra": "alta_sem_contato_72h"}, headers=h).json()
    t2 = client.post("/journey/tasks", json={"patient_id": "cen7b", "regra": "falta_ao_retorno"}, headers=h).json()
    assert client.patch(f"/journey/tasks/{t2['id']}", json={"status": "cancelada"}, headers=h).status_code == 422
    done = client.patch(
        f"/journey/tasks/{t2['id']}", json={"status": "concluida", "observacao": "Remarcado."}, headers=h
    )
    assert done.status_code == 200 and done.json()["status"] == "concluida"
    assert jstore.get("Task", t2["fhir_ref"].split("/")[1])["status"] == "completed"
    assert client.patch(f"/journey/tasks/{t2['id']}", json={"status": "concluida"}, headers=h).status_code == 409

    script = client.get("/journey/patients/cen7/contact-script", headers=h).json()["perguntas"]
    client.post("/journey/patients/cen7/contacts", json={"atendeu": True, "respostas": answers(script)}, headers=h)
    with db_session.SessionLocal() as db:
        closed = db.get(JourneyTask, t1["id"])
        assert closed.status == "concluida" and "Encerrada pelo contato" in closed.closing_note
    assert jstore.get("Task", t1["fhir_ref"].split("/")[1])["status"] == "completed"


# ======================= demais regras =======================
def test_missed_appointment_rule_resolves_with_new_booking_or_contact(client, auth, jstore):
    h = auth()
    item = find(worklist(client, h), "falta_ao_retorno", "cen7b")
    assert "Appointment/cen7b-ag-ret" in item["fontes"] and "Encounter/cen7b-int" in item["fontes"]
    assert item["severidade"] == "atencao" and item["titulo"].startswith("Faltou à consulta de ")
    # remarcar (novo Appointment no prontuário) resolve; a falta antiga fica no histórico
    jstore.create(
        {
            "resourceType": "Appointment",
            "id": "cen7b-ag-novo",
            "status": "booked",
            "start": (clock.now() + timedelta(days=3)).isoformat(),
            "participant": [{"actor": {"reference": "Patient/cen7b"}, "status": "accepted"}],
        }
    )
    assert find(worklist(client, h), "falta_ao_retorno", "cen7b") is None
    # sem retorno novo, o contato com o paciente depois da falta também resolve
    del jstore.resources["Appointment"]["cen7b-ag-novo"]
    assert find(worklist(client, h), "falta_ao_retorno", "cen7b")
    script = client.get("/journey/patients/cen7b/contact-script", headers=h).json()["perguntas"]
    client.post("/journey/patients/cen7b/contacts", json={"atendeu": True, "respostas": answers(script)}, headers=h)
    assert find(worklist(client, h), "falta_ao_retorno", "cen7b") is None


def test_return_not_scheduled_rule_and_grace_period(client, auth, jstore, at):
    h = auth()
    item = find(worklist(client, h), "retorno_nao_agendado", "cen7c")
    assert item and item["fontes"] == ["Encounter/cen7c-int"] and "há 8 dias" in item["titulo"]
    assert item["severidade"] == "atencao"
    jstore.create(
        {
            "resourceType": "Appointment",
            "id": "cen7c-ag-ret",
            "status": "booked",
            "start": (clock.now() + timedelta(days=5)).isoformat(),
            "participant": [{"actor": {"reference": "Patient/cen7c"}, "status": "accepted"}],
        }
    )
    assert find(worklist(client, h), "retorno_nao_agendado", "cen7c") is None
    # ninguém é cobrado no dia da alta: a carência é de 48 h
    at(days=-7, hours=-12)  # 12 h depois da alta de cen7c
    del jstore.resources["Appointment"]["cen7c-ag-ret"]
    assert find(worklist(client, h), "retorno_nao_agendado", "cen7c") is None


def test_changed_medication_without_reconciliation_resolves_after_medrec(client, auth, jstore):
    h = auth()
    item = find(worklist(client, h), "troca_sem_reconciliacao", "cen7d")
    assert item and {"MedicationRequest/cen7d-rx-losartana-enf", "MedicationRequest/cen7d-rx-enalapril-enf"} <= set(
        item["fontes"]
    )
    assert "Enalapril trocado por Losartana" in item["detalhe"]
    assert client.post("/patients/cen7d/medrec", json={}, headers=h).status_code == 200
    assert find(worklist(client, h), "troca_sem_reconciliacao", "cen7d") is None
    # a troca antiga do cenário 6 (91 dias) fica fora da janela de 60 dias
    assert find(worklist(client, h), "troca_sem_reconciliacao", "cen6") is None


def test_overdue_aps_protocol_cites_exam_and_booking_resolves(client, auth, jstore):
    h = auth()
    item = find(worklist(client, h), "protocolo_aps_vencido", "cen7e")
    assert item["papel_sugerido"] == "medico" and "Hemoglobina glicada" in item["detalhe"]
    assert "Observation/cen7e-lab0-hba1c" in item["fontes"] and "Condition/cen7e-dx-0" in item["fontes"]
    assert find(worklist(client, h), "protocolo_aps_vencido", "cen6") is None  # está em consulta agora
    jstore.create(
        {
            "resourceType": "Appointment",
            "id": "cen7e-ag-ret",
            "status": "booked",
            "start": (clock.now() + timedelta(days=4)).isoformat(),
            "participant": [{"actor": {"reference": "Patient/cen7e"}, "status": "accepted"}],
        }
    )
    assert find(worklist(client, h), "protocolo_aps_vencido", "cen7e") is None


def test_non_adherence_signal_from_recent_consultation(client, auth, jstore):
    h = auth()
    cid = client.post(
        "/patients/cen7e/consultations",
        json={"tipo": "consulta", "especialidade": "clinica_medica", "consentimento": CONSENT},
        headers=h,
    ).json()["id"]
    dictate(
        client,
        h,
        cid,
        [
            ("medico", "Como o senhor tem tomado os remédios?"),
            ("paciente", "Doutora, eu esqueço de tomar a metformina quase todo dia."),
        ],
    )
    assert find(worklist(client, h), "nao_adesao_consulta", "cen7e") is None  # consulta ainda em andamento
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    item = find(worklist(client, h), "nao_adesao_consulta", "cen7e")
    assert item and f"Consultation/{cid}" in item["fontes"] and any(r.startswith("Fala/") for r in item["fontes"])
    assert "adesão" in item["detalhe"]
    script = client.get("/journey/patients/cen7e/contact-script", headers=h).json()["perguntas"]
    client.post("/journey/patients/cen7e/contacts", json={"atendeu": True, "respostas": answers(script)}, headers=h)
    assert find(worklist(client, h), "nao_adesao_consulta", "cen7e") is None


# ======================= texto pronto, sem envio =======================
def test_message_draft_is_text_only(client, auth, jstore):
    h = auth()
    res = client.get("/journey/patients/cen7/message-draft", headers=h).json()
    assert res["envio"] is False and "não envia SMS" in res["aviso"]
    text = res["texto"]
    assert text.startswith("Olá, Osvaldo!") and "alta, em" in text
    assert "Furosemida" in text and "Carvedilol" in text  # remédios de uso domiciliar
    assert "retorno está marcado" in text and "192" in text and "falta de ar" in text
    no_return = client.get("/journey/patients/cen7c/message-draft", headers=h).json()["texto"]
    assert "Ainda precisamos confirmar a data do seu retorno" in no_return
    assert "httpx" not in Path("app/journey/service.py").read_text()  # nenhum envio real


# ======================= critério 5: métricas sobre Encounter IMP =======================
def expected_readmission(store: LocalFhirStore, days: int = 30) -> tuple[int, int]:
    """Conta independente, direto dos Encounter IMP de nível superior: (altas com seguimento completo, reinternadas)."""
    from app.timeline.record import parse_dt

    now = clock.now()
    by_patient: dict[str, list[dict]] = {}
    for e in store.encounters_by_class("IMP"):
        if not e.get("partOf"):
            by_patient.setdefault(e["subject"]["reference"], []).append(e)
    complete = readmitted = 0
    for encounters in by_patient.values():
        for e in encounters:
            end = parse_dt(e["period"].get("end"))
            if e["status"] != "finished" or end is None or end > now or end + timedelta(days=days) > now:
                continue
            complete += 1
            if any(
                end < parse_dt(o["period"]["start"]) <= end + timedelta(days=days) for o in encounters if o is not e
            ):
                readmitted += 1
    return complete, readmitted


def test_readmission_metric_is_computed_on_imp_encounters_by_cohort(client, auth, jstore):
    h = auth("gestor")
    res = client.get("/journey/metrics?coorte=risco&dias=365", headers=h)
    assert res.status_code == 200, res.text
    m = res.json()
    complete, readmitted = expected_readmission(jstore)
    total = m["total"]
    assert (total["seguimento_completo"], total["reinternacoes"]) == (complete, readmitted) and readmitted >= 3
    assert total["taxa_reinternacao"] == round(readmitted / complete, 3)
    assert "Encounter IMP" in m["metodo"]
    # as coortes somam o total e cada caso aponta para um Encounter real
    assert sum(c["altas"] for c in m["coortes"]) == total["altas"] == len(m["casos"])
    assert sum(c["reinternacoes"] for c in m["coortes"]) == readmitted
    by_key = {c["chave"]: c for c in m["coortes"]}
    assert by_key["alto"]["taxa_reinternacao"] >= by_key["baixo"]["taxa_reinternacao"]
    for caso in m["casos"]:
        enc = jstore.get("Encounter", caso["encounter_ref"].split("/")[1])
        assert enc["class"]["code"] == "IMP" and enc["status"] == "finished" and enc["period"]["end"] == caso["alta_em"]
        if caso["reinternado"]:
            nxt = jstore.get("Encounter", caso["reinternacao_ref"].split("/")[1])
            assert nxt["class"]["code"] == "IMP" and 0 < caso["dias_ate_reinternacao"] <= 30
    # alta com menos de 30 dias não entra na taxa: está em seguimento
    assert total["em_seguimento"] == total["altas"] - complete and total["em_seguimento"] >= 3


def test_lost_returns_by_cohort_and_other_groupings(client, auth, jstore):
    h = auth("gestor")
    m = client.get("/journey/metrics?coorte=risco", headers=h).json()
    total = m["total"]
    complete = [c for c in m["casos"] if c["seguimento_completo"]]
    assert total["retornos_perdidos"] == sum(1 for c in complete if not c["retorno_realizado"])
    assert total["faltas"] == 1 and total["taxa_retorno_perdido"] == round(
        total["retornos_perdidos"] / len(complete), 3
    )
    kept = next(c for c in complete if c["retorno_realizado"])
    assert kept["retorno_ref"].split("/")[0] in ("Appointment", "Encounter")
    lost_miss = next(c for c in complete if c["falta_ref"])
    assert lost_miss["paciente_id"] == "alt10" and lost_miss["retorno_realizado"] is False
    by_month = client.get("/journey/metrics?coorte=mes", headers=h).json()
    assert by_month["total"] == total and all(len(c["chave"]) == 7 for c in by_month["coortes"])
    by_cid = client.get("/journey/metrics?coorte=cid", headers=h).json()
    assert by_cid["total"] == total and {"I50", "J44"} <= {c["chave"] for c in by_cid["coortes"]}
    short = client.get("/journey/metrics?dias=30", headers=h).json()["total"]
    assert short["altas"] < total["altas"]
    assert client.get("/journey/metrics?coorte=x", headers=h).status_code == 422
    assert metrics.compute(jstore, "risco", 365)["total"] == total


def test_alembic_chain_has_single_head_with_journey_tables(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, inspect

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "app/db/migrations"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["c202"]  # #17 (fila de envio) sobre e341 (#34)
    assert {"d201", "d402"} <= {r.revision for r in script.walk_revisions()}
    url = f"sqlite:///{tmp_path / 'm.db'}"
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "database_url", url)
    command.upgrade(cfg, "head")
    tables = inspect(create_engine(url)).get_table_names()
    assert {"journey_contacts", "journey_tasks", "consultations", "organizations", "auth_tokens"} <= set(tables)


# ======================= entradas adversariais (verificação independente) =======================
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/journey/worklist?patient_id=nao-existe", None),
        ("get", "/journey/patients/nao-existe/contact-script", None),
        ("get", "/journey/patients/nao-existe/message-draft", None),
        ("post", "/journey/patients/nao-existe/contacts", {"atendeu": False}),
        ("post", "/journey/tasks", {"patient_id": "nao-existe", "regra": "falta_ao_retorno"}),
    ],
)
def test_unknown_patient_is_404_not_500(client, auth, jstore, method, path, body):
    res = getattr(client, method)(path, headers=auth(), **({"json": body} if body is not None else {}))
    assert res.status_code == 404, res.text


def test_all_endpoints_require_login(client, jstore):
    for path in ("/journey/worklist", "/journey/rules", "/journey/metrics", "/journey/tasks"):
        assert client.get(path).status_code == 401
    assert client.post("/journey/tasks", json={"patient_id": "cen7", "regra": "x"}).status_code == 401
    assert client.post("/journey/patients/cen7/contacts", json={"atendeu": False}).status_code == 401


def test_task_input_validation_and_conflicts(client, auth, jstore):
    h = auth("enfermagem")
    ok = {"patient_id": "cen7b", "regra": "falta_ao_retorno"}
    assert client.post("/journey/tasks", json=ok | {"papel": "faxineiro"}, headers=h).status_code == 422
    assert client.post("/journey/tasks", json=ok | {"prazo_horas": 0}, headers=h).status_code == 422
    assert client.post("/journey/tasks", json=ok | {"prazo_horas": 100000}, headers=h).status_code == 422
    assert client.post("/journey/tasks", json=ok | {"regra": "inventada"}, headers=h).status_code == 422
    # regra que não se aplica a este paciente
    assert client.post("/journey/tasks", json=ok | {"regra": "troca_sem_reconciliacao"}, headers=h).status_code == 409
    # paciente internado: não está na lista
    assert (
        client.post("/journey/tasks", json={"patient_id": "cen1", "regra": "falta_ao_retorno"}, headers=h).status_code
        == 409
    )
    first = client.post("/journey/tasks", json=ok, headers=h)
    assert first.status_code == 201
    assert client.post("/journey/tasks", json=ok, headers=h).status_code == 409  # duplicada
    tid = first.json()["id"]
    assert (
        client.patch(
            f"/journey/tasks/{tid}", json={"status": "cancelada", "observacao": "   curta  "}, headers=h
        ).status_code
        == 422
    )
    assert client.patch(f"/journey/tasks/{tid}", json={"status": "aberta"}, headers=h).status_code == 422
    assert client.patch("/journey/tasks/99999", json={"status": "concluida"}, headers=h).status_code == 404
    assert client.patch(f"/journey/tasks/{tid}", json={"status": "concluida"}, headers=h).status_code == 200
    assert client.patch(f"/journey/tasks/{tid}", json={"status": "concluida"}, headers=h).status_code == 409
    # concluída libera nova tarefa para o mesmo item
    assert client.post("/journey/tasks", json=ok, headers=h).status_code == 201


def test_contact_input_validation(client, auth, jstore):
    h = auth("enfermagem")
    script = client.get("/journey/patients/cen7/contact-script", headers=h).json()["perguntas"]
    url = "/journey/patients/cen7/contacts"
    assert client.post(url, json={"atendeu": True, "canal": "pombo"}, headers=h).status_code == 422
    assert client.post(url, json={"canal": "telefone"}, headers=h).status_code == 422  # atendeu é obrigatório
    assert client.post(url, json={"atendeu": False, "observacoes": "x" * 1001}, headers=h).status_code == 422
    for bad in ("SIM", "talvez", "", "true"):
        res = client.post(url, json={"atendeu": True, "respostas": answers(script, sinal_febre=bad)}, headers=h)
        assert res.status_code == 422, bad
    # nada foi gravado pelas tentativas inválidas
    assert client.get("/journey/patients/cen7/contacts", headers=h).json() == []
    assert find(worklist(client, h), "alta_sem_contato_72h", "cen7") is not None
    # "não se aplica" num sinal de alarme não gera alerta
    res = client.post(url, json={"atendeu": True, "respostas": answers(script, sinal_febre="nao_se_aplica")}, headers=h)
    assert res.status_code == 201 and res.json()["alerta"] is None
    # tentativa sem sucesso ignora respostas enviadas (não há o que registrar)
    res = client.post(url, json={"atendeu": False, "respostas": answers(script, sinal_febre="sim")}, headers=h)
    assert res.status_code == 201 and res.json()["respostas"] == {} and res.json()["alerta"] is None


def test_repeated_alarm_reuses_open_alert_and_text_is_not_executed(client, auth, jstore):
    h = auth("enfermagem")
    script = client.get("/journey/patients/cen7/contact-script", headers=h).json()["perguntas"]
    body = {
        "atendeu": True,
        "respostas": answers(script, sinal_febre="sim"),
        "observacoes": "<script>alert(1)</script>",
    }
    for _ in range(2):
        assert client.post("/journey/patients/cen7/contacts", json=body, headers=h).status_code == 201
    alerts = client.get("/patients/cen7/alerts?status=aberto", headers=h).json()
    assert len([a for a in alerts if a["source"] == "jornada"]) == 1


def test_patient_leaves_list_after_window_and_message_draft_for_discharged(client, auth, jstore, at):
    h = auth("enfermagem")
    at(days=3, hours=1)
    assert find(worklist(client, h), "alta_sem_contato_72h", "cen7") is not None
    at(days=400)
    assert find(worklist(client, h), "alta_sem_contato_72h", "cen7") is None
    draft = client.get("/journey/patients/cen7/message-draft", headers=h).json()
    assert draft["envio"] is False and "192" in draft["texto"]


def test_metrics_input_validation_and_cohorts(client, auth, jstore):
    h = auth("gestor")
    assert client.get("/journey/metrics?coorte=astrologia", headers=h).status_code == 422
    assert client.get("/journey/metrics?dias=29", headers=h).status_code == 422
    assert client.get("/journey/metrics?dias=99999", headers=h).status_code == 422
    for coorte in ("risco", "mes", "cid"):
        res = client.get(f"/journey/metrics?coorte={coorte}&dias=30", headers=h)
        assert res.status_code == 200, res.text
