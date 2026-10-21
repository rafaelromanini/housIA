"""#35 verificação cética: planos, teste grátis e módulos por plano sob entradas hostis."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app import clock
from app.db import session as db_session
from app.db.models import AuditLog, Entitlement
from tests.test_consultorio import abrir
from tests.test_discharge import RISKY
from tests.test_multiorg import cadastro_completo
from tests.test_planos import consultorio_org, iniciar, plano_de, trocar  # noqa: F401

HOSPITAL = dict(
    organizacao_tipo="hospital",
    conselho=None,
    conselho_numero=None,
    uf=None,
    organizacao_nome="Hospital Norte",
)

MODULOS_URLS = [
    ("get", "/patients/cen6/handoff/latest", None),
    ("post", "/patients/cen6/handoff", {}),
    ("post", "/patients/cen6/handoff/check", {}),
    ("get", "/patients/cen6/medrec/latest", None),
    ("post", "/patients/cen6/medrec", {}),
    ("get", "/patients/cen6/discharge-assessment/latest", None),
    ("get", "/patients/cen6/discharge-assessment/prefill", None),
    ("post", "/patients/cen6/discharge-assessment", {}),
    ("post", "/patients/cen6/discharge-documents", {}),
    ("get", "/patients/cen6/discharge-documents/latest", None),
    ("post", "/discharge-assessments/1/confirm", None),
    ("post", "/discharge-assessments/1/override", {"justification": "x" * 30}),
]


def test_gestor_de_organizacao_comum_nao_troca_o_proprio_plano(client):
    hg, ug = cadastro_completo(client, "gestora@norte.test", **HOSPITAL)
    assert ug["role"] == "gestor"
    org = ug["organizacao"]["id"]
    for corpo in ({"plano": "hospital"}, {"plano": "profissional", "limite_consultas": 99999}):
        assert client.put(f"/orgs/{org}/plano", json=corpo, headers=hg).status_code == 403
    assert client.put("/orgs/1/plano", json={"plano": "teste"}, headers=hg).status_code == 403
    assert client.put(f"/orgs/{org}/plano", json={"plano": "hospital"}).status_code == 401
    assert plano_de(client, hg)["plano"] == "hospital"


def test_gestor_de_outra_organizacao_nao_le_nem_troca_plano_alheio(client, consultorio_org, auth):
    _, org = consultorio_org
    hg, _ = cadastro_completo(client, "gestora@norte.test", **HOSPITAL)
    assert client.get(f"/orgs/{org}/plano", headers=hg).status_code == 404
    assert client.put(f"/orgs/{org}/plano", json={"plano": "hospital"}, headers=hg).status_code == 403


def test_corrida_na_10a_e_11a_consulta_so_uma_passa(client, consultorio_org):
    h, _ = consultorio_org
    for _ in range(9):
        assert iniciar(client, h).status_code == 200
    with ThreadPoolExecutor(8) as ex:
        codigos = sorted(f.result().status_code for f in [ex.submit(iniciar, client, h) for _ in range(8)])
    assert codigos == [200] + [402] * 7, codigos
    p = plano_de(client, h)
    assert p["usadas"] == 10 and p["restantes"] == 0
    with db_session.SessionLocal() as db:
        assert db.query(AuditLog).filter_by(action="consulta.iniciada").count() == 10


def test_sandbox_nao_conta_nem_antes_nem_depois_do_limite(client, consultorio_org):
    h, _ = consultorio_org
    for _ in range(5):
        assert iniciar(client, h, sandbox=True).status_code == 200
    assert plano_de(client, h)["usadas"] == 0
    for _ in range(10):
        assert iniciar(client, h).status_code == 200
    assert iniciar(client, h).status_code == 402
    assert iniciar(client, h, sandbox=True).status_code == 200
    p = plano_de(client, h)
    assert (p["usadas"], p["sandbox"]) == (10, 6)


def test_consulta_reaberta_ou_editada_nao_conta_duas_vezes(client, consultorio_org):
    h, _ = consultorio_org
    c = abrir(client, h, "cen6")
    cid = c["id"]
    assert plano_de(client, h)["usadas"] == 1
    client.post(f"/consultations/{cid}/finish", headers=h)
    client.put(f"/consultations/{cid}/note", json={"atestacao": True}, headers=h)
    client.get(f"/consultations/{cid}", headers=h)
    client.get(f"/patients/cen6/consultations", headers=h)
    assert plano_de(client, h)["usadas"] == 1
    for _ in range(9):
        abrir(client, h, "cen6")
    assert plano_de(client, h)["usadas"] == 10
    assert iniciar(client, h).status_code == 402


def test_trocar_de_plano_ida_e_volta_nao_zera_o_contador(client, consultorio_org, auth):
    h, org = consultorio_org
    admin = auth("gestor")
    for _ in range(10):
        iniciar(client, h)
    trocar(client, admin, org, "profissional")
    assert iniciar(client, h).status_code == 200
    assert trocar(client, admin, org, "teste").status_code == 200
    assert plano_de(client, h)["usadas"] == 11
    assert iniciar(client, h).status_code == 402  # já passou do limite: não reabre a cota


@pytest.mark.parametrize("plano", ["teste", "profissional"])
@pytest.mark.parametrize("metodo,url,corpo", MODULOS_URLS)
def test_modulo_bloqueado_em_toda_rota_do_modulo(client, consultorio_org, auth, plano, metodo, url, corpo):
    h, org = consultorio_org
    trocar(client, auth("gestor"), org, plano)
    kw = {} if corpo is None else {"json": corpo}
    res = getattr(client, metodo)(url, headers=h, **kw)
    assert res.status_code == 403, (res.status_code, res.text)


def test_modulo_bloqueado_nao_vaza_pela_ficha_do_paciente(client, auth):
    """Rebaixar um hospital para profissional não pode expor o risco de alta antigo no painel nem na ficha."""
    hm = auth("medico")
    admin = auth("gestor")
    client.post("/patients/cen3/discharge-assessment", json={"social_checklist": RISKY}, headers=hm)
    org = client.get("/auth/me", headers=hm).json()["organizacao"]["id"]
    assert client.put(f"/orgs/{org}/plano", json={"plano": "profissional"}, headers=admin).status_code == 200
    assert client.get("/patients/cen3/discharge-assessment/latest", headers=hm).status_code == 403
    assert client.get("/patients/cen3", headers=hm).json()["risco_alta"] is None
    painel = client.get("/patients", headers=hm).json()
    assert all(p["risco_alta"] is None for p in painel)


def test_plano_vencido_bloqueia_consulta_real_e_modulos_mas_nao_o_sandbox(client, consultorio_org, auth, monkeypatch):
    h, org = consultorio_org
    admin = auth("gestor")
    fim = (clock.now() + timedelta(days=1)).isoformat()
    assert trocar(client, admin, org, "hospital", vigencia_ate=fim).status_code == 200
    antes = clock.now
    monkeypatch.setattr(clock, "now", lambda: antes() + timedelta(days=2))
    assert iniciar(client, h).status_code == 402
    for metodo, url, corpo in MODULOS_URLS[:1] + MODULOS_URLS[3:4] + MODULOS_URLS[5:6]:
        assert getattr(client, metodo)(url, headers=h).status_code == 403
    p = plano_de(client, h)
    assert p["vencido"] is True and p["modulos"] == []
    # renovar com vigência futura reabre
    novo = (clock.now() + timedelta(days=5)).isoformat()
    assert trocar(client, admin, org, "profissional", vigencia_ate=novo).status_code == 200
    assert iniciar(client, h).status_code == 200


@pytest.mark.parametrize(
    "vigencia",
    ["abc", "", "2000-01-01T00:00:00Z", "2020-13-45", 0, -1, [], {}, "0000-00-00"],
)
def test_vigencia_invalida_devolve_422_e_nao_altera_o_plano(client, consultorio_org, auth, vigencia):
    h, org = consultorio_org
    res = trocar(client, auth("gestor"), org, "profissional", vigencia_ate=vigencia)
    assert res.status_code == 422, res.text
    assert plano_de(client, h)["plano"] == "teste"


def test_vigencia_sem_fuso_nao_derruba_o_servidor(client, consultorio_org, auth):
    h, org = consultorio_org
    futuro = (clock.now() + timedelta(days=3)).replace(tzinfo=None).isoformat()
    res = trocar(client, auth("gestor"), org, "profissional", vigencia_ate=futuro)
    assert res.status_code in (200, 422), res.text
    passado = (clock.now() - timedelta(days=3)).replace(tzinfo=None).isoformat()
    assert trocar(client, auth("gestor"), org, "profissional", vigencia_ate=passado).status_code == 422


@pytest.mark.parametrize("limite", [-1, 0, -999999, "dez", 1.5, True])
def test_limite_negativo_ou_invalido_e_recusado(client, consultorio_org, auth, limite):
    h, org = consultorio_org
    res = trocar(client, auth("gestor"), org, "profissional", limite_consultas=limite)
    assert res.status_code == 422, res.text
    assert plano_de(client, h)["plano"] == "teste"
    with db_session.SessionLocal() as db:
        assert db.query(Entitlement).filter_by(organization_id=org).one().limite_consultas == 10


@pytest.mark.parametrize("plano", ["TESTE", "", None, "hospital ", "gratis", 1])
def test_codigo_de_plano_invalido(client, consultorio_org, auth, plano):
    _, org = consultorio_org
    assert trocar(client, auth("gestor"), org, plano).status_code == 422


def test_limite_personalizado_menor_vale_e_o_aviso_acompanha(client, consultorio_org, auth):
    h, org = consultorio_org
    for _ in range(4):
        iniciar(client, h)
    assert trocar(client, auth("gestor"), org, "teste", limite_consultas=4).status_code == 200
    assert iniciar(client, h).status_code == 402
    assert "4 de 4" in plano_de(client, h)["aviso"]


def test_exclusao_de_dados_do_paciente_nao_devolve_cota(client, consultorio_org, auth):
    h, org = consultorio_org
    for _ in range(10):
        iniciar(client, h)
    client.delete("/patients/cen6/consultation-data", headers=auth("gestor"))
    assert iniciar(client, h).status_code == 402


def test_sem_token_ou_com_token_invalido(client):
    assert client.get("/consult/plano").status_code == 401
    assert client.get("/patients/cen6/discharge-assessment/latest").status_code == 401
    assert client.get("/orgs/1/plano", headers={"Authorization": "Bearer lixo"}).status_code == 401
