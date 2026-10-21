"""#35 Planos e teste grátis de 10 consultas: limite, sandbox livre, troca manual e módulos hospitalares."""

from datetime import timedelta

import pytest

from app import clock
from app.db import session as db_session
from app.db.models import Entitlement, Plan
from tests.test_consultorio import CONSENT, abrir
from tests.test_multiorg import cadastro_completo


def iniciar(client, h, **over):
    return client.post(
        "/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": CONSENT} | over, headers=h
    )


def plano_de(client, h):
    return client.get("/consult/plano", headers=h).json()


def trocar(client, admin, org_id, plano, **body):
    return client.put(f"/orgs/{org_id}/plano", json={"plano": plano} | body, headers=admin)


@pytest.fixture
def consultorio_org(client):
    h, user = cadastro_completo(client)
    return h, user["organizacao"]["id"]


def test_a_11a_consulta_no_teste_devolve_402(client, consultorio_org):
    h, _ = consultorio_org
    for _ in range(10):
        assert iniciar(client, h).status_code == 200
    res = iniciar(client, h)
    assert res.status_code == 402 and "teste grátis acabou" in res.json()["detail"]
    p = plano_de(client, h)
    assert (p["plano"], p["limite"], p["usadas"], p["restantes"]) == ("teste", 10, 10, 0)


def test_sandbox_nao_tem_limite(client, consultorio_org):
    h, _ = consultorio_org
    for _ in range(10):
        iniciar(client, h)
    for _ in range(12):
        assert iniciar(client, h, sandbox=True).status_code == 200
    p = plano_de(client, h)
    assert p["usadas"] == 10 and p["sandbox"] == 12


def test_trocar_para_profissional_libera_novas_consultas(client, consultorio_org, auth):
    h, org = consultorio_org
    for _ in range(10):
        iniciar(client, h)
    assert iniciar(client, h).status_code == 402
    res = trocar(client, auth("gestor"), org, "profissional")
    assert res.status_code == 200 and res.json()["plano"] == "profissional" and res.json()["usadas"] == 10
    assert iniciar(client, h).status_code == 200
    p = plano_de(client, h)
    assert p["limite"] is None and p["restantes"] is None and p["usadas"] == 11 and p["aviso"] is None


def test_alta_403_no_profissional_e_liberada_no_hospital(client, consultorio_org, auth):
    h, org = consultorio_org
    admin = auth("gestor")
    trocar(client, admin, org, "profissional")
    url = "/patients/cen6/discharge-assessment/latest"
    res = client.get(url, headers=h)
    assert res.status_code == 403 and "Profissional" in res.json()["detail"]
    assert client.get("/patients/cen6/handoff/latest", headers=h).status_code == 403
    assert client.get("/patients/cen6/medrec/latest", headers=h).status_code == 403
    trocar(client, admin, org, "hospital")
    assert client.get(url, headers=h).status_code != 403
    assert client.get("/patients/cen6/handoff/latest", headers=h).status_code != 403


def test_clinica_libera_plantao_e_reconciliacao_mas_nao_alta(client, consultorio_org, auth):
    h, org = consultorio_org
    trocar(client, auth("gestor"), org, "clinica")
    assert client.get("/patients/cen6/handoff/latest", headers=h).status_code != 403
    assert client.get("/patients/cen6/medrec/latest", headers=h).status_code != 403
    assert client.get("/patients/cen6/discharge-assessment/latest", headers=h).status_code == 403


def test_hospital_padrao_tem_todos_os_modulos(client, auth):
    h = auth("medico")
    p = plano_de(client, h)
    assert p["plano"] == "hospital" and set(p["modulos"]) == {"passagem_plantao", "reconciliacao", "alta"}
    assert client.get("/patients/cen6/discharge-assessment/latest", headers=h).status_code != 403


def test_contador_da_tela_bate_com_o_backend(client, consultorio_org):
    h, _ = consultorio_org
    esperado = {}
    for n in range(1, 11):
        abrir(client, h, "cen6")
        me = client.get("/auth/me", headers=h).json()["plano"]
        p = plano_de(client, h)
        assert (me["usadas"], me["limite"], me["aviso"]) == (p["usadas"], p["limite"], p["aviso"]) and p["usadas"] == n
        esperado[n] = p["aviso"]
    assert all(esperado[n] is None for n in range(1, 8))  # sem aviso até a 7ª
    assert "8 de 10" in esperado[8] and "Restam 2" in esperado[8]  # aviso a partir da 8ª
    assert "acabou" in esperado[10]


def test_aviso_so_aparece_no_teste(client, consultorio_org, auth):
    h, org = consultorio_org
    for _ in range(8):
        iniciar(client, h)
    assert plano_de(client, h)["aviso"]
    trocar(client, auth("gestor"), org, "profissional")
    assert plano_de(client, h)["aviso"] is None


def test_so_o_administrador_da_plataforma_troca_o_plano(client, consultorio_org, auth):
    h, org = consultorio_org
    assert client.put(f"/orgs/{org}/plano", json={"plano": "hospital"}, headers=h).status_code == 403  # médico
    h2, u2 = cadastro_completo(client, "dr.outro@clinica.test")
    assert client.put(f"/orgs/{org}/plano", json={"plano": "hospital"}, headers=h2).status_code == 403
    assert plano_de(client, h)["plano"] == "teste"
    admin = auth("gestor")
    assert trocar(client, admin, org, "inexistente").status_code == 422
    assert trocar(client, admin, org, "clinica", modulos=["nada"]).status_code == 422
    assert trocar(client, admin, org, "profissional", limite_consultas=0).status_code == 422
    assert trocar(client, admin, 99999, "profissional").status_code == 404


def test_cada_organizacao_tem_o_proprio_plano_e_contador(client, consultorio_org, auth):
    h, org = consultorio_org
    h2, u2 = cadastro_completo(client, "dr.outro@clinica.test")
    for _ in range(10):
        iniciar(client, h)
    trocar(client, auth("gestor"), org, "profissional")
    assert plano_de(client, h2)["plano"] == "teste" and plano_de(client, h2)["usadas"] == 0
    res = client.get(f"/orgs/{u2['organizacao']['id']}/plano", headers=auth("gestor"))
    assert res.status_code == 200 and res.json()["usadas"] == 0  # o contador lê a organização pedida, não a do admin


def test_vigencia_vencida_bloqueia_consulta_e_modulos(client, consultorio_org, auth, monkeypatch):
    h, org = consultorio_org
    admin = auth("gestor")
    fim = (clock.now() + timedelta(days=30)).isoformat()
    assert trocar(client, admin, org, "hospital", vigencia_ate=fim).status_code == 200
    assert trocar(client, admin, org, "hospital", vigencia_ate="2000-01-01T00:00:00Z").status_code == 422
    assert iniciar(client, h).status_code == 200
    antes = clock.now
    monkeypatch.setattr(clock, "now", lambda: antes() + timedelta(days=31))
    res = iniciar(client, h)
    assert res.status_code == 402 and "venceu" in res.json()["detail"]
    assert client.get("/patients/cen6/discharge-assessment/latest", headers=h).status_code == 403
    assert plano_de(client, h)["vencido"] is True


def test_consulta_de_hospital_nao_tem_limite_e_conta_audit_sem_sandbox(client, auth):
    h = auth("medico")
    for _ in range(12):
        abrir(client, h, "cen6")
    p = plano_de(client, h)
    assert p["limite"] is None and p["usadas"] >= 12


def test_catalogo_e_direito_ficam_no_banco(client, consultorio_org):
    h, org = consultorio_org
    plano_de(client, h)
    with db_session.SessionLocal() as db:
        assert {p.codigo for p in db.query(Plan)} == {"teste", "profissional", "clinica", "hospital"}
        ent = db.query(Entitlement).filter_by(organization_id=org).one()
        assert (ent.plano, ent.limite_consultas, ent.modulos_json) == ("teste", 10, [])


def test_migracao_e351_cria_catalogo_e_direito_das_organizacoes_existentes(tmp_path, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from app.config import get_settings

    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "app/db/migrations"))
    command.upgrade(cfg, "e341")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO organizations (nome, tipo, created_at) VALUES ('Consultório X','consultorio','2026-01-01')"
            )
        )
        c.execute(
            text("INSERT INTO organizations (nome, tipo, created_at) VALUES ('Hospital Y','hospital','2026-01-01')")
        )
    command.upgrade(cfg, "e351")
    with eng.connect() as c:
        rows = dict(
            c.execute(
                text("SELECT o.tipo, e.plano FROM entitlements e JOIN organizations o ON o.id=e.organization_id")
            ).all()
        )
        assert rows == {"consultorio": "teste", "hospital": "hospital"}
        assert c.execute(text("SELECT limite_consultas FROM plans WHERE codigo='teste'")).scalar() == 10
    command.downgrade(cfg, "e341")
    with eng.connect() as c:
        assert not c.execute(text("SELECT name FROM sqlite_master WHERE name IN ('plans','entitlements')")).all()
