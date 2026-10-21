"""#34 Modo consultório: paciente avulso, sandbox fora do plano, busca global, fila de revisão e nota em PDF.

Tudo pelo caminho real (API → service → banco → FHIR local), em LLM_MODE=mock e com dados sintéticos.
"""

import re
import zlib
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import clock
from app.consult import consultorio
from app.consult import service as consult_service
from app.db import session as db_session
from app.db.models import AuditLog
from tests.conftest import CONSENT
from tests.test_consult import SCRIPT, approve, say
from tests.test_multiorg import cadastro_completo

PACIENTE = {
    "nome": "Marina Teste Albuquerque",
    "nascimento": "1962-03-14",
    "sexo": "F",
    "telefone": "(11) 90000-0000",
    "alergias": [{"substancia": "Dipirona", "reacao": "urticária", "criticidade": "alta"}],
    "medicamentos": [{"nome": "Losartana", "posologia": "50 mg, 1x ao dia", "via": "oral"}],
}
FALAS = [
    {"falante": "medico", "texto": "Bom dia, o que traz a senhora hoje?", "t": 1},
    {"falante": "paciente", "texto": "Doutora, estou com dor de cabeça e a pressão alta faz uma semana.", "t": 6},
    {"falante": "medico", "texto": "Está tomando a losartana direitinho?", "t": 12},
    {"falante": "paciente", "texto": "Tomo todo dia de manhã. Não tenho febre nem falta de ar.", "t": 17},
]


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def novo_paciente(client, h, **over):
    res = client.post("/patients", json=PACIENTE | over, headers=h)
    assert res.status_code == 201, res.text
    return res.json()


def abrir(client, h, pid, **body):
    res = client.post(
        f"/patients/{pid}/consultations", json={"tipo": "consulta", "consentimento": CONSENT} | body, headers=h
    )
    assert res.status_code == 200, res.text
    return res.json()


def falar(client, h, cid, falas):
    res = client.post(
        f"/consultations/{cid}/segments",
        json={"segmentos": [f | {"origem": "demo"} for f in falas]},
        headers=h,
    )
    assert res.status_code == 200, res.text
    return res.json()["insights"]


def finalizar(client, h, cid):
    res = client.post(f"/consultations/{cid}/finish", headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def consulta_cen6(client, h, *, sandbox=False, aprovar=False) -> int:
    cid = abrir(client, h, "cen6", sandbox=sandbox)["id"]
    ins = say(client, h, cid, SCRIPT)
    for m in ins["sugestoes_medicamentos"]:
        if m["severidade"] == "critico":
            client.post(
                f"/consultations/{cid}/insight-actions", json={"item_key": m["item_key"], "acao": "aceitar"}, headers=h
            )
    finalizar(client, h, cid)
    if aprovar:
        approve(client, h, cid)
    return cid


def pdf_texto(data: bytes) -> str:
    """Decodifica as strings hexadecimais das páginas (conteúdo comprimido) de volta para texto."""
    out = []
    for stream in re.findall(rb"stream\n(.*?)\nendstream", data, re.S):
        raw = zlib.decompress(stream).decode("ascii")
        out += [bytes.fromhex(h).decode("cp1252") for h in re.findall(r"<([0-9a-f]*)> Tj", raw)]
    return "\n".join(out)


# ======================= (1) paciente avulso =======================
def test_post_patients_cria_patient_alergia_e_medicamento_no_fhir(client, auth, store):
    h = auth("medico")
    p = novo_paciente(client, h)
    pid = p["id"]
    assert p["nome"] == PACIENTE["nome"] and p["alergias"] == 1 and p["medicamentos"] == 1 and p["avulso"] is True
    assert re.fullmatch(r"av\d+-[0-9a-f]{12}", pid)
    assert store.get("Patient", pid)["gender"] == "female"
    alergia = store.get("AllergyIntolerance", f"{pid}-alg-0")
    assert alergia["code"]["text"] == "Dipirona" and alergia["criticality"] == "high"
    assert alergia["patient"] == {"reference": f"Patient/{pid}"}
    med = store.get("MedicationStatement", f"{pid}-med-0")
    assert med["status"] == "active" and med["medicationCodeableConcept"]["text"] == "Losartana"
    assert med["subject"] == {"reference": f"Patient/{pid}"}
    # o prontuário lido pela consulta enxerga os dois
    detalhe = client.get(f"/patients/{pid}", headers=h)
    assert detalhe.status_code == 200 and detalhe.json()["idade"] is not None
    log = db_session.SessionLocal().scalars(select(AuditLog).where(AuditLog.action == "paciente.avulso_criado")).one()
    assert PACIENTE["nome"] not in str(log.payload_json) and log.payload_json["alergias"] == 1


def test_validacao_do_cadastro_avulso(client, auth):
    h = auth("medico")
    for ruim in (
        {"nome": "Marina"},  # sem sobrenome
        {"nascimento": "2999-01-01"},
        {"sexo": "X"},
        {"alergias": [{"substancia": "x"}]},
        {"medicamentos": [{"nome": "Losartana", "via": "v" * 80}]},
    ):
        assert client.post("/patients", json=PACIENTE | ruim, headers=h).status_code == 422, ruim
    assert client.post("/patients", json=PACIENTE, headers=auth("gestor")).status_code == 403
    assert client.post("/patients", json=PACIENTE).status_code == 401


def test_consulta_do_avulso_vai_da_abertura_a_exportacao(client, auth):
    h = auth("medico")
    pid = novo_paciente(client, h)["id"]
    cons = abrir(client, h, pid)
    assert cons["patient_id"] == pid and cons["sandbox"] is False
    ins = falar(client, h, cons["id"], FALAS)
    # a losartana declarada (MedicationStatement) é medicamento em uso, citada com a ref do próprio recurso
    refs = {r for m in ins["medicamentos"] for r in m["refs"]}
    assert f"MedicationStatement/{pid}-med-0" in refs
    nota = finalizar(client, h, cons["id"])["nota"]
    assert "Dipirona" in str(nota["alergias"])
    # antes da revisão não exporta; depois, texto e PDF
    assert client.get(f"/consultations/{cons['id']}/export.pdf", headers=h).status_code == 409
    approve(client, h, cons["id"])
    texto = client.get(f"/consultations/{cons['id']}/export", headers=h)
    assert texto.status_code == 200 and PACIENTE["nome"] in texto.json()["texto"]
    pdf = client.get(f"/consultations/{cons['id']}/export.pdf", headers=h)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-1.4") and pdf.content.rstrip().endswith(b"%%EOF")
    assert 'filename="nota-consulta-' in pdf.headers["content-disposition"]
    conteudo = pdf_texto(pdf.content)
    assert PACIENTE["nome"].upper() in conteudo.upper() and "Dipirona" in conteudo
    # o PDF sai reidentificado, sem o token de pseudonimização
    assert "[PACIENTE]" not in conteudo


def test_avulso_sem_alergia_informada_nao_inventa_alergia(client, auth, store):
    h = auth("medico")
    pid = novo_paciente(client, h, alergias=[], medicamentos=[])["id"]
    assert store.get("AllergyIntolerance", f"{pid}-alg-0") is None
    pid2 = novo_paciente(client, h, alergias=[], sem_alergias=True)["id"]
    assert store.get("AllergyIntolerance", f"{pid2}-alg-nka")["code"]["text"].startswith("Nega")
    assert abrir(client, h, pid)["id"]  # prontuário mínimo também abre a consulta


def test_avulso_so_abre_para_a_organizacao_dona(client, auth):
    h = auth("medico")
    pid = novo_paciente(client, h)["id"]
    cid = abrir(client, h, pid)["id"]
    outro, _ = cadastro_completo(client)
    assert client.get(f"/patients/{pid}", headers=outro).status_code == 404
    assert (
        client.post(
            f"/patients/{pid}/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=outro
        ).status_code
        == 404
    )
    assert client.get(f"/patients/{pid}/consultations", headers=outro).status_code == 404
    assert client.get(f"/consultations/{cid}", headers=outro).status_code == 404
    assert client.get("/patients/avulsos", headers=outro).json() == []
    # o repositório FHIR de teste é compartilhado entre testes: confere só a presença do paciente
    assert pid in [p["id"] for p in client.get("/patients/avulsos", headers=h).json()]
    assert pid in [p["id"] for p in client.get("/patients/avulsos?q=albuquerque", headers=h).json()]
    assert client.get("/patients/avulsos?q=zzz", headers=h).json() == []
    # id de outra organização forjado no caminho: também 404, sem revelar que existe
    assert client.get("/patients/av999-aaaaaaaaaaaa", headers=h).status_code == 404


# ======================= (2) sandbox =======================
def test_sandbox_lista_cenarios_sinteticos(client, auth):
    h = auth("medico")
    cen = client.get("/consult/sandbox/cenarios", headers=h).json()
    ids = {c["id"] for c in cen}
    assert {"cen6_consulta", "cen4_cirurgia", "cen_hispanica"} <= ids
    assert all(c["sintetico"] and c["patient_id"] and c["paciente"] for c in cen)


def test_consulta_no_sandbox_nao_aumenta_o_contador_do_plano(client, auth):
    h, _ = cadastro_completo(client)
    antes = client.get("/consult/plano", headers=h).json()
    assert antes == {"plano": "teste_gratis", "limite": 30, "usadas": 0, "restantes": 30, "sandbox": 0}
    consulta_cen6(client, h, sandbox=True, aprovar=True)
    meio = client.get("/consult/plano", headers=h).json()
    assert meio["usadas"] == 0 and meio["restantes"] == 30 and meio["sandbox"] == 1
    cid = abrir(client, h, "cen6")["id"]  # consulta real conta
    depois = client.get("/consult/plano", headers=h).json()
    assert depois["usadas"] == 1 and depois["restantes"] == 29 and depois["sandbox"] == 1
    assert client.get(f"/consultations/{cid}", headers=h).json()["sandbox"] is False


def test_teste_gratis_acaba_mas_o_sandbox_continua(client, auth, monkeypatch):
    h, _ = cadastro_completo(client)
    monkeypatch.setattr(consultorio, "PLANO_TESTE_LIMITE", 2)
    abrir(client, h, "cen6")
    abrir(client, h, "cen6")
    barrada = client.post(
        "/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=h
    )
    assert barrada.status_code == 402 and "teste grátis" in barrada.json()["detail"]
    assert abrir(client, h, "cen6", sandbox=True)["sandbox"] is True
    assert client.get("/consult/plano", headers=h).json()["restantes"] == 0


def test_hospital_nao_tem_limite_de_plano(client, auth):
    h = auth("medico")  # organização padrão (hospital)
    info = client.get("/consult/plano", headers=h).json()
    assert info["plano"] == "ilimitado" and info["limite"] is None and info["restantes"] is None


def test_sandbox_so_aceita_cenario_sintetico(client, auth):
    h = auth("medico")
    pid = novo_paciente(client, h)["id"]
    res = client.post(
        f"/patients/{pid}/consultations",
        json={"tipo": "consulta", "consentimento": CONSENT, "sandbox": True},
        headers=h,
    )
    assert res.status_code == 422 and "cenários sintéticos" in res.json()["detail"]


def test_sandbox_nunca_grava_no_prontuario_e_sai_marcado(client, auth, store, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ehr_writeback", True)
    h = auth("medico")
    antes = len(store.resources["DocumentReference"])
    cid = consulta_cen6(client, h, sandbox=True, aprovar=True)
    res = client.post(f"/consultations/{cid}/send", headers=h)
    assert res.status_code == 200 and res.json()["enviado"] is False and "sandbox" in res.json()["motivo"].lower()
    assert len(store.resources["DocumentReference"]) == antes
    assert client.get(f"/consultations/{cid}", headers=h).json()["status"] == "finalizada"
    assert consultorio.AVISO_SANDBOX in client.get(f"/consultations/{cid}/export", headers=h).json()["texto"]
    assert consultorio.AVISO_SANDBOX in pdf_texto(client.get(f"/consultations/{cid}/export.pdf", headers=h).content)


def test_sandbox_fora_da_jornada(client, auth):
    h = auth("medico")
    sandbox = consulta_cen6(client, h, sandbox=True)
    with db_session.SessionLocal() as db:
        from app.journey.service import load_facts

        facts = load_facts(db, clock.now())
        assert sandbox not in [c.id for rows in facts.consultations.values() for c in rows]
        real = consulta_cen6(client, h)
        facts = load_facts(db, clock.now())
        assert real in [c.id for rows in facts.consultations.values() for c in rows]


# ======================= (3) busca global =======================
def lista(client, h, **params):
    res = client.get("/consultations", params=params, headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def test_busca_por_cid_devolve_a_consulta(client, auth):
    h = auth("medico")
    cid = consulta_cen6(client, h)
    codigos = [i["codigo"] for i in client.get(f"/consultations/{cid}", headers=h).json()["nota"]["cids"]]
    assert codigos, "a nota do cenário traz CID"
    alvo = codigos[0]
    for termo in (alvo, alvo[:3], alvo.lower(), alvo.replace(".", "")):
        assert cid in [c["id"] for c in lista(client, h, cid=termo)["itens"]], termo
    assert lista(client, h, cid="Z99")["total"] == 0
    linha = next(c for c in lista(client, h, cid=alvo)["itens"] if c["id"] == cid)
    assert alvo in [i["codigo"] for i in linha["cids"]] and linha["paciente"]


def test_busca_por_nome_status_e_periodo(client, auth):
    h = auth("medico")
    pid = novo_paciente(client, h)["id"]
    aberta = abrir(client, h, pid)["id"]
    pronta = consulta_cen6(client, h)
    por_nome = lista(client, h, q="marina albuq")
    assert [c["id"] for c in por_nome["itens"]] == [aberta]
    assert por_nome["itens"][0]["paciente"] == PACIENTE["nome"]
    assert {c["id"] for c in lista(client, h, status="em_andamento")["itens"]} == {aberta}
    assert {c["id"] for c in lista(client, h, status="finalizada")["itens"]} == {pronta}
    hoje = clock.now().date()
    assert lista(client, h, de=hoje.isoformat(), ate=hoje.isoformat())["total"] == 2
    assert lista(client, h, de=(hoje + timedelta(days=1)).isoformat())["total"] == 0
    assert lista(client, h, ate=(hoje - timedelta(days=1)).isoformat())["total"] == 0
    assert client.get("/consultations", params={"status": "xis"}, headers=h).status_code == 422
    assert client.get("/consultations", params={"de": "2030-01-02", "ate": "2030-01-01"}, headers=h).status_code == 422
    assert client.get("/consultations", params={"limite": 0}, headers=h).status_code == 422


def test_busca_pagina(client, auth):
    h = auth("medico")
    ids = [abrir(client, h, "cen6")["id"] for _ in range(3)]
    p1 = lista(client, h, limite=2)
    assert p1["total"] == 3 and [c["id"] for c in p1["itens"]] == ids[::-1][:2]
    assert [c["id"] for c in lista(client, h, limite=2, deslocamento=2)["itens"]] == [ids[0]]


def test_busca_so_enxerga_a_propria_organizacao(client, auth):
    h = auth("medico")
    meu = consulta_cen6(client, h)
    outro, _ = cadastro_completo(client)
    assert lista(client, outro)["total"] == 0
    assert meu in [c["id"] for c in lista(client, h)["itens"]]
    assert client.get("/consultations", headers=auth("gestor")).status_code == 403


# ======================= (4) fila de notas para revisar =======================
def test_fila_mostra_notas_finalizadas_e_nao_revisadas(client, auth):
    h = auth("medico")
    em_andamento = abrir(client, h, "cen6")["id"]
    para_revisar = consulta_cen6(client, h)
    revisada = consulta_cen6(client, h, aprovar=True)
    fila = lista(client, h, status="aguardando_revisao")
    assert [c["id"] for c in fila["itens"]] == [para_revisar]
    assert fila["itens"][0]["aguardando_revisao"] is True and fila["itens"][0]["revisada"] is False
    assert em_andamento not in [c["id"] for c in fila["itens"]] and revisada not in [c["id"] for c in fila["itens"]]
    # revisar sem atestar mantém a nota na fila; atestar tira
    got = client.get(f"/consultations/{para_revisar}", headers=h).json()
    assert client.put(f"/consultations/{para_revisar}/note", json=got["nota"], headers=h).status_code == 200
    assert para_revisar in [c["id"] for c in lista(client, h, status="aguardando_revisao")["itens"]]
    approve(client, h, para_revisar)
    assert lista(client, h, status="aguardando_revisao")["total"] == 0
    assert {c["id"] for c in lista(client, h, revisada="true")["itens"]} == {para_revisar, revisada}


def test_editar_a_nota_atestada_devolve_a_consulta_para_a_fila(client, auth):
    h = auth("medico")
    cid = consulta_cen6(client, h, aprovar=True)
    got = client.get(f"/consultations/{cid}", headers=h).json()
    nota = got["nota"]
    nota["plano"]["texto"] += " Retorno em 30 dias."
    assert client.put(f"/consultations/{cid}/note", json=nota, headers=h).status_code == 200
    assert cid in [c["id"] for c in lista(client, h, status="aguardando_revisao")["itens"]]


# ======================= (5) onboarding de 3 passos =======================
def test_onboarding_de_3_passos_termina_na_primeira_nota_aprovada_no_sandbox(client, auth):
    h, _ = cadastro_completo(client)
    ob = client.get("/consult/onboarding", headers=h).json()
    assert [p["chave"] for p in ob["passos"]] == ["cenario", "nota", "aprovar"] and ob["concluido"] is False
    # consulta real não conta para o onboarding do sandbox
    consulta_cen6(client, h, aprovar=True)
    assert not any(p["feito"] for p in client.get("/consult/onboarding", headers=h).json()["passos"])
    cid = abrir(client, h, "cen6", sandbox=True)["id"]
    assert [p["feito"] for p in client.get("/consult/onboarding", headers=h).json()["passos"]] == [True, False, False]
    say(client, h, cid, SCRIPT)
    finalizar(client, h, cid)
    assert [p["feito"] for p in client.get("/consult/onboarding", headers=h).json()["passos"]] == [True, True, False]
    approve(client, h, cid)
    assert client.get("/consult/onboarding", headers=h).json()["concluido"] is True


# ======================= PDF =======================
def test_pdf_pagina_texto_longo_e_preserva_acentos():
    from app.consult import pdf

    texto = "\n".join(f"Linha {i}: ação, atenção, coração — não há ( parênteses ) nem \\ barra" for i in range(150))
    data = pdf.render("Nota de consulta", texto)
    assert data.startswith(b"%PDF-1.4") and b"/Count 3" in data
    saida = pdf_texto(data)
    assert "ação, atenção, coração" in saida and "Linha 149" in saida and "( parênteses )" in saida
    assert "página 3 de 3" in saida


def test_pdf_so_para_perfil_clinico(client, auth):
    h = auth("medico")
    cid = consulta_cen6(client, h, aprovar=True)
    assert client.get(f"/consultations/{cid}/export.pdf", headers=auth("gestor")).status_code == 403
    assert client.get("/consultations/9999/export.pdf", headers=h).status_code == 404
    audit = db_session.SessionLocal().scalars(select(AuditLog).where(AuditLog.action == "consulta.exportada_pdf"))
    assert list(audit) == []
    assert client.get(f"/consultations/{cid}/export.pdf", headers=h).status_code == 200
    assert (
        len(
            list(db_session.SessionLocal().scalars(select(AuditLog).where(AuditLog.action == "consulta.exportada_pdf")))
        )
        == 1
    )


# ======================= migração =======================
def test_migracao_e341_adiciona_sandbox_com_padrao_falso(tmp_path, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from app.config import get_settings

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "app/db/migrations"))
    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    command.upgrade(cfg, "e331")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO consultations (patient_id, kind, specialty, patient_language, status, segments_json, "
                "extra_json, created_at) VALUES ('p','consulta','clinica_medica','pt-BR','finalizada','[]','{}',"
                "CURRENT_TIMESTAMP)"
            )
        )
    command.upgrade(cfg, "head")
    with eng.connect() as conn:
        assert conn.execute(text("SELECT sandbox FROM consultations")).scalar_one() == 0
    command.downgrade(cfg, "e331")
    with eng.connect() as conn:
        assert "sandbox" not in [r[1] for r in conn.execute(text("PRAGMA table_info(consultations)"))]
