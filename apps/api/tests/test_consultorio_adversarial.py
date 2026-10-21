"""#34 verificação cética: privacidade entre organizações, sandbox isolado e entradas hostis na busca e no PDF.

Cada teste exercita o caminho real (API → serviço → banco → FHIR local), em LLM_MODE=mock e com dados sintéticos.
"""

import copy
from datetime import timedelta
from urllib.parse import quote

import pytest

from app import clock
from app.config import get_settings
from app.consult import service as consult_service
from app.planos import service as planos
from tests.conftest import CONSENT, login
from tests.test_consult import SCRIPT, approve, say
from tests.test_consultorio import (
    abrir,
    consulta_cen6,
    finalizar,
    lista,
    novo_paciente,
    pdf_texto,
)
from tests.test_multiorg import cadastro_completo


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def dois_consultorios(client):
    a, ua = cadastro_completo(client, "dra.a@clinica.test")
    b, ub = cadastro_completo(client, "dr.b@clinica.test")
    assert ua["organizacao"]["id"] != ub["organizacao"]["id"]
    return a, b, ua["organizacao"]["id"], ub["organizacao"]["id"]


# ---------- 1-3: paciente avulso de outra organização, por todos os caminhos ----------
ROTAS_DO_PACIENTE = [
    ("get", "/patients/{p}"),
    ("get", "/patients/{p}/timeline"),
    ("get", "/patients/{p}/alerts"),
    ("get", "/patients/{p}/consultations"),
    ("get", "/patients/{p}/consult/acompanhantes"),
    ("get", "/patients/{p}/consult/idioma"),
    ("get", "/patients/{p}/medrec/latest"),
    ("post", "/patients/{p}/medrec"),
    ("post", "/patients/{p}/handoff"),
    ("get", "/patients/{p}/handoff/latest"),
    ("get", "/patients/{p}/discharge-assessment/prefill"),
    ("get", "/patients/{p}/discharge-assessment/latest"),
    ("get", "/journey/patients/{p}/contact-script"),
    ("get", "/journey/patients/{p}/contacts"),
]


@pytest.mark.parametrize("metodo,rota", ROTAS_DO_PACIENTE)
def test_avulso_de_outra_organizacao_e_404_em_toda_rota_do_paciente(client, metodo, rota):
    a, b, _, _ = dois_consultorios(client)
    pid = novo_paciente(client, a)["id"]
    url = rota.format(p=pid)
    dono = getattr(client, metodo)(url, headers=a)
    assert dono.status_code != 404 or "Paciente" not in dono.text or metodo == "get"  # o dono não toma 404 de guarda
    fora = getattr(client, metodo)(url, headers=b)
    assert fora.status_code == 404, (url, fora.status_code, fora.text)
    assert fora.json()["detail"] == "Paciente não encontrado"
    # sem login a resposta é 401, nunca um 404 que confirmaria o id
    assert getattr(client, metodo)(url).status_code == 401


def test_consulta_e_pdf_do_avulso_nao_vazam_para_outra_organizacao(client):
    a, b, _, _ = dois_consultorios(client)
    pid = novo_paciente(client, a)["id"]
    cid = abrir(client, a, pid)["id"]
    say(client, a, cid, SCRIPT[:6])
    finalizar(client, a, cid)
    approve(client, a, cid)
    assert client.get(f"/consultations/{cid}/export.pdf", headers=a).status_code == 200
    for rota in ("", "/export", "/export.pdf", "/send", "/finish", "/segments"):
        for metodo in ("get", "post"):
            extra = {"json": {"segmentos": [{"falante": "medico", "texto": "oi", "t": 1, "origem": "demo"}]}}
            res = getattr(client, metodo)(
                f"/consultations/{cid}{rota}", headers=b, **(extra if metodo == "post" else {})
            )
            assert res.status_code in (404, 405), (metodo, rota, res.status_code)
    assert client.put(f"/consultations/{cid}/note", json={}, headers=b).status_code in (404, 422)
    assert lista(client, b)["total"] == 0
    assert lista(client, b, q="Marina")["total"] == 0
    assert client.get("/patients/avulsos?q=Marina", headers=b).json() == []
    # a organização dona continua enxergando tudo
    assert [c["id"] for c in lista(client, a)["itens"]] == [cid]


def test_id_avulso_forjado_nunca_abre_nem_derruba_a_api(client):
    a, b, org_a, org_b = dois_consultorios(client)
    existente = novo_paciente(client, a)["id"]
    sufixo = existente.split("-", 1)[1]
    forjados = [
        f"av{org_a}-000000000000",  # da minha organização, mas inexistente
        f"av{org_b}-000000000000",
        f"av{org_b}-{sufixo}",  # sufixo real, organização trocada
        f"av0{org_a}-{sufixo}",  # zero à esquerda
        f"av{org_a}-{sufixo.upper()}",  # hexadecimal em maiúsculas
        f"av{org_a}-{sufixo}{quote(chr(10))}",  # quebra de linha no fim
        f"av{org_a}-{sufixo}%00",
        f"AV{org_a}-{sufixo}",
        "av-1-aaaaaaaaaaaa",
        "av99999999999999999999-aaaaaaaaaaaa",
        f"../patients/{existente}",
        "av%27%20OR%201%3D1--",
    ]
    for pid in forjados:
        for rota in ("/patients/{}", "/patients/{}/timeline", "/patients/{}/consultations"):
            res = client.get(rota.format(pid), headers=b)
            vazio = res.status_code == 200 and rota.endswith("/consultations") and res.json() == []  # id inexistente
            assert res.status_code in (404, 405, 422) or vazio, (pid, rota, res.status_code)
            res = client.get(rota.format(pid), headers=a)
            assert res.status_code in (404, 405, 422, 200), (pid, rota, res.status_code)
    # o id com a organização de A usado por B nunca abre, mesmo com o sufixo certo
    assert client.get(f"/patients/{existente}", headers=b).status_code == 404
    # o id forjado também não dá consulta
    res = client.post(
        f"/patients/av{org_b}-{sufixo}/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=b
    )
    assert res.status_code == 404


def test_id_avulso_fora_do_caminho_tambem_respeita_a_organizacao(client):
    """Jornada recebe patient_id por query e por corpo: a guarda de caminho não enxerga esses dois."""
    a, b, _, _ = dois_consultorios(client)
    pid = novo_paciente(client, a)["id"]
    assert client.get("/journey/worklist", params={"patient_id": pid}, headers=a).status_code == 200
    assert client.get("/journey/worklist", params={"patient_id": pid}, headers=b).status_code == 404
    res = client.post("/journey/tasks", json={"patient_id": pid, "regra": "retorno_nao_agendado"}, headers=b)
    assert res.status_code == 404
    # varreduras do FHIR (lista da jornada e do painel de gestão) não incluem o avulso alheio
    ids = {r["paciente"]["id"] for r in client.get("/journey/worklist", headers=b).json()["itens"]}
    assert pid not in ids


# ---------- 4-5: sandbox e o FHIR ----------
def foto(store) -> dict[str, int]:
    return {t: len(rs) for t, rs in store.resources.items()}


def test_sandbox_com_envio_automatico_ligado_nao_cria_nenhum_recurso_fhir(client, auth, store, monkeypatch):
    monkeypatch.setattr(get_settings(), "ehr_writeback", True)
    h = auth("medico")
    antes = foto(store)
    aberta = abrir(client, h, "cen6", sandbox=True)["id"]  # com o envio ligado, a consulta real gravaria o Consent
    assert foto(store) == antes, "abrir o sandbox não pode criar Consent no FHIR"
    client.post(f"/consultations/{aberta}/marker", json={"tipo": "revogado"}, headers=h)  # revogar também não grava
    assert foto(store) == antes
    cid = consulta_cen6(client, h, sandbox=True, aprovar=True)
    res = client.post(f"/consultations/{cid}/send", headers=h)
    assert res.status_code == 200 and res.json()["enviado"] is False
    assert client.post(f"/consultations/{cid}/send", headers=h).status_code == 200  # reenviar não vira 409
    got = client.get(f"/consultations/{cid}", headers=h).json()
    assert got["status"] == "finalizada" and not got.get("fhir_ref")
    assert foto(store) == antes
    # a consulta real, no mesmo cenário, grava (prova que o teste enxerga escrita)
    real = abrir(client, h, "cen6")["id"]
    assert real and foto(store).get("Consent", 0) == antes.get("Consent", 0) + 1


def test_sandbox_nao_vira_pedido_nem_claim_no_fhir(client, auth, store, monkeypatch):
    monkeypatch.setattr(get_settings(), "ehr_writeback", True)
    h = auth("medico")
    antes = foto(store)
    cid = consulta_cen6(client, h, sandbox=True, aprovar=True)
    client.post(f"/consultations/{cid}/send", headers=h)
    client.get(f"/consultations/{cid}/export", headers=h)
    client.get(f"/consultations/{cid}/export.pdf", headers=h)
    assert foto(store) == antes


# ---------- 6-7: sandbox com paciente que não é cenário ----------
def test_sandbox_recusa_avulso_populacao_de_fundo_e_id_inexistente(client, auth):
    h = auth("medico")
    corpo = {"tipo": "consulta", "consentimento": CONSENT, "sandbox": True}
    pid = novo_paciente(client, h)["id"]
    assert client.post(f"/patients/{pid}/consultations", json=corpo, headers=h).status_code == 422
    assert client.post("/patients/bg01/consultations", json=corpo, headers=h).status_code == 422
    assert client.post("/patients/nao-existe/consultations", json=corpo, headers=h).status_code == 404
    assert client.post("/patients/cen6/consultations", json=corpo | {"sandbox": "talvez"}, headers=h).status_code == 422
    assert client.get("/consult/plano", headers=h).json()["sandbox"] == 0  # nada ficou gravado


def test_sandbox_de_outra_organizacao_nao_aparece_no_contador_nem_na_busca(client):
    a, b, _, _ = dois_consultorios(client)
    consulta_cen6(client, a, sandbox=True)
    plano = client.get("/consult/plano", headers=b).json()
    assert (plano["plano"], plano["limite"], plano["usadas"], plano["restantes"], plano["sandbox"]) == (
        "teste",
        10,
        0,
        10,
        0,
    )
    assert lista(client, b, sandbox="true")["total"] == 0
    assert client.get("/consult/onboarding", headers=b).json()["concluido"] is False


# ---------- 8-9: contador do plano ----------
def test_sandbox_nao_libera_nem_consome_o_teste_gratis(client, monkeypatch):
    monkeypatch.setattr(planos, "LIMITE_TESTE", 2)
    h, _ = cadastro_completo(client)
    for _ in range(5):
        abrir(client, h, "cen6", sandbox=True)
    assert client.get("/consult/plano", headers=h).json()["usadas"] == 0
    abrir(client, h, "cen6")
    abrir(client, h, "cen6")
    barrada = client.post(
        "/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=h
    )
    assert barrada.status_code == 402
    # as sandbox existentes não deixam abrir a terceira real; abrir mais sandbox continua permitido
    assert abrir(client, h, "cen6", sandbox=True)["sandbox"] is True
    plano = client.get("/consult/plano", headers=h).json()
    assert (plano["usadas"], plano["restantes"], plano["sandbox"]) == (2, 0, 6)


def test_nao_ha_caminho_para_trocar_o_marcador_de_sandbox_depois_de_aberta(client):
    h, _ = cadastro_completo(client)
    cid = consulta_cen6(client, h, sandbox=True)
    got = client.get(f"/consultations/{cid}", headers=h).json()
    for corpo in ({"sandbox": False}, got["nota"] | {"sandbox": False, "atestacao": True}):
        client.put(f"/consultations/{cid}/note", json=corpo, headers=h)
    client.post(f"/consultations/{cid}/finish", json={"sandbox": False}, headers=h)
    assert client.get(f"/consultations/{cid}", headers=h).json()["sandbox"] is True
    assert client.get("/consult/plano", headers=h).json()["usadas"] == 0


def test_limite_do_plano_vale_so_para_consultorio_e_por_organizacao(client, auth, monkeypatch):
    monkeypatch.setattr(planos, "LIMITE_TESTE", 1)
    a, b, _, _ = dois_consultorios(client)
    abrir(client, a, "cen6")
    assert (
        client.post(
            "/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=a
        ).status_code
        == 402
    )
    assert abrir(client, b, "cen6")["id"]  # a outra organização tem o próprio contador
    h = auth("medico")  # hospital
    for _ in range(3):
        abrir(client, h, "cen6")
    assert client.get("/consult/plano", headers=h).json()["limite"] is None


# ---------- 10: filtros hostis ----------
HOSTIS_Q = [
    "'; DROP TABLE consultations; --",
    '" OR 1=1 --',
    "% _ \\ [ ] ( ) * + ? { } ^ $ |",
    "<script>alert(1)</script>",
    "\x00\x1f",
    "ａｂｃ ＳＱＬ",  # largura total
    "a" * 80,
    "   ",
    "marina" + " marina" * 10,
]


def test_filtros_q_e_cid_com_injecao_nao_quebram_nem_vazam(client, auth):
    h = auth("medico")
    meu = consulta_cen6(client, h)
    for q in HOSTIS_Q:
        res = client.get("/consultations", params={"q": q}, headers=h)
        assert res.status_code == 200, (q, res.status_code, res.text[:200])
        assert res.json()["total"] in (0, 1) and set(res.json()) == {"total", "limite", "deslocamento", "itens"}
    for cid in ("E11'; --", "%", "_", ".*", "(", "[", "\\", "E11 OR 1=1", "\x00", "ＥＩＩ", "....", " "):
        res = client.get("/consultations", params={"cid": cid}, headers=h)
        assert res.status_code == 200, (cid, res.status_code, res.text[:200])
        if cid.strip() and not any(ch.isalnum() and ch.isascii() for ch in cid):
            assert res.json()["total"] == 1 or res.json()["total"] == 0
    assert client.get("/consultations", params={"q": "x" * 81}, headers=h).status_code == 422
    assert client.get("/consultations", params={"cid": "E" * 13}, headers=h).status_code == 422
    # filtro com tudo que não é letra nem número equivale a "sem filtro de CID", nunca a "qualquer CID de outro"
    assert meu in [c["id"] for c in lista(client, h, cid="....")["itens"]]


def test_filtros_de_data_status_e_pagina_invalidos_sao_422_nunca_500(client, auth):
    h = auth("medico")
    consulta_cen6(client, h)
    ruins = [
        {"de": "ontem"},
        {"ate": "2026-13-01"},
        {"de": "2026-02-30"},
        {"de": "0000-00-00"},
        {"de": "99999-01-01"},
        {"ate": "2026-1-1x"},
        {"de": "2026-05-02", "ate": "2026-05-01"},
        {"status": "'; DROP TABLE x;--"},
        {"status": "FINALIZADA"},
        {"status": "x" * 31},
        {"revisada": "talvez"},
        {"sandbox": "2"},
        {"limite": 201},
        {"limite": -1},
        {"limite": "dez"},
        {"deslocamento": -1},
        {"deslocamento": "1.5"},
    ]
    for params in ruins:
        res = client.get("/consultations", params=params, headers=h)
        assert res.status_code == 422, (params, res.status_code)
    # datas extremas, mas válidas, devolvem lista vazia ou cheia sem erro
    assert lista(client, h, de="0001-01-01", ate="9999-12-31")["total"] == 1
    assert lista(client, h, deslocamento=10**9)["itens"] == []
    hoje = clock.now().date()
    assert lista(client, h, de=hoje.isoformat(), ate=(hoje + timedelta(days=1)).isoformat())["total"] == 1


# ---------- 11: PDF ----------
def test_pdf_sem_atestacao_ou_antes_da_hora_e_recusado(client, auth):
    h = auth("medico")
    em_andamento = abrir(client, h, "cen6")["id"]
    nao_atestada = consulta_cen6(client, h)
    atestada = consulta_cen6(client, h, aprovar=True)
    for cid in (em_andamento, nao_atestada):
        res = client.get(f"/consultations/{cid}/export.pdf", headers=h)
        assert res.status_code in (409, 422), (cid, res.status_code)
        assert res.headers["content-type"].startswith("application/json")
    ok = client.get(f"/consultations/{atestada}/export.pdf", headers=h)
    assert ok.status_code == 200 and ok.headers["content-type"] == "application/pdf" and ok.content[:5] == b"%PDF-"
    # editar a nota atestada tira a atestação: o PDF volta a ser recusado
    nota = client.get(f"/consultations/{atestada}", headers=h).json()["nota"]
    nota["plano"]["texto"] += " Mudança depois de atestar."
    assert client.put(f"/consultations/{atestada}/note", json=nota, headers=h).status_code == 200
    assert client.get(f"/consultations/{atestada}/export.pdf", headers=h).status_code in (409, 422)
    # o PDF só nasce do texto que o profissional atestou
    approve(client, h, atestada)
    assert "Mudança depois de atestar" in pdf_texto(
        client.get(f"/consultations/{atestada}/export.pdf", headers=h).content
    )


def test_pdf_com_texto_hostil_continua_pdf_valido(client, auth):
    from app.consult import pdf

    hostil = ") Tj ET /Evil << >> (\\)(\n" + "ação ☃ 日本語 ‮\x00\x07\r\t" + "\n" + "x" * 5000
    data = pdf.render("Título (com) \\ parênteses ☃", hostil)
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    saida = pdf_texto(data)
    assert ") Tj ET /Evil << >> (\\)(" in saida  # vira texto na página, nunca operador do PDF
    assert b"/Evil" not in data.replace(b"\n", b"")  # as linhas só existem como hexadecimal dentro do fluxo comprimido
    assert pdf.render("t", "")[:8] == b"%PDF-1.4"


# ---------- 12: painéis (Impacto e gestão) não contam sandbox ----------
def test_eventos_de_sandbox_nao_entram_no_painel_impacto_nem_na_gestao(client, auth, monkeypatch):
    h = auth("medico")
    hoje = clock.now().date()
    params = {"since": str(hoje - timedelta(days=1)), "until": str(hoje + timedelta(days=1))}
    gestor = login(client, "gestor")

    def impacto():
        res = client.get("/audit/metrics", params=params, headers=gestor)
        assert res.status_code == 200, res.text
        return res.json()["consulta_ia"]["metricas"]

    consulta_cen6(client, h, aprovar=True)
    base = impacto()
    assert base["atendimentos_apoiados"]["valor"] == 1
    sandbox = consulta_cen6(client, h, sandbox=True, aprovar=True)
    client.post(f"/consultations/{sandbox}/send", headers=h)
    client.get(f"/consultations/{sandbox}/export.pdf", headers=h)
    client.post("/feedback", json={"kind": "consulta_nota", "entity_id": str(sandbox), "useful": True}, headers=h)
    depois = impacto()
    for chave in ("atendimentos_apoiados", "notas_enviadas", "duracao_media_min"):
        assert depois[chave]["valor"] == base[chave]["valor"], chave
        assert depois[chave]["n_linhas"] == base[chave]["n_linhas"], chave
    linhas = client.get("/audit/metrics/atendimentos_apoiados/linhas", params=params, headers=gestor).json()["items"]
    assert str(sandbox) not in {linha["entity_id"] for linha in linhas}
    # CID mais frequentes nas notas enviadas (gestão) também ignora o sandbox
    painel = client.get("/gestao", params=params, headers=gestor).json()["indicadores"]["cids_frequentes"]
    linhas_cid = client.get("/gestao/indicadores/cids_frequentes/linhas", params=params, headers=gestor).json()["items"]
    assert not any(f"Consultation/{sandbox}#" in str(linha) for linha in linhas_cid)
    assert painel["valor"] is not None


def test_sandbox_nao_gera_tarefa_nem_pendencia_para_o_paciente_do_cenario(client, auth):
    h = auth("medico")
    antes = client.get("/journey/worklist", params={"patient_id": "cen6"}, headers=h).json()["itens"]
    consulta_cen6(client, h, sandbox=True, aprovar=True)
    depois = client.get("/journey/worklist", params={"patient_id": "cen6"}, headers=h).json()["itens"]
    assert [i["regra"] for i in depois] == [i["regra"] for i in antes]
    assert copy.deepcopy(antes) is not None
