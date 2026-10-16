"""Pedidos e prescrições em rascunho a partir da fala, checados antes de propor (#19).

Os critérios de aceite passam pelo caminho real (API/TestClient sobre o cen6, LLM mock): 'vamos pedir hemograma e
creatinina' gera 2 rascunhos com Fala/N; amoxicilina em alérgico à penicilina fica crítico e só é assinada com
justificativa; 'aumentar a losartana para 100' vira mudança de dose; creatinina de 10 dias gera aviso de repetição; o
envio cria ServiceRequest draft no FHIR local. As frases adversariais passam pelo Engine sobre o prontuário.
"""

import csv
import re
from datetime import timedelta

import pytest

from app import clock
from app.audit import service as audit_service  # noqa: F401  (garante o modelo AuditLog carregado)
from app.consult import ai as consult_ai
from app.consult import knowledge, orders
from app.consult import service as consult_service
from app.consult.engine import Engine
from app.consult.schemas import Insights, Segment
from app.db import session as db_session
from app.db.models import AuditLog
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from app.timeline.record import PatientRecord
from tests.test_consult import start


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def say(client, h, cid, *falas: tuple[str, str]) -> dict:
    segs = [{"falante": who, "texto": t} for who, t in falas]
    res = client.post(f"/consultations/{cid}/segments", json={"segmentos": segs}, headers=h)
    assert res.status_code == 200, res.text
    return res.json()["insights"]


def by_item(ins: dict, item: str) -> dict:
    return next(d for d in ins["pedidos"] if d["item"] == item)


def decide(client, h, cid, draft_id, **body):
    return client.put(f"/consultations/{cid}/pedidos/{draft_id}", json=body, headers=h)


def engine_for(record, pid: str, falas: list[tuple[str, str]], kind: str = "consulta") -> Engine:
    rec = record(pid)
    segs = [Segment(n=i, t=float(i), falante=who, texto=t) for i, (who, t) in enumerate(falas, 1)]  # type: ignore[arg-type]
    return Engine(rec, build_timeline(rec), build_context(rec), segs, "clinica_medica", kind, clock.now())


def spoken(record, pid: str, texto: str, who: str = "medico") -> list:
    """Rascunhos que saem de UMA fala (sem as sugestões de protocolo e de hipótese)."""
    return [d for d in engine_for(record, pid, [(who, texto)]).run().pedidos if d.fonte_pedido == "fala"]


# ---------- critérios de aceite ----------
def test_hemograma_e_creatinina_geram_dois_rascunhos_com_fala(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("medico", "Vamos pedir hemograma e creatinina."))
    exames = [d for d in ins["pedidos"] if d["fonte_pedido"] == "fala"]
    assert sorted(d["item"] for d in exames) == ["Creatinina", "Hemograma completo"]
    assert all(d["tipo"] == "exame" and d["acao"] == "solicitar" and d["refs"] == ["Fala/1"] for d in exames)
    creat = by_item(ins, "Creatinina")
    assert creat["loinc"] == "2160-0" and creat["preparo"]  # LOINC e preparo vêm do catálogo
    assert creat["decisao"]["estado"] == "pendente"


def test_amoxicilina_em_alergica_a_penicilina_e_critica_e_so_assina_com_justificativa(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("medico", "Vou começar amoxicilina 500 mg de 8 em 8 horas por 7 dias."))
    d = by_item(ins, "Amoxicilina")
    assert (d["dose"], d["frequencia"], d["duracao"], d["duracao_dias"]) == ("500 mg", "de 8 em 8 horas", "7 dias", 7)
    assert d["periodicidade"] == {"frequencia": 1, "periodo": 8, "unidade": "h"}
    assert d["gravidade"] == "critico" and d["refs"] == ["Fala/1"]
    alergia = next(c for c in d["checagens"] if c["tipo"] == "alergia")
    assert alergia["severidade"] == "critico"
    assert alergia["refs"] == ["AllergyIntolerance/cen6-alg-0"]
    # sem justificativa, ou com uma curta demais, a assinatura é barrada (409) e nada é gravado
    sem = decide(client, h, cid, d["id"], estado="assinado")
    assert sem.status_code == 409 and sem.json()["detail"]["codigo"] == "pedido_critico"
    assert sem.json()["detail"]["checagens"][0]["tipo"] == "alergia"
    assert decide(client, h, cid, d["id"], estado="assinado", justificativa="urgente").status_code == 409
    assert (
        by_item(client.get(f"/consultations/{cid}", headers=h).json()["insights"], "Amoxicilina")["decisao"]["estado"]
        == "pendente"
    )
    ok = decide(
        client,
        h,
        cid,
        d["id"],
        estado="assinado",
        justificativa="Dessensibilização acompanhada pela alergia; paciente ciente do risco.",
    )
    assert ok.status_code == 200, ok.text
    assert by_item(ok.json()["insights"], "Amoxicilina")["decisao"]["estado"] == "assinado"
    log = [a for a in audit_rows() if a.action == "consulta.pedido_assinado"]
    assert (
        log
        and log[-1].payload_json["alertas_criticos"]
        and "Dessensibilização" in log[-1].payload_json["justificativa"]
    )


def audit_rows() -> list[AuditLog]:
    from sqlalchemy import select

    with db_session.SessionLocal() as db:
        return list(db.scalars(select(AuditLog).order_by(AuditLog.id)))


def test_aumentar_a_losartana_para_100_e_mudanca_de_dose(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("medico", "Aumentar a losartana para 100."))
    d = by_item(ins, "Losartana")
    assert d["acao"] == "alterar_dose" and d["tipo"] == "medicamento"
    assert d["dose"] == "100 mg" and d["dose_anterior"] == "50 mg"  # a unidade vem da prescrição ativa
    assert d["prescricao_ref"] == "MedicationRequest/cen6-rx-losartana-c1"
    assert d["refs"] == ["Fala/1"]
    # a dose do rascunho segue para o FHIR como mudança (priorPrescription), não como início
    resource = orders.medication_request(
        orders.OrderDraft.model_validate(d),
        patient_id="cen6",
        encounter_id=None,
        author="Dra. X",
        when=clock.now(),
        consultation_id=1,
    )
    assert resource["priorPrescription"] == {"reference": "MedicationRequest/cen6-rx-losartana-c1"}
    assert resource["dosageInstruction"][0]["doseAndRate"][0]["doseQuantity"] == {"value": 100.0, "unit": "mg"}


def test_creatinina_feita_ha_10_dias_gera_aviso_de_repeticao(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("medico", "Vamos pedir hemograma e creatinina."))
    aviso = next(c for c in by_item(ins, "Creatinina")["checagens"] if c["tipo"] == "exame_repetido")
    assert aviso["severidade"] == "atencao" and "10 dias" in aviso["texto"] and "1,5 mg/dL" in aviso["texto"]
    assert aviso["refs"] == ["Observation/cen6-lab2-creatinina"]
    # um exame sem resultado recente não gera aviso de repetição (TSH nunca foi feito)
    ins = say(client, h, cid, ("medico", "Vou pedir TSH."))
    assert not [c for c in by_item(ins, "TSH")["checagens"] if c["tipo"] == "exame_repetido"]


def test_envio_cria_service_request_e_medication_request_draft_no_fhir_local(client, auth, monkeypatch, store):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(
        client,
        h,
        cid,
        ("medico", "Vamos pedir hemograma e creatinina."),
        ("medico", "Vou começar dipirona 1 g de 6 em 6 horas se tiver dor, por 3 dias."),
    )
    for d in ins["pedidos"]:
        if d["fonte_pedido"] == "fala":
            assert decide(client, h, cid, d["id"], estado="assinado").status_code == 200
    recusado = [d for d in ins["pedidos"] if d["fonte_pedido"] != "fala"]
    if recusado:
        assert decide(client, h, cid, recusado[0]["id"], estado="recusado").status_code == 200
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200

    # EHR_WRITEBACK desligado: o Bundle exporta os rascunhos assinados (e só eles)
    export = client.post(f"/consultations/{cid}/send", headers=h).json()
    assert export["enviado"] is False and len(export["pedidos"]) == 3
    assert export["bundle"]["type"] == "transaction" and len(export["bundle"]["entry"]) == 4  # nota + 3 pedidos
    assert "Pedidos e prescrições assinados" in export["texto"] and "Dipirona" in export["texto"]

    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    before = {rt: set(store.resources[rt]) for rt in ("ServiceRequest", "MedicationRequest")}
    sent = client.post(f"/consultations/{cid}/send", headers=h).json()
    try:
        assert sent["enviado"] is True
        made = [r for r in sent["pedidos"]]
        assert sorted(r.split("/")[0] for r in made) == ["MedicationRequest", "ServiceRequest", "ServiceRequest"]
        srs = [store.resources["ServiceRequest"][r.split("/")[1]] for r in made if r.startswith("ServiceRequest")]
        assert all(sr["status"] == "draft" and sr["intent"] == "proposal" for sr in srs)
        assert {sr["code"]["coding"][0]["code"] for sr in srs} == {"58410-2", "2160-0"}  # LOINC do catálogo
        assert all(sr["supportingInfo"] == [{"reference": sent["fhir_ref"]}] for sr in srs)
        mr = store.resources["MedicationRequest"][next(r for r in made if r.startswith("Medication")).split("/")[1]]
        assert mr["status"] == "draft" and mr["intent"] == "proposal"
        dosage = mr["dosageInstruction"][0]
        assert dosage["asNeededBoolean"] is True and dosage["timing"]["repeat"]["boundsDuration"]["value"] == 3
        # a prescrição proposta NÃO entra na prescrição ativa nem na reconciliação como medicamento em uso
        rec = PatientRecord.load(store, "cen6")
        assert "dipirona" not in {m.ingredient for m in rec.active_meds}
        assert [m.ingredient for m in rec.proposed_meds] == ["dipirona"]
        assert any(p.texto.startswith(("Hemograma", "Creatinina")) and p.proposto for p in rec.pending_exams)
    finally:
        for rt, ids in before.items():
            for rid in set(store.resources[rt]) - ids:
                store.resources[rt].pop(rid, None)


def test_prescricao_proposta_aparece_na_reconciliacao_do_paciente(client, auth, store):
    """A prescrição assinada na consulta entra na reconciliação, conferida contra alergias e o que já está prescrito."""
    h = auth("medico")
    # cen5: internada, com alergia registrada a dipirona
    draft = orders.OrderDraft(
        id="med-1-dipirona-iniciar",
        tipo="medicamento",
        acao="iniciar",
        item="Dipirona",
        chave="dipirona",
        dose="1 g",
        frequencia="de 6 em 6 horas",
        refs=["Fala/1"],
        decisao={"estado": "assinado", "justificativa": "teste", "por": "x", "em": "y"},
    )
    mr = orders.medication_request(
        draft, patient_id="cen5", encounter_id=None, author="Dra. Helena", when=clock.now(), consultation_id=99
    ) | {"id": "teste-prop-dipirona"}
    store.resources["MedicationRequest"][mr["id"]] = mr
    try:
        run = client.post("/patients/cen5/medrec", json={}, headers=h)
        assert run.status_code == 200, run.text
        body = run.json()
        prop = next(p for p in body["prescricoes_propostas"] if p["ref"] == "MedicationRequest/teste-prop-dipirona")
        assert prop["prescritor"] == "Dra. Helena" and prop["acao"] == "nova" and prop["alertas"]
        achado = next(a for a in body["achados"] if a["titulo"].startswith("Prescrição proposta — Alergia"))
        assert achado["severidade"] == "critico" and "MedicationRequest/teste-prop-dipirona" in achado["refs"]
        # o rascunho não é tratado como medicamento em uso: nenhuma linha da tabela de reconciliação o cita
        assert all("MedicationRequest/teste-prop-dipirona" not in row["refs"] for row in body["linhas"])
        timeline = client.get("/patients/cen5/timeline?kinds=med_order", headers=h).json()
        assert any(e["source_ref"].endswith("teste-prop-dipirona") and "rascunho" in e["summary"] for e in timeline)
    finally:
        store.resources["MedicationRequest"].pop("teste-prop-dipirona", None)


# ---------- assinatura, edição e travas ----------
def test_assinar_exige_dose_edicao_corrige_e_recusar_nao_exige_justificativa(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("medico", "Vamos aumentar a losartana."), ("medico", "Vou pedir ferritina."))
    losartana = by_item(ins, "Losartana")
    assert losartana["dose"] == ""  # o médico não disse a dose: o rascunho não inventa
    assert decide(client, h, cid, losartana["id"], estado="assinado").status_code == 422
    fixed = decide(client, h, cid, losartana["id"], estado="assinado", dose="75 mg", frequencia="1 vez ao dia")
    assert fixed.status_code == 200
    d = by_item(fixed.json()["insights"], "Losartana")
    assert (d["dose"], d["frequencia"], d["decisao"]["estado"], d["decisao"]["editado"]) == (
        "75 mg",
        "1 vez ao dia",
        "assinado",
        True,
    )
    assert d["periodicidade"] == {"frequencia": 1, "periodo": 1, "unidade": "d"}
    ferritina = by_item(ins, "Ferritina")
    assert decide(client, h, cid, ferritina["id"], estado="recusado").status_code == 200
    back = decide(client, h, cid, ferritina["id"], estado="pendente").json()
    assert by_item(back["insights"], "Ferritina")["decisao"]["estado"] == "pendente"
    assert decide(client, h, cid, "med-9-inexistente", estado="recusado").status_code == 404


def test_pedidos_nao_assinados_ou_recusados_nao_vao_para_o_prontuario(client, auth, monkeypatch, store):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("medico", "Vou pedir ferritina e TSH."))
    assert decide(client, h, cid, by_item(ins, "TSH")["id"], estado="assinado").status_code == 200
    assert decide(client, h, cid, by_item(ins, "Ferritina")["id"], estado="recusado").status_code == 200
    client.post(f"/consultations/{cid}/finish", headers=h)
    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    before = set(store.resources["ServiceRequest"])
    sent = client.post(f"/consultations/{cid}/send", headers=h).json()
    try:
        assert len(sent["pedidos"]) == 1
        sr = store.resources["ServiceRequest"][sent["pedidos"][0].split("/")[1]]
        assert sr["code"]["text"] == "TSH"
        # depois do envio os rascunhos ficam travados
        again = decide(client, h, cid, by_item(ins, "Ferritina")["id"], estado="assinado")
        assert again.status_code == 409
    finally:
        for rid in set(store.resources["ServiceRequest"]) - before:
            store.resources["ServiceRequest"].pop(rid, None)


def test_so_o_medico_assina_a_enfermagem_recusa_e_o_gestor_nao_decide(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("medico", "Vou pedir TSH e ferritina."))
    tsh, ferritina = by_item(ins, "TSH")["id"], by_item(ins, "Ferritina")["id"]
    assert decide(client, auth("gestor"), cid, tsh, estado="assinado").status_code == 403
    nurse = auth("enfermagem")
    assert decide(client, nurse, cid, tsh, estado="assinado").status_code == 403
    assert decide(client, nurse, cid, ferritina, estado="recusado").status_code == 200
    assert decide(client, h, cid, tsh, estado="assinado").status_code == 200


def test_rascunhos_citam_so_refs_validas_e_o_modo_cirurgico_nao_gera(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(
        client, h, cid, ("medico", "Vamos pedir hemograma e creatinina."), ("medico", "Aumentar a losartana para 100.")
    )
    assert ins["pedidos"] and all(d["refs"] for d in ins["pedidos"])
    from tests.test_consult import all_refs

    refs = set(all_refs(ins["pedidos"]))
    assert refs and all(re.match(r"^[A-Z][A-Za-z]+/[\w.-]+$", r) for r in refs)
    surgery = client.post(
        "/patients/cen4b/consultations", json={"tipo": "cirurgia", "especialidade": "ortopedia"}, headers=h
    )
    sid = surgery.json()["id"]
    out = say(client, h, sid, ("medico", "Vou pedir hemograma e começar cefazolina 2 g."))
    assert out["pedidos"] == []  # o ditado cirúrgico tem os próprios pedidos propostos (relatório)


# ---------- sugestões das regras ----------
def test_protocolo_pendente_vira_sugestao_de_pedido(client, auth):
    h = auth("medico")
    created = client.post("/patients/cen7e/consultations", json={"tipo": "consulta"}, headers=h).json()
    sugestoes = {d["chave"]: d for d in created["insights"]["pedidos"]}
    hba1c = sugestoes["hba1c"]
    assert hba1c["fonte_pedido"] == "protocolo" and hba1c["acao"] == "solicitar" and hba1c["loinc"] == "4548-4"
    assert "Protocolo Diabetes tipo 2" in hba1c["justificativa"]
    assert hba1c["refs"][0] == "Condition/cen7e-dx-0"
    assert "ecg" in sugestoes and sugestoes["ecg"]["fonte_pedido"] == "protocolo"  # item de texto do protocolo
    # a sugestão pode ser assinada item a item como qualquer rascunho
    ok = decide(client, h, created["id"], hba1c["id"], estado="assinado")
    assert ok.status_code == 200 and by_item(ok.json()["insights"], hba1c["item"])["decisao"]["estado"] == "assinado"


def test_proximo_passo_da_hipotese_vira_sugestao_com_a_fonte_da_hipotese(record):
    falas = [("paciente", "Doutora, estou com tontura quando levanto e suor frio.")]
    eng = engine_for(record, "cen6", falas)
    ins = eng.run()
    # com os exames de 10 dias atrás, repetir creatinina/potássio/sódio seria ruído: nada é sugerido por hipótese
    assert not [d for d in ins.pedidos if d.fonte_pedido == "hipotese"]
    eng.chart.now = eng.chart.now + timedelta(days=45)  # 45 dias depois, o intervalo de repetição já passou
    ins = eng.run()
    hip = [d for d in ins.pedidos if d.fonte_pedido == "hipotese"]
    assert {"potassio", "sodio"} <= {d.chave for d in hip}
    for d in hip:
        assert d.refs and d.justificativa.startswith("Próximo passo de «") and d.acao == "solicitar"
        origem = next(x for x in ins.hipoteses if d.justificativa.startswith(f"Próximo passo de «{x.hipotese}»"))
        assert d.refs == origem.refs  # a sugestão herda a fonte da hipótese
        assert not [c for c in d.checagens if c.tipo == "exame_repetido"]
    assert len([d for d in ins.pedidos if d.fonte_pedido != "fala"]) <= orders.MAX_SUGGESTIONS


# ---------- segurança: as checagens ----------
def test_checagens_cobrem_tfg_interacao_duplicidade_cruzada_e_alto_alerta(record):
    # TFG ~40 no cen6: metformina maior que o teto e AINE são alertas
    metformina = spoken(record, "cen6", "Vou aumentar a metformina para 1000 mg de 12 em 12 horas.")[0]
    assert {c.tipo for c in metformina.checagens} == {"dose_tfg"}
    aine = spoken(record, "cen6", "Vou prescrever ibuprofeno 600 mg de 8 em 8 horas.")[0]
    tipos = {c.tipo: c for c in aine.checagens}
    assert tipos["dose_tfg"].severidade == "critico" and tipos["interacao"].severidade == "atencao"
    assert aine.gravidade == "critico"
    # duplicidade: a losartana já está em uso (início ≠ ajuste), e outro BRA seria duplicidade terapêutica
    assert "duplicidade" in {c.tipo for c in spoken(record, "cen6", "Vou começar losartana 50 mg.")[0].checagens}
    valsartana = spoken(record, "cen6", "Vamos iniciar valsartana 80 mg.")[0]
    assert any(c.tipo == "duplicidade" and "Losartana" in c.texto for c in valsartana.checagens)
    # reatividade cruzada: cefalosporina em alérgica à penicilina é atenção, não bloqueio
    cef = spoken(record, "cen6", "Vou prescrever ceftriaxona 1 g.")[0]
    assert [(c.tipo, c.severidade) for c in cef.checagens if c.tipo == "reatividade_cruzada"] == [
        ("reatividade_cruzada", "atencao")
    ]
    # alto alerta (MPP): varfarina pede dupla checagem; suspender betabloqueador/anticoagulante avisa retirada abrupta
    assert "alto_alerta" in {c.tipo for c in spoken(record, "cen6", "Vou começar varfarina 5 mg.")[0].checagens}
    # alergia registrada por princípio ativo (cen5, dipirona): crítica, com a fonte do prontuário
    dip = spoken(record, "cen5", "Vou prescrever dipirona 1 g.")[0]
    assert [(c.tipo, c.severidade) for c in dip.checagens if c.tipo == "alergia"] == [("alergia", "critico")]


def test_medicamento_relatado_pelo_paciente_entra_nas_interacoes_e_alergia_relatada_na_checagem(record):
    falas = [
        ("paciente", "Eu tomo ibuprofeno quase todo dia por causa do joelho."),
        ("paciente", "Tenho alergia a sulfa."),
        ("medico", "Vou começar sulfametoxazol com trimetoprima 800 mg de 12 em 12 horas por 5 dias."),
        ("medico", "E aumentar a losartana para 100."),
    ]
    drafts = {d.chave: d for d in engine_for(record, "cen6", falas).run().pedidos}
    sulfa = drafts["sulfametoxazol_trimetoprima"]
    assert any(c.tipo == "alergia" and c.refs == ["Fala/2"] for c in sulfa.checagens)  # alergia só dita na consulta
    losartana = drafts["losartana"]
    assert any(c.tipo == "interacao" and "Ibuprofeno" in c.texto and c.refs == ["Fala/1"] for c in losartana.checagens)


def test_suspender_classe_critica_avisa_retirada_abrupta(record):
    d = spoken(record, "cen5", "Vamos suspender o diazepam.")[0]
    assert d.acao == "suspender" and d.prescricao_ref.startswith("MedicationRequest/")
    assert [c.tipo for c in d.checagens] == ["retirada_abrupta"]


# ---------- 12+ frases adversariais ----------
NAO_GERAM = [
    ("medico", "Não vou pedir hemograma hoje."),
    ("medico", "Não precisa pedir exame nenhum por enquanto."),
    ("medico", "Se piorar a gente pede o TSH."),
    ("medico", "Caso tenha febre, vou pedir hemograma."),
    ("medico", "A senhora já fez hemograma?"),
    ("medico", "Vamos avaliar a metformina depois."),
    ("medico", "O hemograma de ontem estava normal."),
    ("medico", "Continue tomando a losartana como está."),
    ("medico", "Mantenho a metformina na mesma dose."),
    ("paciente", "Doutora, a senhora vai pedir hemograma?"),
    ("paciente", "Vou começar amoxicilina que eu tinha em casa."),
    ("paciente", "Eu tomo ibuprofeno e a minha filha pediu um exame de sangue."),
    ("acompanhante", "A minha mãe precisa aumentar a losartana?"),
    ("medico", "Vou examinar a senhora agora, pode deitar."),
    ("medico", "Sem necessidade de pedir tomografia."),
    ("medico", "O sódio e o potássio estavam normais no último exame."),
    ("medico", "Vamos conversar sobre a dieta."),
]


@pytest.mark.parametrize(("who", "texto"), NAO_GERAM)
def test_frases_que_nao_sao_pedido_nao_geram_rascunho(record, who, texto):
    assert spoken(record, "cen6", texto, who) == [], texto


def test_frases_variadas_geram_o_pedido_certo(record):
    caso = {
        "Vou pedir um hemograma completo, uma creatinina e TSH.": {"Hemograma completo", "Creatinina", "TSH"},
        "vamos pedir ecg pra ver o coracao": {"Eletrocardiograma"},  # sem acento e sem pontuação
        "Pede um raio x de tórax e uma ultrassonografia de abdome.": set(),  # imperativo "pede" não é intenção
        "Vou solicitar um raio X de tórax.": {"Radiografia de tórax"},
        "VAMOS COLHER UM PERFIL LIPÍDICO": {"Perfil lipídico"},
        "Vou pedir o exame de urina e urocultura": {"Urina tipo 1 (EAS)", "Urocultura"},
        "Preciso repetir a hemoglobina glicada.": {"Hemoglobina glicada (HbA1c)"},
    }
    for texto, esperado in caso.items():
        assert {d.item for d in spoken(record, "cen6", texto)} == esperado, texto


def test_posologia_por_regex_e_continuacao_em_outra_oracao():
    p = orders.parse_posology("500 mg de 8 em 8 horas por 7 dias")
    assert (p.dose, p.frequencia, p.duracao, p.duracao_dias) == ("500 mg", "de 8 em 8 horas", "7 dias", 7)
    assert orders.parse_posology("1 comprimido 2x ao dia por duas semanas").duracao_dias == 14
    assert orders.parse_posology("0,5 mg a noite por uso continuo").dose == "0,5 mg"
    assert orders.parse_posology("20 gotas a cada 6 horas se dor").frequencia == "a cada 6 horas · se necessário (SOS)"
    assert orders.parse_posology("10 mg por via oral uma vez ao dia").via == "via oral"
    assert orders.parse_posology("500 mg para 7 dias").dose == "500 mg"  # "para 7 dias" não é dose-alvo
    assert orders.parse_posology("para 100 mg").dose == "100 mg" and orders.parse_posology("para 100 mg").new_dose


def test_posologia_dita_em_oracoes_separadas_e_troca_de_medicamento(record):
    d = spoken(record, "cen6", "Vou prescrever amoxicilina, 500 mg, de 8 em 8 horas, por 7 dias, para a infecção.")[0]
    assert (d.dose, d.frequencia, d.duracao, d.justificativa) == (
        "500 mg",
        "de 8 em 8 horas",
        "7 dias",
        "Para a infecção",
    )
    troca = {
        x.chave: x for x in spoken(record, "cen6", "Vamos trocar a glibenclamida pela gliclazida 30 mg pela manhã.")
    }
    assert troca["glibenclamida"].acao == "suspender" and troca["gliclazida"].acao == "iniciar"
    assert troca["gliclazida"].dose == "30 mg" and troca["glibenclamida"].prescricao_ref.startswith(
        "MedicationRequest/"
    )
    # o mesmo item repetido mais adiante vale o valor mais recente, com as duas falas como fonte
    falas = [("medico", "Vou pedir hemograma."), ("medico", "Aliás, o hemograma vamos pedir hoje mesmo.")]
    merged = [d for d in engine_for(record, "cen6", falas).run().pedidos if d.chave == "hemograma"]
    assert len(merged) == 1 and merged[0].refs == ["Fala/1", "Fala/2"]


def test_fala_do_paciente_corrigida_pela_ia_para_medico_passa_a_valer(record):
    """A IA corrige o falante de falas atribuídas por heurística: o rascunho segue a atribuição final."""
    eng = engine_for(record, "cen6", [("paciente", "Vamos pedir creatinina.")])
    assert [d for d in eng.run().pedidos if d.fonte_pedido == "fala"] == []
    eng = engine_for(record, "cen6", [("medico", "Vamos pedir creatinina.")])
    assert [d.chave for d in eng.run().pedidos if d.fonte_pedido == "fala"] == ["creatinina"]


# ---------- exames repetidos, pendentes e idade ----------
def test_exame_pendente_e_idade_em_idoso(record):
    rec = record("cen6")
    # duas leituras do mesmo exame: a mais recente decide a repetição
    eng = engine_for(record, "cen6", [("medico", "Vou pedir potássio.")])
    d = next(x for x in eng.run().pedidos if x.chave == "potassio")
    assert any(c.tipo == "exame_repetido" and "Observation/" in c.refs[0] for c in d.checagens)
    # exame fora da janela de repetição não avisa: avança o relógio 40 dias
    old_now = rec.now
    eng2 = engine_for(record, "cen6", [("medico", "Vou pedir creatinina.")])
    eng2.chart.now = old_now + timedelta(days=40)
    d2 = next(x for x in eng2.run().pedidos if x.chave == "creatinina")
    assert not [c for c in d2.checagens if c.tipo == "exame_repetido"]
    # idoso (cen5, 78 anos): benzodiazepínico novo cai na regra de Beers
    benzo = spoken(record, "cen5", "Vou prescrever clonazepam 0,5 mg à noite.")[0]
    assert any(c.tipo == "dose_idade" for c in benzo.checagens)


# ---------- a IA só completa ----------
def _live(base: Insights, pedidos_ia: list) -> consult_ai.ConsultLiveOutput:
    payload = consult_ai.mock_live(base) | {"anamnese": base.anamnese.model_dump(), "pedidos_ia": pedidos_ia}
    return consult_ai.ConsultLiveOutput.model_validate(payload)


def test_ia_completa_so_campos_vazios_e_nunca_cria_nem_remove(record):
    eng = engine_for(
        record,
        "cen6",
        [("medico", "Vou pedir TSH."), ("medico", "Vou começar amoxicilina 500 mg de 8 em 8 horas.")],
    )
    base = eng.run()
    tsh = next(d for d in base.pedidos if d.chave == "tsh")
    amox = next(d for d in base.pedidos if d.chave == "amoxicilina")
    valid = {"Fala/1", "Fala/2", "Condition/cen6-dx-0"}
    out = _live(
        base,
        [
            {"id": tsh.id, "justificativa": "Fraqueza e cansaço: rastrear hipotireoidismo.", "refs": ["Fala/1"]},
            {"id": amox.id, "dose": "1 g", "duracao": "7 dias", "justificativa": "Infecção.", "refs": ["Fala/2"]},
            {"id": "exm-9-inexistente-solicitar", "justificativa": "inventado", "refs": ["Fala/1"]},
            {"id": tsh.id, "justificativa": "sem fonte", "refs": ["Fala/99"]},
        ],
    )
    merged = consult_ai.merge_live(base, out, valid | {r for d in base.pedidos for r in d.refs}, eng.segments)
    m_tsh = next(d for d in merged.pedidos if d.chave == "tsh")
    m_amox = next(d for d in merged.pedidos if d.chave == "amoxicilina")
    assert m_tsh.justificativa.startswith("Fraqueza") and m_tsh.origem == "ia"
    assert m_amox.dose == "500 mg" and m_amox.duracao == "7 dias"  # o que o médico disse prevalece sobre a IA
    assert [d.id for d in merged.pedidos] == [d.id for d in base.pedidos]  # não cria nem remove rascunho
    assert [c.model_dump() for c in m_amox.checagens] == [c.model_dump() for c in amox.checagens]
    assert any(x.get("onde") == "pedidos" for x in merged.descartados)  # ref inválida e id inexistente descartados


# ---------- catálogo e prompts ----------
def test_catalogo_de_exames_tem_loinc_fonte_e_preparo():
    path = knowledge.get_settings().data_dir / "reference" / "lab_catalog.csv"
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) >= 20
    keys = set()
    for r in rows:
        assert re.fullmatch(r"\d{1,5}-\d", r["loinc"]), r["chave"]
        assert r["fonte"].strip() and r["preparo"].strip() and r["sinonimos"].strip()
        assert r["categoria"] in ("laboratorio", "imagem")
        assert all(w == w.lower() for w in r["sinonimos"].split("|")), r["chave"]
        assert r["chave"] not in keys
        keys.add(r["chave"])
        for k in filter(None, r["chaves_resultado"].split("|")):
            from app.fhir_client import codes

            assert k in codes.LABS, (r["chave"], k)
    # o catálogo cobre os exames que os protocolos de APS cobram em laboratório
    result_keys = {k for lab in knowledge.lab_catalog() for k in lab.result_keys}
    protocol_labs = {p.source.split(":", 1)[1] for p in knowledge.protocols() if p.source.startswith("lab:")}
    assert protocol_labs <= result_keys


def test_prompt_clinico_foi_atualizado_para_os_pedidos():
    text = (knowledge.get_settings().data_dir.parent / "apps/api/app/llm/prompts/consult_live_clinica.md").read_text(
        encoding="utf-8"
    )
    assert "version: 1.4.0" in text and "pedidos_ia" in text and "pedidos_em_rascunho" in text


def test_medicamentos_do_prontuario_mostram_a_prescricao_proposta(client, auth, monkeypatch, store):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("medico", "Vou começar dipirona 1 g de 6 em 6 horas."))
    assert decide(client, h, cid, by_item(ins, "Dipirona")["id"], estado="assinado").status_code == 200
    client.post(f"/consultations/{cid}/finish", headers=h)
    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    before = set(store.resources["MedicationRequest"])
    client.post(f"/consultations/{cid}/send", headers=h)
    try:
        consult_service.clear_chart_cache()
        again = start(client, h)
        proposto = [m for m in again["insights"]["medicamentos"] if m["status"] == "proposto"]
        assert [m["medicamento"] for m in proposto] == ["Dipirona"] and proposto[0]["refs"][0].startswith("Medication")
        assert all(m["medicamento"] != "Dipirona" for m in again["insights"]["medicamentos"] if m["status"] == "em_uso")
    finally:
        for rid in set(store.resources["MedicationRequest"]) - before:
            store.resources["MedicationRequest"].pop(rid, None)
