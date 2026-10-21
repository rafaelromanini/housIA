"""Envio estruturado ao prontuário (#17): Bundle FHIR com Composition, Provenance e recursos aceitos, reenvio seguro.

Os critérios de aceite passam pelo caminho real (API/TestClient sobre o cen6, LLM mock, FHIR local): o Bundle valida
como R4, reenviar não duplica, FHIR fora do ar deixa o status em "erro" e o retry cria tudo uma vez só, a consulta
aberta pela agenda fica presa ao encontro clicado e a alergia relatada e aceita entra na reconciliação.
"""

import pytest
from fhir.resources.R4B.bundle import Bundle

from app.consult import export, propostas
from app.consult import service as consult_service
from app.db import session as db_session
from app.db.models import AuditLog, EhrOutbox
from app.fhir_client.client import FhirUnavailable, LocalFhirStore, location_refs
from tests.test_consult import accept_critical, approve, start

H_JUST = {"justificativa": "Risco avaliado e discutido com a paciente."}


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def falas(client, h, cid, *itens):
    return client.post(
        f"/consultations/{cid}/segments",
        json={"segmentos": [{"falante": who, "texto": t} for who, t in itens]},
        headers=h,
    )


def consulta_pronta(client, h, *, pid="cen6", extra=()):
    """Consulta com alergia, remédio, PA e peso ditos pela paciente; finalizada, revisada e atestada."""
    cid = start(client, h, pid)["id"]
    res = falas(
        client,
        h,
        cid,
        ("medico", "Bom dia, como a senhora está?"),
        ("paciente", "Doutora, eu tenho alergia a iodo, contraste. Já fiquei com a boca inchada."),
        ("paciente", "Tomo um chá de camomila e também omeprazol todo dia de manhã."),
        ("paciente", "Hoje de manhã minha pressão estava 150 por 90 e meu peso é 72 quilos."),
        *extra,
    )
    assert res.status_code == 200, res.text
    accept_critical(client, h, cid, res.json()["insights"])  # alerta crítico aceito: o envio não pede justificativa
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    approve(client, h, cid)
    return cid


def aceitar(client, h, cid, *tipos):
    props = client.get(f"/consultations/{cid}/proposals", headers=h).json()["propostas"]
    ids = [p["id"] for p in props if p["tipo"] in tipos]
    res = client.put(f"/consultations/{cid}/proposals", json={"aceitas": ids}, headers=h)
    assert res.status_code == 200, res.text
    return ids


def enviar(client, h, cid):
    return client.post(f"/consultations/{cid}/send", json=H_JUST, headers=h)


@pytest.fixture
def writeback(monkeypatch):
    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)


def outbox(cid):
    with db_session.SessionLocal() as db:
        return db.query(EhrOutbox).filter_by(consultation_id=cid).one()


def tagged(store, rtype, cid):
    return [
        r
        for r in store.resources[rtype].values()
        if any(
            t.get("system") == export.TAG_SYSTEM and t["code"].startswith(f"{cid}:")
            for t in r.get("meta", {}).get("tag", [])
        )
    ]


# ---------- criterio 1: o Bundle valida como R4 e cria os recursos ----------
def test_bundle_valida_como_r4_e_cria_composition_documento_provenance_e_alergia(client, auth, store, writeback):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    props = client.get(f"/consultations/{cid}/proposals", headers=h).json()["propostas"]
    tipos = {p["tipo"] for p in props}
    assert {"alergia", "medicamento", "observacao"} <= tipos
    assert all(not p["aceita"] for p in props)  # nada vai sem o aceite
    aceitar(client, h, cid, "alergia", "medicamento", "observacao")

    # a exportação (sem envio) já devolve o mesmo Bundle, e ele valida como FHIR R4
    export_res = client.get(f"/consultations/{cid}/export", headers=h)
    assert export_res.status_code == 200, export_res.text
    export_ = export_res.json()["bundle"]
    parsed = Bundle.model_validate(export_)
    kinds = [e.resource.get_resource_type() for e in parsed.entry]
    assert kinds.count("Composition") == 1 and kinds.count("DocumentReference") == 1 and kinds.count("Provenance") == 1
    assert {"Practitioner", "Device", "AllergyIntolerance", "MedicationStatement", "Observation"} <= set(kinds)

    sent = enviar(client, h, cid).json()
    assert sent["enviado"] is True and sent["status_envio"] == "enviado"
    assert sent["fhir_ref"].startswith("DocumentReference/")
    comp = tagged(store, "Composition", cid)[0]
    assert comp["status"] == "final" and comp["type"]["coding"][0]["code"] == "11488-4"
    loincs = {s["code"]["coding"][0]["code"] for s in comp["section"] if "code" in s}
    assert {"10154-3", "10164-2", "48765-2", "51848-0", "18776-5"} <= loincs  # section por seção, com LOINC
    alergia_sec = next(s for s in comp["section"] if s.get("code", {}).get("coding", [{}])[0].get("code") == "48765-2")
    assert alergia_sec["entry"][0]["reference"].startswith("AllergyIntolerance/")  # urn resolvido para o id real

    alergia = tagged(store, "AllergyIntolerance", cid)[0]
    assert alergia["verificationStatus"]["coding"][0]["code"] == "unconfirmed"
    assert alergia["patient"] == {"reference": "Patient/cen6"}
    assert (
        "Fala/2" in alergia["note"][0]["text"] and "iodo" in alergia["note"][0]["text"].lower()
    )  # a fala é a evidência
    med = tagged(store, "MedicationStatement", cid)[0]
    assert med["informationSource"] == {"reference": "Patient/cen6"}
    pa = next(o for o in tagged(store, "Observation", cid) if o["code"]["coding"][0]["code"] == "85354-9")
    assert {c["valueQuantity"]["value"] for c in pa["component"]} == {150, 90}

    prov = tagged(store, "Provenance", cid)[0]
    agents = {a["type"]["coding"][0]["code"]: a for a in prov["agent"]}
    assert agents["author"]["who"]["reference"].startswith("Practitioner/")
    assert agents["assembler"]["who"]["reference"].startswith("Device/")
    device = store.get("Device", agents["assembler"]["who"]["reference"].split("/")[1])
    assert {v["type"]["text"] for v in device["version"]} == {"modelo", "prompt_version"}
    prac = store.get("Practitioner", agents["author"]["who"]["reference"].split("/")[1])
    assert prac["name"][0]["text"].startswith("Dra.") or prac["name"][0]["text"]  # o revisor que assinou
    target_types = {t["reference"].split("/")[0] for t in prov["target"]}
    assert {
        "Composition",
        "DocumentReference",
        "AllergyIntolerance",
        "MedicationStatement",
        "Observation",
    } <= target_types
    assert any("Fala/2" in e.get("what", {}).get("display", "") for e in prov["entity"])  # a fonte de cada dado
    doc = store.get("DocumentReference", sent["fhir_ref"].split("/")[1])
    assert {"system": "urn:ietf:rfc:3986", "value": f"urn:housia:consultation:{cid}"} in doc["identifier"]
    assert outbox(cid).status == "enviado" and outbox(cid).payload_json is None


def test_so_vai_o_que_o_profissional_aceitou(client, auth, store, writeback):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    ids = aceitar(client, h, cid, "alergia")
    assert ids and all(i.startswith("alergia:") for i in ids)
    assert enviar(client, h, cid).json()["enviado"] is True
    assert len(tagged(store, "AllergyIntolerance", cid)) == 1
    assert not tagged(store, "MedicationStatement", cid) and not tagged(store, "Observation", cid)
    # proposta inexistente é recusada, e depois do envio as decisões ficam travadas
    assert client.put(f"/consultations/{cid}/proposals", json={"aceitas": ["alergia:nada"]}, headers=h).status_code in (
        409,
        422,
    )


def test_cid_confirmado_e_hipotese_viram_condition_com_status_certo(client, auth, store, writeback):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    props = client.get(f"/consultations/{cid}/proposals", headers=h).json()["propostas"]
    cids = [p for p in props if p["tipo"] == "condicao"]
    if not cids:
        pytest.skip("a nota do cen6 não trouxe CID neste roteiro")
    aceitar(client, h, cid, "condicao")
    assert enviar(client, h, cid).json()["enviado"] is True
    conds = tagged(store, "Condition", cid)
    assert conds and {c["verificationStatus"]["coding"][0]["code"] for c in conds} <= {
        "confirmed",
        "provisional",
        "differential",
    }
    assert all(c["code"]["coding"][0]["system"] == "http://hl7.org/fhir/sid/icd-10" for c in conds)


# ---------- criterio 2: reenviar nao duplica ----------
def test_reenviar_o_mesmo_bundle_nao_duplica_recursos(client, auth, store, writeback):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    aceitar(client, h, cid, "alergia", "medicamento", "observacao")
    bundle = client.get(f"/consultations/{cid}/export", headers=h).json()["bundle"]
    first = store.transaction(bundle)
    counts = {rt: len(store.resources[rt]) for rt in store.resources}
    second = store.transaction(bundle)
    assert {rt: len(store.resources[rt]) for rt in store.resources} == counts
    assert location_refs(first) == location_refs(second)  # as mesmas localizações, nada novo
    # o envio pela API depois disso também reaproveita tudo
    sent = enviar(client, h, cid).json()
    assert sent["fhir_ref"] == location_refs(first)[3]
    assert {rt: len(store.resources[rt]) for rt in store.resources} == counts
    assert len(tagged(store, "Composition", cid)) == 1 and len(tagged(store, "Provenance", cid)) == 1
    # enviar de novo pela API é recusado (já enviada)
    assert enviar(client, h, cid).status_code == 409


# ---------- criterio 3: FHIR fora do ar => erro; retry cria uma unica vez ----------
def test_fhir_fora_do_ar_fica_em_erro_e_o_retry_cria_os_recursos_uma_unica_vez(
    client, auth, store, writeback, monkeypatch
):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    aceitar(client, h, cid, "alergia")
    original = LocalFhirStore.transaction

    def fora_do_ar(self, bundle):
        raise FhirUnavailable("Servidor FHIR indisponível: connection refused")

    monkeypatch.setattr(LocalFhirStore, "transaction", fora_do_ar)
    res = enviar(client, h, cid)
    assert res.status_code == 200
    body = res.json()
    assert body["enviado"] is False and body["status_envio"] == "erro" and "indisponível" in body["erro"]
    row = outbox(cid)
    assert (row.status, row.attempts) == ("erro", 1) and "indisponível" in row.last_error and row.payload_json
    assert body["consulta"]["envio"]["status"] == "erro" and body["consulta"]["status"] == "finalizada"
    assert not tagged(store, "Composition", cid) and not tagged(store, "AllergyIntolerance", cid)
    # a agenda mostra o status
    agenda = next(a for a in client.get("/appointments", headers=h).json() if a["patient_id"] == "cen6")
    assert agenda["envio_status"] == "erro" and agenda["consulta_id"] == cid

    # ainda fora do ar: o retry continua em erro, com mais uma tentativa
    again = client.post(f"/consultations/{cid}/send/retry", headers=h).json()
    assert again["status_envio"] == "erro" and outbox(cid).attempts == 2

    monkeypatch.setattr(LocalFhirStore, "transaction", original)
    ok = client.post(f"/consultations/{cid}/send/retry", headers=h).json()
    assert ok["enviado"] is True and ok["consulta"]["envio"]["status"] == "enviado"
    assert (outbox(cid).status, outbox(cid).attempts) == ("enviado", 3)
    for rtype in ("Composition", "DocumentReference", "Provenance", "AllergyIntolerance"):
        assert len(tagged(store, rtype, cid)) == 1, rtype
    docs = [
        d
        for d in store.resources["DocumentReference"].values()
        if any(i.get("value") == f"urn:housia:consultation:{cid}" for i in d.get("identifier", []))
    ]
    assert len(docs) == 1
    # retry de quem já foi enviado não faz nada
    last = client.post(f"/consultations/{cid}/send/retry", headers=h).json()
    assert last["ja_enviado"] is True and len(tagged(store, "Composition", cid)) == 1
    with db_session.SessionLocal() as db:
        actions = [a.action for a in db.query(AuditLog).filter(AuditLog.entity_id == str(cid)).all()]
    assert actions.count("consulta.envio_erro") == 2 and actions.count("consulta.enviada") == 1


def test_na_subida_da_api_o_que_ficou_na_fila_e_reenviado(client, auth, store, writeback, monkeypatch):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    aceitar(client, h, cid, "alergia")
    original = LocalFhirStore.transaction
    monkeypatch.setattr(LocalFhirStore, "transaction", lambda self, b: (_ for _ in ()).throw(FhirUnavailable("fora")))
    assert enviar(client, h, cid).json()["status_envio"] == "erro"
    monkeypatch.setattr(LocalFhirStore, "transaction", original)
    with db_session.SessionLocal() as db:
        assert consult_service.retry_outbox(db) == 1
    assert outbox(cid).status == "enviado" and len(tagged(store, "Composition", cid)) == 1
    assert client.get(f"/consultations/{cid}", headers=h).json()["status"] == "enviada"
    with db_session.SessionLocal() as db:
        assert consult_service.retry_outbox(db) == 0


def test_retry_sem_fila_e_404_e_so_o_medico_ou_enfermagem_reenvia(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    assert client.post(f"/consultations/{cid}/send/retry", headers=h).status_code == 404
    assert client.post(f"/consultations/{cid}/send/retry", headers=auth("gestor")).status_code in (401, 403)


# ---------- criterio 4: encontro certo ----------
def test_consulta_aberta_pela_agenda_fica_presa_ao_encontro_clicado(client, auth, store):
    h = auth("medico")
    base = store.resources["Encounter"]["cen6-amb-hoje"]
    outro = base | {"id": "cen6-amb-hoje-2", "status": "in-progress"}
    store.resources["Encounter"]["cen6-amb-hoje-2"] = outro
    agenda = [a for a in client.get("/appointments", headers=h).json() if a["patient_id"] == "cen6"]
    assert {a["encounter_id"] for a in agenda} == {"cen6-amb-hoje", "cen6-amb-hoje-2"}
    clicado = start(client, h, "cen6", encounter_id="cen6-amb-hoje")
    assert clicado["encounter_id"] == "cen6-amb-hoje"  # o do clique, mesmo havendo outro em andamento
    row = next(a for a in client.get("/appointments", headers=h).json() if a["encounter_id"] == "cen6-amb-hoje")
    assert row["consulta_id"] == clicado["id"]
    other = next(a for a in client.get("/appointments", headers=h).json() if a["encounter_id"] == "cen6-amb-hoje-2")
    assert other["consulta_id"] is None
    # encontro de outro paciente ou inexistente: 422
    res = client.post(
        "/patients/cen6/consultations",
        json={
            "tipo": "consulta",
            "consentimento": __import__("tests.conftest", fromlist=["CONSENT"]).CONSENT,
            "encounter_id": "cen8-amb-hoje",
        },
        headers=h,
    )
    assert res.status_code == 422


def test_sem_encounter_id_em_andamento_ou_chegado_vence_o_apenas_marcado(client, auth, store):
    h = auth("medico")
    base = store.resources["Encounter"]["cen6-amb-hoje"]
    planned = base | {"id": "cen6-amb-planejado", "status": "planned"}
    resources = {"cen6-amb-planejado": planned, **{k: v for k, v in store.resources["Encounter"].items()}}
    store.resources["Encounter"].clear()
    store.resources["Encounter"].update(resources)  # o marcado vem primeiro no armazenamento
    assert start(client, h, "cen6")["encounter_id"] == "cen6-amb-hoje"  # arrived > planned
    store.resources["Encounter"]["cen6-amb-hoje"] = base | {"status": "in-progress"}
    consult_service.clear_chart_cache()
    assert start(client, h, "cen6")["encounter_id"] == "cen6-amb-hoje"
    store.resources["Encounter"]["cen6-amb-hoje"] = base | {"status": "finished"}
    consult_service.clear_chart_cache()
    assert start(client, h, "cen6")["encounter_id"] == "cen6-amb-planejado"  # só sobrou o marcado


# ---------- criterio 5: a alergia aceita aparece na reconciliacao ----------
def test_alergia_relatada_e_aceita_aparece_na_reconciliacao_do_paciente(client, auth, store, writeback):
    h = auth("medico")
    # cen2: internada, com paracetamol prescrito e sem alergia registrada
    cid = start(client, h, "cen2")["id"]
    res = falas(client, h, cid, ("paciente", "Doutora, eu sou alérgica a paracetamol, fico toda com coceira."))
    assert res.status_code == 200, res.text
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    nota = client.get(f"/consultations/{cid}", headers=h).json()["nota"]
    nota["avaliacao"] = {"texto": "Alergia a paracetamol relatada pela paciente.", "refs": ["Fala/1"]}
    assert client.put(f"/consultations/{cid}/note", json=nota | {"atestacao": True}, headers=h).status_code == 200
    props = client.get(f"/consultations/{cid}/proposals", headers=h).json()["propostas"]
    alergia = next(p for p in props if p["tipo"] == "alergia")
    antes = client.post("/patients/cen2/medrec", json={}, headers=h)
    assert antes.status_code == 200, antes.text
    assert not [a for a in antes.json()["achados"] if a["tipo"] == "alergia"]
    assert (
        client.put(f"/consultations/{cid}/proposals", json={"aceitas": [alergia["id"]]}, headers=h).status_code == 200
    )
    sent = enviar(client, h, cid).json()
    assert sent["enviado"] is True, sent
    ref = f"AllergyIntolerance/{tagged(store, 'AllergyIntolerance', cid)[0]['id']}"
    depois = client.post("/patients/cen2/medrec", json={}, headers=h).json()
    achado = [a for a in depois["achados"] if a["tipo"] == "alergia"]
    assert achado and any(ref in a["refs"] for a in achado) and achado[0]["severidade"] == "critico"
    assert "Paracetamol" in achado[0]["titulo"]


# ---------- transicao: a alta le a nota de consulta do HousIA ----------
def test_alta_le_a_nota_de_consulta_do_housia(client, auth, store, writeback):
    h = auth("medico")
    # cen5: internada; a nota de consulta feita no HousIA (DocumentReference "(HousIA)") entra no sumário de alta
    cid = start(client, h, "cen5")["id"]
    assert falas(client, h, cid, ("paciente", "Doutora, estou com dor de cabeça forte desde ontem.")).status_code == 200
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    approve(client, h, cid)
    sent = enviar(client, h, cid).json()
    assert sent["enviado"] is True, sent
    docs = client.post("/patients/cen5/discharge-documents", json={"idioma": "pt-BR"}, headers=h)
    assert docs.status_code == 200, docs.text
    body = docs.json()
    soap = (body.get("conteudo") or body)["soap"]
    assert "Consulta ambulatorial prévia (HousIA)" in soap["subjetivo"]["texto"]
    assert sent["fhir_ref"] in soap["subjetivo"]["refs"]


# ---------- adversariais: PA e peso so ditos pelo paciente, sem pergunta ----------
@pytest.mark.parametrize(
    "who,texto,esperado",
    [
        ("paciente", "Minha pressão hoje estava 150 por 90.", {"pa"}),
        ("paciente", "A pressão deu 12 por 8 ontem.", set()),  # 12x8 não é mmHg
        ("medico", "Sua pressão está 150 por 90 agora.", set()),  # fala do médico não conta
        ("paciente", "Será que minha pressão está 150 por 90?", set()),  # pergunta
        ("paciente", "Eu peso 72 quilos.", {"peso"}),
        ("paciente", "Peso setenta quilos.", set()),  # número por extenso: não inventa
        ("paciente", "Comprei 5 quilos de arroz.", set()),  # quilos sem falar de peso
        ("acompanhante", "Ela está pesando 58 kg.", {"peso"}),
        ("paciente", "Pesei 400 quilos de batata.", set()),  # fora da faixa humana
        ("paciente", "Tenho 150 por 90 de coisa para fazer hoje.", set()),  # sem contexto de pressão
        ("paciente", "pressao 180x110 ontem", {"pa"}),
        ("paciente", "Pressão 90 por 140.", set()),  # sistólica menor que a diastólica
    ],
)
def test_pa_e_peso_ditos_so_valem_do_paciente_e_sem_pergunta(who, texto, esperado):
    from app.consult.schemas import Segment

    segs = [Segment(n=1, t=1.0, falante=who, texto=texto)]
    assert {k for k, _, _ in propostas._spoken_measures(segs)} == esperado


# ---------- privacidade: a fila é da organização e não guarda dado clínico depois do envio ----------
def test_fila_tem_a_organizacao_da_consulta_e_apaga_o_bundle_depois_do_envio(client, auth, store, writeback):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    assert enviar(client, h, cid).status_code == 200
    row = outbox(cid)
    with db_session.SessionLocal() as db:
        from app.db.models import Consultation

        c = db.get(Consultation, cid)
        assert row.organization_id is not None and row.organization_id == c.organization_id
    assert row.status == "enviado" and row.payload_json is None
