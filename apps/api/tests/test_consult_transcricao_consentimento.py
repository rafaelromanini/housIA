"""#2 transcrição editável + #13 consentimento registrado, recusa sem IA e marcos de privacidade."""

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.consult import service as consult_service
from app.db import session as db_session
from app.db.models import AuditLog
from app.llm import client as llm_client
from tests.conftest import CONSENT
from tests.test_consult import SCRIPT, approve, start


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


@pytest.fixture
def writeback(monkeypatch, store):
    """EHR_WRITEBACK ligado; os Consent criados no FHIR local saem ao fim (a loja é da sessão inteira)."""
    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    before = set(store.resources["Consent"])
    yield store
    for rid in set(store.resources["Consent"]) - before:
        store.resources["Consent"].pop(rid)


def audit(action: str, cid: int) -> list[dict]:
    with db_session.SessionLocal() as db:
        rows = db.scalars(
            select(AuditLog).where(AuditLog.action == action, AuditLog.entity_id == str(cid)).order_by(AuditLog.id)
        ).all()
        return [r.payload_json for r in rows]


def post_segments(client, h, cid, falas):
    return client.post(
        f"/consultations/{cid}/segments",
        json={"segmentos": [{"falante": f, "texto": t, "origem": "digitado"} for f, t in falas]},
        headers=h,
    )


def tags(consulta: dict) -> dict[str, str]:
    return {t["rotulo"]: t["status"] for t in consulta["insights"]["queixas"]}


# ---------- #2: edição da transcrição ----------
def test_trocar_o_falante_de_medico_para_paciente_faz_aparecer_a_tag_tontura(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = post_segments(client, h, cid, [("medico", "Tenho tontura"), ("paciente", "Bom dia, doutora")]).json()
    assert "Tontura" not in {t["rotulo"] for t in ins["insights"]["queixas"]}  # dita pelo profissional, não é queixa

    res = client.patch(f"/consultations/{cid}/segments/1", json={"falante": "paciente"}, headers=h)
    assert res.status_code == 200, res.text
    seg = res.json()["segmentos"][0]
    assert seg["falante"] == "paciente" and seg["falante_auto"] is False
    assert seg["falante_original"] == "medico" and seg["editado_por"] and seg["editado_em"]
    tontura = next(t for t in res.json()["insights"]["queixas"] if t["rotulo"] == "Tontura")
    assert tontura["status"] == "presente" and "Fala/1" in tontura["refs"]
    # persiste
    assert tags(client.get(f"/consultations/{cid}", headers=h).json())["Tontura"] == "presente"


def test_editar_o_texto_reroda_as_regras_e_guarda_o_original(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [("paciente", "Estou com tontura desde ontem")])
    res = client.patch(f"/consultations/{cid}/segments/1", json={"texto": "Estou com febre desde ontem"}, headers=h)
    assert res.status_code == 200
    seg = res.json()["segmentos"][0]
    assert seg["texto"] == "Estou com febre desde ontem" and seg["texto_original"] == "Estou com tontura desde ontem"
    t = tags(res.json())
    assert t.get("Febre") == "presente" and "Tontura" not in t
    # editar de novo não troca o original
    again = client.patch(f"/consultations/{cid}/segments/1", json={"texto": "Estou com tosse"}, headers=h).json()
    assert again["segmentos"][0]["texto_original"] == "Estou com tontura desde ontem"


def test_audit_da_edicao_nao_contem_o_texto_da_fala(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [("paciente", "Meu segredo clínico é uma tontura azul")])
    client.patch(
        f"/consultations/{cid}/segments/1",
        json={"texto": "Texto novo e sigiloso sobre vertigem rara", "falante": "acompanhante"},
        headers=h,
    )
    rows = audit("consulta.fala_editada", cid)
    assert len(rows) == 1 and rows[0]["n"] == 1 and sorted(rows[0]["campos"]) == ["falante", "texto"]
    with db_session.SessionLocal() as db:
        every = " ".join(
            str(r.payload_json) for r in db.scalars(select(AuditLog).where(AuditLog.entity_id == str(cid)))
        )
    assert "segredo" not in every and "sigiloso" not in every and "vertigem" not in every


def test_edicao_sem_mudanca_ou_invalida(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [("paciente", "Estou com tontura")])
    assert client.patch(f"/consultations/{cid}/segments/1", json={}, headers=h).status_code == 422
    assert client.patch(f"/consultations/{cid}/segments/1", json={"texto": "   "}, headers=h).status_code == 422
    assert client.patch(f"/consultations/{cid}/segments/1", json={"falante": "robo"}, headers=h).status_code == 422
    assert client.patch(f"/consultations/{cid}/segments/9", json={"texto": "x"}, headers=h).status_code == 404
    same = client.patch(f"/consultations/{cid}/segments/1", json={"texto": "Estou com tontura"}, headers=h)
    assert same.status_code == 200 and same.json()["segmentos"][0]["texto_original"] is None
    assert audit("consulta.fala_editada", cid) == []  # nada mudou: nada auditado
    assert (
        client.patch(f"/consultations/{cid}/segments/1", json={"texto": "x"}, headers=auth("gestor")).status_code == 403
    )


def test_finish_com_2_falas_no_corpo_gera_a_nota_com_o_total_mais_2(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [(f["falante"], f["texto"]) for f in SCRIPT[:5]])
    extra = {
        "segmentos": [
            {"falante": "paciente", "texto": "Estou com muita sede também", "origem": "microfone"},
            {"falante": "medico", "texto": "Vamos pedir um exame de sangue", "origem": "microfone"},
        ]
    }
    res = client.post(f"/consultations/{cid}/finish", json=extra, headers=h)
    assert res.status_code == 200, res.text
    done = res.json()
    assert done["status"] == "finalizada" and done["nota"] and len(done["segmentos"]) == 5 + 2
    assert [s["n"] for s in done["segmentos"]] == list(range(1, 8))
    assert done["segmentos"][-1]["texto"] == "Vamos pedir um exame de sangue"
    assert done["cobertura"]["total"] >= 1
    finished = audit("consulta.finalizada", cid)[0]
    assert finished["falas"] == 7 and finished["falas_no_corpo"] == 2
    # a consulta já finalizada não aceita falas pelo finish (e nada é gravado)
    again = client.post(f"/consultations/{cid}/finish", json=extra, headers=h)
    assert again.status_code == 409
    assert len(client.get(f"/consultations/{cid}", headers=h).json()["segmentos"]) == 7


def test_finish_sem_corpo_continua_valendo(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [("paciente", "Estou com tontura")])
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200


def test_finish_com_falas_e_atomico_quando_a_nota_falha(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [("paciente", "Estou com tontura")])

    def boom(*_a, **_k):
        raise llm_client.LLMError("fora do ar")

    monkeypatch.setattr(consult_service.ai, "run_note", boom)
    res = client.post(
        f"/consultations/{cid}/finish",
        json={"segmentos": [{"falante": "paciente", "texto": "e dor de cabeça", "origem": "microfone"}]},
        headers=h,
    )
    assert res.status_code == 502
    after = client.get(f"/consultations/{cid}", headers=h).json()
    assert after["status"] == "em_andamento" and len(after["segmentos"]) == 1  # a fala do corpo não ficou


def test_patch_depois_de_finalizada_marca_a_nota_como_desatualizada_e_lista_as_secoes(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [(f["falante"], f["texto"]) for f in SCRIPT])
    done = client.post(f"/consultations/{cid}/finish", headers=h).json()
    assert done["nota_meta"].get("desatualizada") is None
    cited = next(
        (key, int(ref.split("/")[1]))
        for key, sec in done["nota"].items()
        if isinstance(sec, dict) and sec.get("refs")
        for ref in sec["refs"]
        if ref.startswith("Fala/")
    )
    key, n = cited
    res = client.patch(f"/consultations/{cid}/segments/{n}", json={"texto": "Texto corrigido da fala"}, headers=h)
    assert res.status_code == 200, res.text
    stale = res.json()["nota_meta"]["desatualizada"]
    assert n in stale["falas"] and stale["secoes"] and stale["em"]
    assert consult_service.NOTE_LABELS[key] in stale["secoes"]
    assert audit("consulta.fala_editada", cid)[0]["nota_desatualizada"] is True
    # a nota em si não foi regenerada; revisar e salvar reconcilia
    note = res.json()["nota"]
    saved = client.put(f"/consultations/{cid}/note", json=note, headers=h).json()
    assert saved["nota_meta"]["desatualizada"] is None


def test_nao_edita_depois_de_enviada(client, auth, monkeypatch):
    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [("paciente", "Estou com tontura")])
    client.post(f"/consultations/{cid}/finish", headers=h)
    approve(client, h, cid)
    sent = client.post(
        f"/consultations/{cid}/send", json={"justificativa": "Risco discutido com a paciente"}, headers=h
    )
    assert sent.status_code == 200, sent.text
    assert client.patch(f"/consultations/{cid}/segments/1", json={"texto": "x"}, headers=h).status_code == 409


# ---------- #13: consentimento ----------
def test_post_de_consulta_sem_consentimento_devolve_422(client, auth):
    h = auth("medico")
    assert client.post("/patients/cen6/consultations", json={"tipo": "consulta"}, headers=h).status_code == 422
    sem_finalidades = {"quem": "paciente", "recusou": False}
    res = client.post("/patients/cen6/consultations", json={"consentimento": sem_finalidades}, headers=h)
    assert res.status_code == 422
    bad_who = CONSENT | {"quem": "vizinho"}
    assert client.post("/patients/cen6/consultations", json={"consentimento": bad_who}, headers=h).status_code == 422


def test_consent_fhir_e_criado_com_as_finalidades_na_provision(client, auth, writeback):
    h = auth("medico")
    consent = CONSENT | {"finalidades": {"transcricao": True, "ia_externa": True, "traducao": False, "emocoes": False}}
    created = start(client, h, consentimento=consent)
    ref = created["consentimento"]["fhir_ref"]
    assert ref and ref.startswith("Consent/")
    resource = writeback.get("Consent", ref.split("/")[1])
    assert resource["status"] == "active" and resource["patient"] == {"reference": "Patient/cen6"}
    assert resource["scope"]["coding"][0]["code"] == "patient-privacy"
    provisions = {p["purpose"][0]["code"]: p["type"] for p in resource["provision"]["provision"]}
    assert provisions == {"transcricao": "permit", "ia_externa": "permit", "traducao": "deny", "emocoes": "deny"}
    row = audit("consulta.consentimento", created["id"])[0]
    assert row["fhir_ref"] == ref and row["finalidades"]["traducao"] is False and row["recusou"] is False

    # aparece na linha do tempo do paciente
    timeline = client.get("/patients/cen6/timeline", params={"kinds": "consent"}, headers=h).json()
    assert any(e["source_ref"] == ref and "transcrição" in e["summary"].lower() for e in timeline)


def test_sem_writeback_o_consent_fica_so_na_consulta(client, auth, store):
    h = auth("medico")
    before = set(store.resources["Consent"])
    created = start(client, h)
    assert created["consentimento"]["fhir_ref"] is None and set(store.resources["Consent"]) == before
    with db_session.SessionLocal() as db:
        c = db.get(consult_service.Consultation, created["id"])
        assert c.extra_json["consentimento"]["consent"]["resourceType"] == "Consent"


def test_consent_de_acompanhante_aponta_o_related_person(client, auth, writeback):
    h = auth("medico")
    cons = CONSENT | {"quem": "acompanhante", "related_person_id": "cen6-fam-0"}
    created = start(client, h, consentimento=cons)
    assert created["consentimento"]["quem"] == "acompanhante" and created["consentimento"]["quem_nome"]
    resource = writeback.get("Consent", created["consentimento"]["fhir_ref"].split("/")[1])
    assert resource["performer"][0]["reference"] == "RelatedPerson/cen6-fam-0"
    unknown = CONSENT | {"quem": "responsavel", "related_person_id": "nao-existe"}
    res = client.post("/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": unknown}, headers=h)
    assert res.status_code == 422


def test_recusa_bloqueia_insights_e_traducao_e_a_nota_sai_em_modo_regras(client, auth, monkeypatch):
    h = auth("medico")
    created = start(client, h, consentimento=CONSENT | {"recusou": True})
    cid = created["id"]
    assert created["consentimento"]["recusou"] is True and created["consentimento"]["permite"]["ia"] is False
    # recusa vale para todas as finalidades, mesmo que o corpo as traga marcadas
    assert not any(created["consentimento"]["finalidades"].values())
    assert post_segments(client, h, cid, [(f["falante"], f["texto"]) for f in SCRIPT[:8]]).status_code == 200  # regras
    assert client.post(f"/consultations/{cid}/insights", headers=h).status_code == 403
    assert client.post(f"/consultations/{cid}/translate", json={"ns": [1]}, headers=h).status_code == 403
    assert client.post(f"/consultations/{cid}/audio?falante=medico", content=b"abc", headers=h).status_code == 403

    # a nota não chama o LLM nem em LLM_MODE=real: só as regras
    monkeypatch.setattr(get_settings(), "llm_mode", "real")

    def never(*_a, **_k):
        raise AssertionError("o LLM não pode ser chamado com a recusa do paciente")

    monkeypatch.setattr(llm_client, "_call", never)
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200, done.text
    meta = done.json()["nota_meta"]
    assert meta["modo"] == "regras" and meta["sem_ia"] is True
    assert done.json()["nota"]["queixa_principal"]["texto"]
    assert audit("consulta.finalizada", cid)[0]["sem_ia"] is True


def test_recusa_no_copiloto_responde_so_por_regra(client, auth):
    h = auth("medico")
    cid = start(client, h, consentimento=CONSENT | {"recusou": True})["id"]
    post_segments(client, h, cid, [("paciente", "Estou com tontura")])
    res = client.post(f"/consultations/{cid}/ask", json={"pergunta": "Explique a fisiologia da tontura"}, headers=h)
    assert res.status_code == 200
    resp = res.json()["mensagem"]["resposta"]
    assert resp["intencao"] == "sem_resposta" and "não autorizou" in resp["aviso"]


def test_ia_externa_false_bloqueia_a_ia_mas_nao_a_traducao_das_regras_nem_a_transcricao(client, auth):
    h = auth("medico")
    fin = {"transcricao": True, "ia_externa": False, "traducao": True, "emocoes": True}
    created = start(client, h, consentimento=CONSENT | {"finalidades": fin})
    cid = created["id"]
    assert created["consentimento"]["permite"] == {
        "ia": False,
        "traducao": False,
        "transcricao_servidor": True,
        "emocoes": True,
    }
    assert client.post(f"/consultations/{cid}/insights", headers=h).status_code == 403
    assert client.post(f"/consultations/{cid}/translate", headers=h).status_code == 403
    assert post_segments(client, h, cid, [("paciente", "Estou com tontura")]).status_code == 200
    assert client.post(f"/consultations/{cid}/finish", headers=h).json()["nota_meta"]["modo"] == "regras"


def test_traducao_false_bloqueia_so_a_traducao(client, auth):
    h = auth("medico")
    fin = {"transcricao": True, "ia_externa": True, "traducao": False, "emocoes": True}
    cid = start(client, h, consentimento=CONSENT | {"finalidades": fin}, idioma_paciente="es")["id"]
    assert client.post(f"/consultations/{cid}/translate", headers=h).status_code == 403
    post_segments(client, h, cid, [("paciente", "Tengo mareos")])
    assert client.post(f"/consultations/{cid}/insights", headers=h).status_code == 200


def test_consentimento_dado_permite_insights_e_traducao(client, auth):
    h = auth("medico")
    cid = start(client, h, idioma_paciente="es")["id"]
    post_segments(client, h, cid, [("paciente", "Tengo mareos")])
    assert client.post(f"/consultations/{cid}/insights", headers=h).status_code == 200
    assert client.post(f"/consultations/{cid}/translate", headers=h).status_code == 200


def test_pausa_aparece_como_marco_na_transcricao_e_no_audit(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [("paciente", "Estou com tontura")])
    res = client.post(f"/consultations/{cid}/marker", json={"tipo": "pausa"}, headers=h)
    assert res.status_code == 200, res.text
    marco = res.json()["segmentos"][-1]
    assert marco["origem"] == "marco" and marco["marco"] == "pausa" and marco["n"] == 2
    assert res.json()["consentimento"]["estado"] == "pausado"
    assert audit("consulta.marco_privacidade", cid) == [{"patient_id": "cen6", "marco": "pausa", "n": 2}]

    # durante a pausa não entra fala; pausar de novo é erro
    assert post_segments(client, h, cid, [("paciente", "e febre")]).status_code == 409
    assert client.post(f"/consultations/{cid}/marker", json={"tipo": "pausa"}, headers=h).status_code == 409
    back = client.post(f"/consultations/{cid}/marker", json={"tipo": "retomada"}, headers=h)
    assert back.status_code == 200 and back.json()["consentimento"]["estado"] == "ativo"
    after = post_segments(client, h, cid, [("paciente", "e febre")])
    assert after.status_code == 200 and after.json()["novos"][0]["n"] == 4
    assert client.post(f"/consultations/{cid}/marker", json={"tipo": "retomada"}, headers=h).status_code == 409
    assert client.post(f"/consultations/{cid}/marker", json={"tipo": "outro"}, headers=h).status_code == 422


def test_marco_nao_e_fala_para_as_regras_nem_para_a_cobertura_e_nao_se_edita(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [(f["falante"], f["texto"]) for f in SCRIPT[:6]])
    base = client.get(f"/consultations/{cid}", headers=h).json()["insights"]
    client.post(f"/consultations/{cid}/marker", json={"tipo": "pausa"}, headers=h)
    client.post(f"/consultations/{cid}/marker", json={"tipo": "retomada"}, headers=h)
    now = client.get(f"/consultations/{cid}", headers=h).json()
    assert [s["origem"] for s in now["segmentos"]].count("marco") == 2
    assert now["insights"]["falas_analisadas"] == 8
    assert now["insights"]["comunicacao"] == base["comunicacao"]  # as 6 falas, sem os marcos
    assert {t["rotulo"] for t in now["insights"]["queixas"]} == {t["rotulo"] for t in base["queixas"]}
    assert client.patch(f"/consultations/{cid}/segments/7", json={"texto": "x"}, headers=h).status_code == 409
    done = client.post(f"/consultations/{cid}/finish", headers=h).json()
    assert all(f["n"] not in (7, 8) for f in done["cobertura"]["fora"])
    assert done["cobertura"]["total"] >= 1


def test_revogacao_encerra_a_captura_inativa_o_consent_e_a_nota_sai_so_com_regras(client, auth, writeback):
    h = auth("medico")
    created = start(client, h)
    cid, ref = created["id"], created["consentimento"]["fhir_ref"]
    post_segments(client, h, cid, [("paciente", "Estou com tontura")])
    res = client.post(f"/consultations/{cid}/marker", json={"tipo": "revogado"}, headers=h)
    assert res.status_code == 200 and res.json()["consentimento"]["estado"] == "revogado"
    assert res.json()["segmentos"][-1]["marco"] == "revogado"
    assert writeback.get("Consent", ref.split("/")[1])["status"] == "inactive"
    assert post_segments(client, h, cid, [("paciente", "e febre")]).status_code == 403
    assert client.post(f"/consultations/{cid}/insights", headers=h).status_code == 403
    assert client.post(f"/consultations/{cid}/marker", json={"tipo": "retomada"}, headers=h).status_code == 409
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200 and done.json()["nota_meta"]["sem_ia"] is True
    timeline = client.get("/patients/cen6/timeline", params={"kinds": "consent"}, headers=h).json()
    assert any(e["source_ref"] == ref and "revogado" in e["summary"] for e in timeline)


def test_emocoes_false_deixa_as_emocoes_vazias(client, auth):
    h = auth("medico")
    falas = [(f["falante"], f["texto"]) for f in SCRIPT]
    com = start(client, h)["id"]
    post_segments(client, h, com, falas)
    ligado = client.get(f"/consultations/{com}", headers=h).json()["insights"]
    assert ligado["emocoes"] and ligado["linha_emocional"]

    fin = {"transcricao": True, "ia_externa": True, "traducao": True, "emocoes": False}
    sem = start(client, h, consentimento=CONSENT | {"finalidades": fin})["id"]
    post_segments(client, h, sem, falas)
    assert client.post(f"/consultations/{sem}/insights", params={"force": True}, headers=h).status_code == 200
    desligado = client.get(f"/consultations/{sem}", headers=h).json()["insights"]
    assert desligado["emocoes"] == [] and desligado["linha_emocional"] == []
    assert desligado["comunicacao"]["falas"] == len(SCRIPT)  # o resto da análise segue igual
    assert {t["rotulo"] for t in desligado["queixas"]} == {t["rotulo"] for t in ligado["queixas"]}
    done = client.post(f"/consultations/{sem}/finish", headers=h).json()
    assert done["insights"]["emocoes"] == []


def test_lista_de_acompanhantes_para_o_consentimento(client, auth):
    res = client.get("/patients/cen6/consult/acompanhantes", headers=auth("medico"))
    assert res.status_code == 200
    assert any(rp["id"] == "cen6-fam-0" and rp["nome"] for rp in res.json())


# ---------- verificação independente (c1): nenhuma via de LLM com recusa, ia_externa=false ou revogação ----------
def _sem_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_mode", "real")

    def never(*_a, **_k):
        raise AssertionError("o LLM não pode ser chamado sem o consentimento do paciente")

    monkeypatch.setattr(llm_client, "_call", never)


def _varrer_vias_de_llm(client, h, cid, monkeypatch):
    """Cada rota que chega ao LLM: ou bloqueia (403) ou responde só por regra, sem nunca chamar `_call`."""
    _sem_llm(monkeypatch)
    assert client.post(f"/consultations/{cid}/insights", params={"force": True}, headers=h).status_code == 403
    assert client.post(f"/consultations/{cid}/translate", json={"ns": [1]}, headers=h).status_code == 403
    audio = client.post(f"/consultations/{cid}/audio?falante=medico", content=b"abc", headers=h)
    assert audio.status_code in (
        403,
        503,
    )  # 503: STT do servidor não configurado; a trava de STT é a finalidade "transcricao"
    for pergunta in ("Explique a fisiologia da tontura", "posso usar dipirona?", "qual o diagnóstico mais provável?"):
        res = client.post(f"/consultations/{cid}/ask", json={"pergunta": pergunta}, headers=h)
        assert res.status_code == 200, res.text
        assert res.json()["mensagem"]["resposta"]["modo"] != "ia"
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200, done.text
    assert done.json()["nota_meta"]["sem_ia"] is True


@pytest.mark.parametrize("variante", ["recusa", "ia_externa_false", "revogado"])
def test_nenhuma_via_de_llm_roda_sem_consentimento(client, auth, monkeypatch, variante):
    h = auth("medico")
    fin = {"transcricao": True, "ia_externa": False, "traducao": True, "emocoes": True}
    body = {
        "recusa": CONSENT | {"recusou": True},
        "ia_externa_false": CONSENT | {"finalidades": fin},
        "revogado": CONSENT,
    }[variante]
    cid = start(client, h, consentimento=body, idioma_paciente="es")["id"]
    post_segments(client, h, cid, [("paciente", "Tengo mareos"), ("medico", "Há quanto tempo?")])
    if variante == "revogado":
        # a análise por IA já rodou com o consentimento; depois ele é retirado
        assert client.post(f"/consultations/{cid}/insights", headers=h).status_code == 200
        assert client.post(f"/consultations/{cid}/marker", json={"tipo": "revogado"}, headers=h).status_code == 200
    _varrer_vias_de_llm(client, h, cid, monkeypatch)


@pytest.mark.parametrize("variante", ["recusa", "revogado"])
def test_cirurgia_sem_consentimento_nao_chama_o_llm(client, auth, monkeypatch, variante):
    h = auth("medico")
    body = CONSENT | ({"recusou": True} if variante == "recusa" else {})
    cid = start(client, h, tipo="cirurgia", especialidade="ortopedia", consentimento=body)["id"]
    post_segments(client, h, cid, [("medico", "Iniciada a artroplastia total do joelho direito.")])
    if variante == "revogado":
        assert client.post(f"/consultations/{cid}/marker", json={"tipo": "revogado"}, headers=h).status_code == 200
    _sem_llm(monkeypatch)
    assert client.post(f"/consultations/{cid}/insights", params={"force": True}, headers=h).status_code == 403
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200, done.text
    assert done.json()["nota_meta"]["sem_ia"] is True


@pytest.mark.parametrize(
    "consentimento",
    [
        None,
        {},
        {"quem": "paciente"},
        {"quem": "paciente", "finalidades": {"transcricao": True}},
        CONSENT | {"quem": "medico"},
        CONSENT | {"recusou": "talvez"},
        CONSENT | {"finalidades": {**CONSENT["finalidades"], "ia_externa": "sim, claro"}},
        "autorizado",
        [CONSENT],
    ],
)
def test_consentimento_ausente_ou_malformado_devolve_422(client, auth, consentimento):
    res = client.post(
        "/patients/cen6/consultations",
        json={"tipo": "consulta", "consentimento": consentimento},
        headers=auth("medico"),
    )
    assert res.status_code == 422


def test_consentimento_nao_se_forja_com_campos_extras_nem_por_segmentos(client, auth, monkeypatch):
    h = auth("medico")
    fin = {"transcricao": True, "ia_externa": True, "traducao": True, "emocoes": True, "extra": True}
    forjado = CONSENT | {"recusou": True, "estado": "ativo", "permite": {"ia": True}, "finalidades": fin}
    created = start(client, h, consentimento=forjado)
    cid = created["id"]
    assert created["consentimento"]["permite"]["ia"] is False and created["consentimento"]["recusou"] is True
    # falas com origem "marco"/campos de marco não viram marco nem destravam nada
    res = client.post(
        f"/consultations/{cid}/segments",
        json={"segmentos": [{"falante": "paciente", "texto": "Tenho febre", "origem": "marco", "marco": "retomada"}]},
        headers=h,
    )
    assert res.status_code == 200 and res.json()["novos"][0]["origem"] == "digitado"
    assert res.json()["novos"][0]["marco"] is None
    assert client.post(f"/consultations/{cid}/marker", json={"tipo": "retomada"}, headers=h).status_code == 409
    client.patch(
        f"/consultations/{cid}/segments/1", json={"texto": "x", "consentimento": {"recusou": False}}, headers=h
    )
    client.post(f"/consultations/{cid}/finish", json={"segmentos": [], "consentimento": {"recusou": False}}, headers=h)
    _sem_llm(monkeypatch)
    assert client.post(f"/consultations/{cid}/insights", headers=h).status_code == 403


def test_audit_da_edicao_nunca_tem_o_texto_nem_o_antigo(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [("paciente", "Meu CPF é 123.456.789-09 e sinto tontura")])
    novo = "Corrigido: sinto dor de cabeça forte"
    res = client.patch(f"/consultations/{cid}/segments/1", json={"texto": novo, "falante": "medico"}, headers=h)
    assert res.status_code == 200
    with db_session.SessionLocal() as db:
        rows = [r.payload_json for r in db.scalars(select(AuditLog).where(AuditLog.entity_id == str(cid))).all()]
    dump = str(rows)
    for secret in ("123.456.789-09", "Corrigido", "dor de cabeça", "tontura"):
        assert secret not in dump
