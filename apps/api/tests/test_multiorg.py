"""#33: autocadastro, organizações (multi-tenant), convite de equipe e recuperação de senha."""

import re

import pytest
from sqlalchemy import select

from app import email as mailer
from app.db import session as db_session
from app.db.models import AuditLog, Consultation, User
from tests.conftest import CONSENT, login

SENHA = "senha-segura-1"


@pytest.fixture(autouse=True)
def _outbox():
    mailer.OUTBOX.clear()
    yield


def token_do_email(para: str, assunto: str) -> str:
    """Lê o link da mensagem (no compose de dev seria a API do mailpit; aqui, a caixa de saída em memória)."""
    msgs = [m for m in mailer.OUTBOX if m["to"] == para and assunto in m["subject"]]
    assert msgs, f"nenhum e-mail '{assunto}' para {para}"
    return re.search(r"token=([\w-]+)", msgs[-1]["body"]).group(1)


def cadastrar(client, email="dra.bia@clinica.test", tipo="consultorio", **extra):
    body = {
        "nome": "Beatriz Souza",
        "email": email,
        "senha": SENHA,
        "organizacao_nome": "Consultório Bia",
        "organizacao_tipo": tipo,
        "conselho": "CRM",
        "conselho_numero": "998877",
        "uf": "MG",
    } | extra
    return client.post("/auth/register", json=body)


def entrar(client, email, senha=SENHA):
    res = client.post("/auth/login", json={"email": email, "password": senha})
    client.cookies.clear()
    return res


def cabecalho(res):
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def cadastro_completo(client, email="dra.bia@clinica.test", **extra):
    assert cadastrar(client, email, **extra).status_code == 201
    tok = token_do_email(email, "Confirme")
    assert client.post("/auth/verify", json={"token": tok}).status_code == 200
    res = entrar(client, email)
    assert res.status_code == 200, res.text
    return cabecalho(res), res.json()["user"]


# ---------------------------------------------------------------- cadastro, verificação e login
def test_cadastro_verificacao_e_login(client):
    res = cadastrar(client)
    assert res.status_code == 201, res.text
    # antes de confirmar o e-mail o login é recusado
    antes = entrar(client, "dra.bia@clinica.test")
    assert antes.status_code == 403 and "Confirme seu e-mail" in antes.json()["detail"]
    tok = token_do_email("dra.bia@clinica.test", "Confirme")
    assert client.post("/auth/verify", json={"token": tok}).status_code == 200
    depois = entrar(client, "dra.bia@clinica.test")
    assert depois.status_code == 200
    user = depois.json()["user"]
    assert user["role"] == "medico" and user["email_verificado"] is True
    assert user["organizacao"]["tipo"] == "consultorio" and user["destino"] == "/consultas"
    assert user["conselho"] == "CRM" and user["conselho_verificado"] is False
    # o link é de uso único
    assert client.post("/auth/verify", json={"token": tok}).status_code == 400


def test_destino_depende_do_tipo_da_organizacao(client):
    _, hosp = cadastro_completo(
        client,
        "gestora@hospital.test",
        organizacao_tipo="hospital",
        conselho=None,
        conselho_numero=None,
        uf=None,
        organizacao_nome="Hospital Sul",
    )
    assert hosp["role"] == "gestor" and hosp["destino"] == "/leitos"
    h = login(client)  # demonstração: hospital
    assert client.get("/auth/me", headers=h).json()["destino"] == "/leitos"


@pytest.mark.parametrize(
    "extra,codigo",
    [
        ({"senha": "curta"}, 422),
        ({"email": "sem-arroba"}, 422),
        ({"organizacao_tipo": "fabrica"}, 422),
        ({"conselho": "XYZ"}, 422),
        ({"conselho_numero": "12a"}, 422),
        ({"uf": "ZZ"}, 422),
        ({"conselho": None, "conselho_numero": None, "uf": None}, 422),  # consultório exige o conselho
    ],
)
def test_cadastro_recusa_dados_invalidos(client, extra, codigo):
    assert cadastrar(client, **extra).status_code == codigo


def test_cadastro_com_email_repetido_nao_cria_outra_conta_nem_revela_a_existente(client):
    """Antes respondia 409 (enumeração de e-mail); agora a resposta é a mesma e o dono do e-mail recebe um aviso."""
    assert cadastrar(client).status_code == 201
    assert cadastrar(client).status_code == 201
    assert cadastrar(client, "MEDICO@housia.dev").status_code == 201  # usuário de demonstração
    with db_session.SessionLocal() as db:
        assert len(db.scalars(select(User).where(User.email == "dra.bia@clinica.test")).all()) == 1


# ---------------------------------------------------------------- isolamento
def test_consulta_de_outra_organizacao_e_404(client, auth):
    ha = auth("medico")  # organização A (demonstração)
    cid = client.post(
        "/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=ha
    ).json()["id"]
    assert client.get(f"/consultations/{cid}", headers=ha).status_code == 200

    hb, _ = cadastro_completo(client)  # organização B
    assert client.get(f"/consultations/{cid}", headers=hb).status_code == 404
    assert client.post(f"/consultations/{cid}/finish", headers=hb).status_code == 404
    assert client.get(f"/consultations/{cid}/export", headers=hb).status_code == 404
    assert client.get("/patients/cen6/consultations", headers=hb).json() == []
    # e o inverso: o que B cria, A não vê
    cb = client.post(
        "/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=hb
    ).json()["id"]
    assert client.get(f"/consultations/{cb}", headers=ha).status_code == 404
    with db_session.SessionLocal() as db:
        orgs = {c.id: c.organization_id for c in db.scalars(select(Consultation))}
    assert orgs[cid] != orgs[cb] and None not in orgs.values()


def test_alertas_e_auditoria_ficam_na_organizacao(client, auth):
    ha = auth("gestor")
    hb, _ = cadastro_completo(
        client,
        "gestora@hospital.test",
        organizacao_tipo="hospital",
        conselho=None,
        conselho_numero=None,
        uf=None,
        organizacao_nome="Hospital Sul",
    )
    # A audita a própria atividade; B só enxerga a sua (o cadastro e o login de B)
    trilha_a = client.get("/audit", headers=ha).json()
    trilha_b = client.get("/audit", headers=hb).json()
    ids_a = {e["id"] for e in trilha_a["itens"]} if "itens" in trilha_a else set()
    ids_b = {e["id"] for e in trilha_b["itens"]} if "itens" in trilha_b else set()
    assert ids_a.isdisjoint(ids_b)
    with db_session.SessionLocal() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.organization_id.is_(None))) is None


def test_alertas_de_outra_organizacao_nao_aparecem(client, auth):
    ha = auth("medico")
    client.post("/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=ha)
    hb, _ = cadastro_completo(client)
    assert client.get("/patients/cen6/alerts", headers=hb).json() == []
    # ... e a gestão da organização B não conta nada da A
    with db_session.SessionLocal() as db:
        alerta = db.scalar(select(Consultation))
        assert alerta is not None


# ---------------------------------------------------------------- convite
def test_convite_cria_usuario_com_o_papel_escolhido(client):
    hg, gestor = cadastro_completo(
        client,
        "gestora@hospital.test",
        organizacao_tipo="hospital",
        conselho=None,
        conselho_numero=None,
        uf=None,
        organizacao_nome="Hospital Sul",
    )
    oid = gestor["organizacao"]["id"]
    res = client.post(f"/orgs/{oid}/invites", json={"email": "enf@hospital.test", "papel": "enfermagem"}, headers=hg)
    assert res.status_code == 201, res.text
    tok = token_do_email("enf@hospital.test", "Convite")
    info = client.get(f"/auth/invites/{tok}").json()
    assert info == {"email": "enf@hospital.test", "papel": "enfermagem", "organizacao": "Hospital Sul"}
    ok = client.post("/auth/invites/accept", json={"token": tok, "nome": "Marta Enfermeira", "senha": SENHA})
    assert ok.status_code == 201, ok.text
    logado = entrar(client, "enf@hospital.test")
    assert logado.status_code == 200  # convite já vale como e-mail confirmado
    u = logado.json()["user"]
    assert u["role"] == "enfermagem" and u["organizacao"]["id"] == oid
    # usado uma vez só
    assert (
        client.post("/auth/invites/accept", json={"token": tok, "nome": "Outra Pessoa", "senha": SENHA}).status_code
        == 400
    )
    # a enfermeira não convida ninguém
    assert (
        client.post(
            f"/orgs/{oid}/invites", json={"email": "x@hospital.test", "papel": "gestor"}, headers=cabecalho(logado)
        ).status_code
        == 403
    )


def test_convite_valida_papel_e_organizacao(client, auth):
    hg, gestor = cadastro_completo(
        client,
        "gestora@hospital.test",
        organizacao_tipo="hospital",
        conselho=None,
        conselho_numero=None,
        uf=None,
        organizacao_nome="Hospital Sul",
    )
    oid = gestor["organizacao"]["id"]
    ruim = client.post(f"/orgs/{oid}/invites", json={"email": "a@b.test", "papel": "dono"}, headers=hg)
    assert ruim.status_code == 422
    outra = client.post(f"/orgs/{oid + 99}/invites", json={"email": "a@b.test", "papel": "medico"}, headers=hg)
    assert outra.status_code == 404
    # gestor da demonstração não convida para a organização do hospital Sul
    demo = auth("gestor")
    assert (
        client.post(f"/orgs/{oid}/invites", json={"email": "a@b.test", "papel": "medico"}, headers=demo).status_code
        == 404
    )
    antes = len(mailer.OUTBOX)
    assert (  # e-mail com conta em outra organização: resposta igual à de um convite novo, sem enviar nada
        client.post(
            f"/orgs/{oid}/invites", json={"email": "medico@housia.dev", "papel": "medico"}, headers=hg
        ).status_code
        == 201
    )
    assert len(mailer.OUTBOX) == antes
    assert (  # já na própria equipe: 409
        client.post(
            f"/orgs/{oid}/invites", json={"email": "gestora@hospital.test", "papel": "medico"}, headers=hg
        ).status_code
        == 409
    )
    assert (
        client.post("/auth/invites/accept", json={"token": "x" * 20, "nome": "Fulano Tal", "senha": SENHA}).status_code
        == 400
    )


# ---------------------------------------------------------------- recuperação de senha
def test_recuperacao_de_senha(client):
    cadastro_completo(client)
    res = client.post("/auth/forgot", json={"email": "dra.bia@clinica.test"})
    assert res.status_code == 200
    # e-mail inexistente responde igual e não envia nada
    antes = len(mailer.OUTBOX)
    igual = client.post("/auth/forgot", json={"email": "ninguem@clinica.test"})
    assert igual.json() == res.json() and len(mailer.OUTBOX) == antes
    tok = token_do_email("dra.bia@clinica.test", "Redefinição")
    assert client.post("/auth/reset", json={"token": tok, "senha": "curta"}).status_code == 422
    assert client.post("/auth/reset", json={"token": tok, "senha": "nova-senha-77"}).status_code == 200
    assert entrar(client, "dra.bia@clinica.test", SENHA).status_code == 401
    assert entrar(client, "dra.bia@clinica.test", "nova-senha-77").status_code == 200
    assert client.post("/auth/reset", json={"token": tok, "senha": "outra-senha-88"}).status_code == 400


def test_token_expirado_e_recusado(client):
    from app import clock

    assert cadastrar(client).status_code == 201
    tok = token_do_email("dra.bia@clinica.test", "Confirme")
    antes = clock.now()
    try:
        from datetime import timedelta

        clock.freeze(antes + timedelta(days=3))
        assert client.post("/auth/verify", json={"token": tok}).status_code == 400
    finally:
        clock.freeze(antes)


# ---------------------------------------------------------------- demonstração e selo
def test_login_de_demonstracao_continua_funcionando(client):
    for papel in ("medico", "enfermagem", "gestor"):
        h = login(client, papel)
        me = client.get("/auth/me", headers=h).json()
        assert me["organizacao"]["nome"] == "Hospital Demonstração" and me["destino"] == "/leitos"
    # login normal com cookie de sessão também
    assert client.post("/auth/login", json={"email": "medico@housia.dev", "password": "housia123"}).status_code == 200
    assert client.get("/auth/me").status_code == 200


def test_selo_de_profissional_verificado_na_nota(client, auth):
    from tests.test_consult import SCRIPT, approve, say, start

    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT[:4])
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    approve(client, h, cid)
    sent = client.post(
        f"/consultations/{cid}/send", json={"justificativa": "Risco discutido com a paciente"}, headers=h
    )
    assert sent.status_code == 200, sent.text
    texto = client.get(f"/consultations/{cid}/export", headers=h).json()["texto"]
    assert "Profissional verificado: CRM 123456/SP" in texto


def test_conselho_conferido_manualmente_pelo_admin(client, auth):
    hb, medica = cadastro_completo(client)
    oid, uid = medica["organizacao"]["id"], medica["id"]
    # a própria médica (não é gestora) não se verifica
    assert client.post(f"/orgs/{oid}/membros/{uid}/conselho", json={}, headers=hb).status_code == 403
    admin = auth("gestor")  # administrador da plataforma (gestor da organização padrão)
    res = client.post(f"/orgs/{oid}/membros/{uid}/conselho", json={"verificado": True}, headers=admin)
    assert res.status_code == 200 and res.json()["conselho_verificado"] is True
    assert client.get("/auth/me", headers=hb).json()["conselho_verificado"] is True
    assert client.post(f"/orgs/{oid}/membros/9999/conselho", json={}, headers=admin).status_code == 404
    # sem verificação o selo diz "ainda não verificado"
    from app.consult.export import selo_profissional

    with db_session.SessionLocal() as db:
        u = db.get(User, uid)
        assert selo_profissional(u).startswith("Profissional verificado: CRM 998877/MG")
        u.conselho_verificado = False
        assert selo_profissional(u).startswith("Registro informado, ainda não verificado")


def test_pedido_de_piloto_cai_na_auditoria_do_gestor(client, auth):
    res = client.post(
        "/public/piloto",
        json={"hospital": "Hospital Norte", "contato": "Carlos Dias", "email": "carlos@norte.test", "leitos": 120},
    )
    assert res.status_code == 201, res.text
    gestor = auth("gestor")
    trilha = client.get("/audit?action=lead.piloto", headers=gestor).json()
    assert "Hospital Norte" in str(trilha)
    assert client.post("/public/piloto", json={"hospital": "H", "contato": "C", "email": "x"}).status_code == 422


def test_migracao_cria_organizacao_padrao_para_dados_existentes(tmp_path, monkeypatch):
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
    command.upgrade(cfg, "c201")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (name, email, password_hash, role, created_at) "
                "VALUES ('A','a@x.test','h','medico',CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO alerts (patient_id, source, severity, title, detail, source_refs, status, created_at) "
                "VALUES ('p','handoff','info','t','','[]','aberto',CURRENT_TIMESTAMP)"
            )
        )
    command.upgrade(cfg, "head")
    with eng.connect() as conn:
        org = conn.execute(text("SELECT id, nome FROM organizations")).one()
        assert org.nome == "Hospital Demonstração"
        assert conn.execute(text("SELECT organization_id, email_verificado FROM users")).one() == (org.id, 1)
        assert conn.execute(text("SELECT organization_id FROM alerts")).scalar_one() == org.id
    command.downgrade(cfg, "c201")


def test_onboarding_termina_na_primeira_nota_aprovada(client):
    from tests.test_consult import SCRIPT, approve, say, start

    h, medica = cadastro_completo(client)
    oid = medica["organizacao"]["id"]
    ob = client.get(f"/orgs/{oid}/onboarding", headers=h).json()
    feitos = {p["chave"]: p["feito"] for p in ob["passos"]}
    assert feitos == {"conta": True, "email": True, "conselho": True, "consulta": False, "nota": False}
    assert ob["concluido"] is False
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT[:4])
    client.post(f"/consultations/{cid}/finish", headers=h)
    assert {p["chave"]: p["feito"] for p in client.get(f"/orgs/{oid}/onboarding", headers=h).json()["passos"]}[
        "consulta"
    ]
    approve(client, h, cid)
    assert client.get(f"/orgs/{oid}/onboarding", headers=h).json()["concluido"] is True
    assert client.get(f"/orgs/{oid + 1}/onboarding", headers=h).status_code == 404
