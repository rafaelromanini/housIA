"""Consulta com IA: transcrição em tempo real, Smart Apps, nota, envio ao prontuário e centro cirúrgico."""

import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.consult import ai as consult_ai
from app.consult import service as consult_service
from app.consult.chart import egfr_ckd_epi_2021
from app.consult.knowledge import demo_scripts
from app.consult.text import keyword_status, mentioned_drugs
from app.db import session as db_session
from app.db.models import AuditLog
from app.llm import client as llm_client

SCRIPT = demo_scripts()["cen6_consulta"]["falas"]


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def start(client, h, pid="cen6", **body):
    res = client.post(f"/patients/{pid}/consultations", json={"tipo": "consulta"} | body, headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def say(client, h, cid, falas):
    payload = {
        "segmentos": [{"falante": f["falante"], "texto": f["texto"], "t": f["t"], "origem": "demo"} for f in falas]
    }
    res = client.post(f"/consultations/{cid}/segments", json=payload, headers=h)
    assert res.status_code == 200, res.text
    return res.json()["insights"]


def approve(client, h, cid):
    """Revisa e atesta a nota como ela está: o servidor só exporta e envia nota revisada e atestada (#14)."""
    got = client.get(f"/consultations/{cid}", headers=h).json()
    if got["status"] == "enviada":  # já no prontuário: nada a revisar
        return got
    res = client.put(f"/consultations/{cid}/note", json=got["nota"] | {"atestacao": True}, headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def accept_critical(client, h, cid, ins):
    """O profissional aceita os alertas críticos de medicamento na consulta (senão o envio pede justificativa)."""
    for m in ins["sugestoes_medicamentos"]:
        if m["severidade"] == "critico":
            res = client.post(
                f"/consultations/{cid}/insight-actions", json={"item_key": m["item_key"], "acao": "aceitar"}, headers=h
            )
            assert res.status_code == 200, res.text


def all_refs(obj) -> list[str]:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "refs":
                found += v
            else:
                found += all_refs(v)
    elif isinstance(obj, list):
        for v in obj:
            found += all_refs(v)
    return found


# ---------- utilidades determinísticas ----------
def test_egfr_ckd_epi_2021():
    assert egfr_ckd_epi_2021(1.5, 60, female=True) == pytest.approx(39.6, abs=0.2)
    assert egfr_ckd_epi_2021(0.9, 40, female=False) > 90


def test_negation_and_drug_mentions():
    assert keyword_status("Não, dor no peito eu não tenho.", ("dor no peito",)) == "negado"
    assert keyword_status("Falta de ar também não.", ("falta de ar",)) == "negado"
    assert keyword_status("Não fiz xixi desde que tiraram a sonda", ("nao fiz xixi",)) == "presente"
    assert ("ibuprofeno", "Ibuprofeno", "em_uso") in mentioned_drugs("Comprei um ibuprofeno na farmácia")
    assert ("@aine", "anti-inflamatório", "em_uso") in mentioned_drugs("tomei um anti-inflamatório")


# ---------- fluxo completo (cenário 6) ----------
def test_pre_consult_questions_come_from_the_chart(client, auth):
    c = start(client, auth("medico"))
    ins = c["insights"]
    assert c["roteiro_demo"] == "cen6_consulta"
    assert ins["falas_analisadas"] == 0
    perguntas = " ".join(q["pergunta"] for q in ins["perguntas"])
    assert "Glibenclamida" in perguntas  # sulfonilureia em uso → pergunta sobre refeições
    assert ins["tfg"] == pytest.approx(39.6, abs=0.2)
    assert any(p["status"] == "sem_registro" and "pés" in p["item"] for p in ins["protocolos"])


def test_live_smart_apps_update_as_the_conversation_goes(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]

    early = say(client, h, cid, SCRIPT[:7])  # tontura, quase queda, suor frio
    tags = {t["rotulo"]: t["status"] for t in early["queixas"]}
    assert tags["Tontura"] == "presente" and tags["Suor frio e tremor"] == "presente"
    early_q = [q["pergunta"] for q in early["perguntas"]]
    assert any("falta de ar" in q for q in early_q)  # cansaço → falta de ar/inchaço/palpitação?

    late = say(client, h, cid, SCRIPT[7:])
    late_q = [q["pergunta"] for q in late["perguntas"]]
    covered = [q["pergunta"] for q in late["perguntas_abordadas"]]
    # a médica perguntou de falta de ar na fala 21: a sugestão sai da lista e vai para "já abordadas"
    assert not any("falta de ar" in q for q in late_q)
    assert any("falta de ar" in q for q in covered)
    assert {t["rotulo"]: t["status"] for t in late["queixas"]}["Dor no peito"] == "negado"

    cids = {h_["cid"] for h_ in late["hipoteses"]}
    assert {"E16.0", "N17.9", "I95.1", "E87.1"} <= cids
    hypo = next(h_ for h_ in late["hipoteses"] if h_["cid"] == "E16.0")
    assert hypo["confianca"] == "alta" and any(r.startswith("Fala/") for r in hypo["refs"])

    safety = " ".join(m["texto"] for m in late["sugestoes_medicamentos"])
    assert "Tríplice ameaça" in safety and "Metformina" in safety and "Glibenclamida" in safety
    assert any(a["alergeno"].startswith("Camarão") and a["fonte"] == "relatado" for a in late["alergias"])
    assert any(m["medicamento"] == "Ibuprofeno" and m["status"] == "relatado" for m in late["medicamentos"])
    emotions = {(e["emocao"], e["quem"]) for e in late["emocoes"]}
    assert ("ansiedade", "paciente") in emotions and ("alivio", "paciente") in emotions
    assert late["comunicacao"]["falas"] == len(SCRIPT)
    assert any(e["chave"] == "sodio" and e["status"] == "baixo" for e in late["exames"])

    # toda fonte citada existe: falas da conversa ou recursos do prontuário
    for ref in all_refs(late):
        if ref.startswith("Fala/"):
            assert 1 <= int(ref.split("/")[1]) <= len(SCRIPT)
        else:
            assert re.match(r"^[A-Z][A-Za-z]+/[\w.-]+$", ref), ref


def test_speaker_is_inferred_when_capture_does_not_say(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    res = client.post(
        f"/consultations/{cid}/segments",
        json={"segmentos": [{"texto": "A senhora tem sentido falta de ar?"}, {"texto": "Não, doutora."}]},
        headers=h,
    ).json()
    assert [(s["falante"], s["falante_auto"]) for s in res["novos"]] == [("medico", True), ("paciente", True)]


def test_finish_review_and_send_to_ehr(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    accept_critical(client, h, cid, say(client, h, cid, SCRIPT))
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200, done.text
    note = done.json()["nota"]
    assert done.json()["status"] == "finalizada"
    assert note["queixa_principal"]["texto"] and note["avaliacao"]["refs"]
    assert any(c["codigo"] == "E16.0" for c in note["cids"])
    assert any("camarão" in p["texto"].lower() for p in note["pendencias"])
    assert note["cronologia"] and note["orientacoes_paciente"]

    assert (
        client.post(f"/consultations/{cid}/segments", json={"segmentos": [{"texto": "oi"}]}, headers=h).status_code
        == 409
    )

    bad = client.put(f"/consultations/{cid}/note", json={"queixa_principal": "x"}, headers=h)
    assert bad.status_code == 422
    note["plano"]["texto"] = "Suspender ibuprofeno e glibenclamida; retorno em 2 semanas."
    reviewed = client.put(f"/consultations/{cid}/note", json=note, headers=h).json()
    assert reviewed["nota_meta"]["revisada_por"] and reviewed["nota_meta"]["atestacao"] is False
    assert client.post(f"/consultations/{cid}/send", headers=h).status_code == 409  # revisou, mas não atestou
    reviewed = client.put(f"/consultations/{cid}/note", json=note | {"atestacao": True}, headers=h).json()
    assert reviewed["nota_meta"]["atestacao"] is True

    export = client.post(f"/consultations/{cid}/send", headers=h).json()
    assert export["enviado"] is False and export["recurso"]["resourceType"] == "DocumentReference"
    assert "Suspender ibuprofeno" in export["texto"]

    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    sent = client.post(f"/consultations/{cid}/send", headers=h).json()
    assert sent["enviado"] is True and sent["fhir_ref"].startswith("DocumentReference/")
    timeline = client.get("/patients/cen6/timeline?kinds=note", headers=h).json()
    assert any(e["source_ref"] == sent["fhir_ref"] for e in timeline)
    assert client.post(f"/consultations/{cid}/send", headers=h).status_code == 409


def test_audit_never_stores_the_transcript(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    client.post(f"/consultations/{cid}/insights", headers=h)
    client.post(f"/consultations/{cid}/finish", headers=h)
    with db_session.SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action.like("consulta.%"))).all()
    actions = {r.action for r in rows}
    assert {"consulta.iniciada", "consulta.analise_ia", "consulta.finalizada"} <= actions
    dump = json.dumps([r.payload_json for r in rows], ensure_ascii=False)
    assert "ibuprofeno" not in dump.lower() and "tonta" not in dump.lower()


def test_gestor_cannot_open_consultations(client, auth):
    assert client.post("/patients/cen6/consultations", json={}, headers=auth("gestor")).status_code == 403


def test_appointments_list_today_with_consultation_status(client, auth):
    h = auth("medico")
    agenda = client.get("/appointments", headers=h).json()
    joana = next(a for a in agenda if a["patient_id"] == "cen6")
    assert joana["status_fhir"] == "arrived" and joana["consulta_id"] is None
    cid = start(client, h)["id"]
    joana = next(a for a in client.get("/appointments", headers=h).json() if a["patient_id"] == "cen6")
    assert joana["consulta_id"] == cid and joana["consulta_status"] == "em_andamento"


# ---------- IA real (cliente falso): pseudonimização e validação de fontes ----------
class FakeClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(self.payload))]
        )


def test_ai_cycle_is_pseudonymized_and_drops_items_without_valid_sources(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    empty_anamnese = {
        "queixa_principal": "Tontura postural",
        "hda": "Há 3 semanas.",
        "antecedentes": [],
        "medicacoes_em_uso": [],
        "alergias": [],
        "habitos_e_contexto": [],
        "revisao_de_sistemas": [],
        "refs": ["Fala/4"],
    }
    payload = {
        "resumo": "Tontura postural e hipoglicemia em uso de glibenclamida.",
        "destaques": [],
        "queixas": [],
        "emocoes": [],
        "perguntas": [
            {
                "pergunta": "Quantas vezes por semana tem tido o suor frio?",
                "motivo": "frequência",
                "prioridade": "alta",
                "refs": ["Fala/7"],
            },
            {"pergunta": "Pergunta inventada", "motivo": "x", "prioridade": "alta", "refs": ["Fala/999"]},
        ],
        "hipoteses": [],
        "sugestoes_medicamentos": [],
        "cirurgia": [],
        "historico_relatado": [],
        "anamnese": empty_anamnese,
        "falantes": [],
    }
    fake = FakeClient(payload)
    monkeypatch.setattr(llm_client.get_settings(), "llm_mode", "real")
    monkeypatch.setattr(llm_client, "_client", fake)
    res = client.post(f"/consultations/{cid}/insights", headers=h)
    assert res.status_code == 200, res.text
    body = res.json()
    sent = json.dumps(fake.calls[0], ensure_ascii=False, default=str)
    assert "Joana" not in sent and "Fernanda" not in sent and "[PACIENTE]" in sent
    assert fake.calls[0]["output_config"]["effort"] == "low"
    perguntas = body["insights"]["perguntas"]
    assert any(q["origem"] == "ia" and "suor frio" in q["pergunta"] for q in perguntas)
    assert not any(q["pergunta"] == "Pergunta inventada" for q in perguntas)
    assert body["ia"]["descartados"] and body["insights"]["modo"] == "ia"
    # a próxima fala re-mescla a última análise da IA com as regras atualizadas
    again = say(client, h, cid, [{"falante": "paciente", "texto": "Também sinto sede o tempo todo.", "t": 200}])
    assert any(q["origem"] == "ia" for q in again["perguntas"])
    monkeypatch.setattr(llm_client, "_client", None)


def test_translation_for_foreign_language_patient(client, auth):
    h = auth("medico")
    cid = start(client, h, idioma_paciente="es")["id"]
    say(
        client,
        h,
        cid,
        [
            {"falante": "paciente", "texto": "Me duele la cabeza desde ayer.", "t": 1},
            {"falante": "medico", "texto": "Desde quando a dor começou?", "t": 4},
        ],
    )
    out = client.post(f"/consultations/{cid}/translate", json={}, headers=h).json()
    assert set(out["traducoes"]) == {"1", "2"}
    seg = client.get(f"/consultations/{cid}", headers=h).json()["segmentos"]
    assert seg[0]["idioma"] == "es" and seg[0]["traducao"]


def test_server_side_transcription_is_pluggable(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    res = client.post(
        f"/consultations/{cid}/audio?falante=paciente", content=b"\x00\x01", headers=h | {"Content-Type": "audio/webm"}
    )
    assert res.status_code == 503  # sem STT_URL configurado

    from app.consult import stt

    settings = consult_service.get_settings()
    monkeypatch.setattr(settings, "stt_url", "http://stt.local/v1/audio/transcriptions")
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs | {"url": url})
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"text": "Estou tonta quando levanto."})

    monkeypatch.setattr(stt.httpx, "post", fake_post)
    res = client.post(
        f"/consultations/{cid}/audio?falante=paciente", content=b"\x00\x01", headers=h | {"Content-Type": "audio/webm"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["novos"][0]["origem"] == "servidor"
    assert captured["data"]["language"] == "pt" and captured["files"]["file"][2] == "audio/webm"


# ---------- centro cirúrgico ----------
def test_surgical_dictation_report_opme_and_billing_checks(client, auth):
    h = auth("medico")
    c = start(client, h, pid="cen4", tipo="cirurgia", especialidade="cirurgia_geral")
    assert c["roteiro_demo"] == "cen4_cirurgia"
    ins = say(client, h, c["id"], demo_scripts()["cen4_cirurgia"]["falas"])
    rep = ins["relatorio_cirurgico"]
    fields = {f["campo"]: f for f in rep["campos"]}
    assert "apendicite" in fields["diagnostico_pre"]["texto"].lower()
    assert "cefazolina" in fields["antibioticoprofilaxia"]["texto"].lower()
    assert fields["horario"]["texto"] == "" and fields["equipe"]["texto"] == ""
    opme = {o["codigo"]: o for o in rep["opme"]}
    assert opme["DEMO-OPME-002"]["citado"] == 2 and opme["DEMO-OPME-002"]["status"] == "conferido"
    assert opme["DEMO-OPME-009"]["status"] == "nao_autorizado"
    assert any(p["severidade"] == "critico" and "glosa" in p["texto"] for p in rep["pendencias"])
    assert rep["cids"][0]["codigo"] == "K35.8"
    done = client.post(f"/consultations/{c['id']}/finish", headers=h).json()
    assert done["nota"]["relatorio_cirurgico"]["campos"]


def test_specialty_profile_changes_suggested_questions(client, auth):
    h = auth("medico")
    uro = start(client, h, especialidade="urologia")["insights"]["perguntas"]
    assert any("jato" in q["pergunta"] for q in uro)
    clin = start(client, h)["insights"]["perguntas"]
    assert not any("jato" in q["pergunta"] for q in clin)
    assert (
        client.post("/patients/cen6/consultations", json={"especialidade": "astrologia"}, headers=h).status_code == 422
    )


def test_merge_never_removes_rule_based_safety_items():
    from app.consult.schemas import Insights, MedSuggestion

    base = Insights(
        tipo="consulta",
        especialidade="clinica_medica",
        falas_analisadas=1,
        modo="regras",
        sugestoes_medicamentos=[
            MedSuggestion(texto="Suspender ibuprofeno", tipo="suspender", severidade="critico", refs=["Fala/1"])
        ],
    )
    out = consult_ai.ConsultLiveOutput.model_validate(consult_ai.mock_live(base))
    merged = consult_ai.merge_live(base, out, {"Fala/1"}, [])
    assert merged.sugestoes_medicamentos[0].texto == "Suspender ibuprofeno"


def test_export_reidentifies_tokens_sent_to_the_llm(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT[:6])
    note = client.post(f"/consultations/{cid}/finish", headers=h).json()["nota"]
    note["hda"]["texto"] = "[PACIENTE] veio com a filha ([FAMILIAR]) relatando tontura."
    client.put(f"/consultations/{cid}/note", json=note | {"atestacao": True}, headers=h)
    # pendência crítica aberta: copiar/baixar só depois do envio justificado
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 409
    sent = client.post(
        f"/consultations/{cid}/send", json={"justificativa": "Risco discutido com a paciente"}, headers=h
    )
    assert sent.status_code == 200, sent.text
    text = client.get(f"/consultations/{cid}/export", headers=h).json()["texto"]
    assert "Joana Ferreira Lima veio com a filha (Fernanda Lima Duarte)" in text and "[PACIENTE]" not in text
