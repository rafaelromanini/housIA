"""#53 — audit_log à prova de adulteração: cadeia, verificação, imutabilidade, exportação e leituras."""

import csv
import hashlib
import hmac
import io
import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, delete, select, text, update
from sqlalchemy.exc import DatabaseError

from app import clock
from app.audit import chain, trigger
from app.auth.security import hash_password
from app.db import session as db_session
from app.db.models import AuditLog, Organization, User
from tests.conftest import login
from tests.test_consult import SCRIPT, say, start


def sql(stmt: str, **params):
    with db_session.engine.begin() as conn:
        return conn.execute(text(stmt), params)


def ids_da_cadeia() -> list[int]:
    with db_session.SessionLocal() as db:
        return list(db.scalars(select(AuditLog.id).where(AuditLog.hash.is_not(None)).order_by(AuditLog.id)))


@pytest.fixture
def com_historico(client, auth):
    """Algumas ações auditadas (login, consulta iniciada, leitura) para haver o que encadear."""
    h = auth("medico")
    cid = start(client, h)["id"]
    assert client.get(f"/consultations/{cid}", headers=h).status_code == 200
    return client, auth, cid


def verify(client, auth):
    res = client.get("/audit/verify", headers=auth("gestor"))
    assert res.status_code == 200, res.text
    return res.json()


def outra_org_gestor(client, nome="Hospital Outro"):
    with db_session.SessionLocal() as db:
        org = Organization(nome=nome, tipo="hospital")
        db.add(org)
        db.flush()
        db.add(
            User(
                name="Gestora Outra",
                email="gestora@outro.test",
                role="gestor",
                password_hash=hash_password("housia123"),
                organization_id=org.id,
                email_verificado=True,
            )
        )
        db.commit()
    res = client.post("/auth/login", json={"email": "gestora@outro.test", "password": "housia123"})
    client.cookies.clear()
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


# ---------- cadeia e verificação ----------
def test_cadeia_integra_encadeia_cada_linha_a_anterior(com_historico):
    client, auth, _ = com_historico
    out = verify(client, auth)
    assert out["integra"] is True and out["primeira_quebra"] is None
    assert out["encadeadas"] >= 3 and out["cabeca"]
    with db_session.SessionLocal() as db:
        rows = list(db.scalars(select(AuditLog).where(AuditLog.organization_id.is_not(None)).order_by(AuditLog.id)))
    por_org: dict[int, str | None] = {}
    for r in rows:
        assert r.prev_hash == por_org.get(r.organization_id)
        assert r.hash == chain.calcular(r, r.prev_hash)
        por_org[r.organization_id] = r.hash


def test_alterar_uma_linha_faz_o_verify_falhar(com_historico):
    client, auth, _ = com_historico
    alvo = ids_da_cadeia()[2]
    sql("UPDATE audit_log SET action = 'consulta.finalizada' WHERE id = :i", i=alvo)
    out = verify(client, auth)
    assert out["integra"] is False
    assert out["primeira_quebra"]["id"] == alvo and "alterada" in out["primeira_quebra"]["motivo"]


def test_adulterar_o_payload_tambem_quebra(com_historico):
    client, auth, cid = com_historico
    with db_session.SessionLocal() as db:
        alvo = db.scalar(select(AuditLog.id).where(AuditLog.action == "consulta.visualizada"))
    sql("UPDATE audit_log SET payload_json = :p WHERE id = :i", p=json.dumps({"patient_id": "outro"}), i=alvo)
    assert verify(client, auth)["primeira_quebra"]["id"] == alvo


def test_apagar_linha_do_meio_quebra_o_elo_seguinte(com_historico):
    client, auth, _ = com_historico
    ids = ids_da_cadeia()
    meio = ids[len(ids) // 2]
    seguinte = next(i for i in ids if i > meio)
    sql("DELETE FROM audit_log WHERE id = :i", i=meio)
    out = verify(client, auth)
    assert out["integra"] is False
    assert out["primeira_quebra"]["id"] == seguinte and "elo quebrado" in out["primeira_quebra"]["motivo"]


def test_remover_o_hash_de_uma_linha_e_quebra(com_historico):
    client, auth, _ = com_historico
    alvo = ids_da_cadeia()[2]
    sql("UPDATE audit_log SET hash = NULL, prev_hash = NULL WHERE id = :i", i=alvo)
    out = verify(client, auth)
    assert out["integra"] is False and out["primeira_quebra"]["id"] == alvo


def test_refazer_o_hash_sem_a_chave_nao_engana(com_historico, monkeypatch):
    client, auth, _ = com_historico
    alvo = ids_da_cadeia()[2]
    sql("UPDATE audit_log SET action = 'x.forjada' WHERE id = :i", i=alvo)
    with db_session.SessionLocal() as db:  # o atacante recalcula o hash com outra chave (não tem a do servidor)
        a = db.get(AuditLog, alvo)
        monkeypatch.setattr(chain, "chave", lambda: b"chave-do-atacante")
        novo = chain.calcular(a, a.prev_hash)
    monkeypatch.undo()
    sql("UPDATE audit_log SET hash = :h WHERE id = :i", h=novo, i=alvo)
    assert verify(client, auth)["primeira_quebra"]["id"] == alvo


def test_linhas_antigas_sem_hash_sao_anteriores_ao_encadeamento_e_nao_quebram(client, auth):
    with db_session.SessionLocal() as db:
        assert db.scalar(select(AuditLog.id).limit(1)) is None  # nada encadeado antes das linhas legadas
        org = db.scalar(select(User.organization_id).where(User.email == "gestor@housia.dev"))
        for n in range(3):
            db.add(AuditLog(action="legado.acao", entity="x", organization_id=org, payload_json={"n": n}))
        db.commit()
    h = auth("gestor")
    client.get("/audit", headers=h)  # ainda sem linhas novas
    out = client.get("/audit/verify", headers=h).json()
    assert out["integra"] is True and out["anteriores_ao_encadeamento"] >= 3 and out["encadeadas"] >= 1


def test_orm_e_sql_em_massa_nao_alteram_nem_apagam(com_historico):
    with db_session.SessionLocal() as db:
        a = db.scalar(select(AuditLog).limit(1))
        a.action = "adulterada"
        with pytest.raises(chain.AuditoriaImutavel):
            db.commit()
    with db_session.SessionLocal() as db:
        db.delete(db.scalar(select(AuditLog).limit(1)))
        with pytest.raises(chain.AuditoriaImutavel):
            db.commit()
    with db_session.SessionLocal() as db:
        with pytest.raises(chain.AuditoriaImutavel):
            db.execute(delete(AuditLog))
        with pytest.raises(chain.AuditoriaImutavel):
            db.execute(update(AuditLog).values(action="x"))


def test_gatilho_do_banco_barra_update_e_delete(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'g.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, action TEXT)"))
        for stmt in trigger.statements("sqlite"):
            conn.execute(text(stmt))
        conn.execute(text("INSERT INTO audit_log (action) VALUES ('a')"))
    for stmt in ("UPDATE audit_log SET action = 'b'", "DELETE FROM audit_log"):
        with pytest.raises(DatabaseError), engine.begin() as conn:
            conn.execute(text(stmt))
    with engine.begin() as conn:
        assert conn.execute(text("SELECT action FROM audit_log")).scalar() == "a"
    assert trigger.statements("postgresql") and "BEFORE TRUNCATE" in trigger.POSTGRES_UP[2]


def test_verify_so_o_gestor_e_so_da_propria_organizacao(com_historico):
    client, auth, _ = com_historico
    assert client.get("/audit/verify", headers=auth("medico")).status_code == 403
    assert client.get("/audit/verify", headers=auth("enfermagem")).status_code == 403
    assert client.get("/audit/verify").status_code == 401
    minha = verify(client, auth)
    sql("UPDATE audit_log SET action = 'x.forjada' WHERE id = :i", i=ids_da_cadeia()[1])
    outra = client.get("/audit/verify", headers=outra_org_gestor(client)).json()
    assert minha["integra"] is True and outra["integra"] is True  # a cadeia da outra organização é só dela
    assert outra["total"] <= 2
    assert verify(client, auth)["integra"] is False


# ---------- exportação ----------
def test_exportacao_json_traz_hash_assinatura_e_e_auditada(com_historico):
    client, auth, _ = com_historico
    h = auth("gestor")
    res = client.get("/audit/export", headers=h)
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    assert res.headers["x-conteudo-sha256"] == hashlib.sha256(res.content).hexdigest()
    assert (
        res.headers["x-assinatura"]
        == hmac.new(chain.chave(), res.headers["x-conteudo-sha256"].encode(), hashlib.sha256).hexdigest()
    )
    doc = res.json()
    conteudo = json.dumps(doc["itens"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert doc["conteudo_sha256"] == hashlib.sha256(conteudo).hexdigest() and doc["cadeia"]["integra"] is True
    assert doc["itens"] and all("name" not in i and "email" not in i for i in doc["itens"])  # sem PII além do id
    logado = client.get("/audit", params={"action": "audit.exportado"}, headers=h).json()
    assert logado["total"] == 1 and logado["items"][0]["payload"]["arquivo_sha256"] == res.headers["x-conteudo-sha256"]


def test_exportacao_csv_neutraliza_formula_e_filtra_periodo(com_historico):
    client, auth, cid = com_historico
    h = auth("gestor")
    with db_session.SessionLocal() as db:  # entidade com cara de fórmula de planilha
        from app.audit.service import log_action

        log_action(db, db.scalar(select(User).where(User.role == "gestor")), "teste.formula", "x", "=HYPERLINK(1)")
    res = client.get("/audit/export", params={"formato": "csv"}, headers=h)
    assert res.status_code == 200 and res.headers["x-conteudo-sha256"] == hashlib.sha256(res.content).hexdigest()
    linhas = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig")), delimiter=";"))
    assert linhas[0][-2:] == ["prev_hash", "hash"]
    celulas = [c for linha in linhas[1:] for c in linha]
    assert "'=HYPERLINK(1)" in celulas and "=HYPERLINK(1)" not in celulas
    ontem = (clock.now() - timedelta(days=3)).date().isoformat()
    antigo = client.get("/audit/export", params={"until": ontem}, headers=h).json()
    assert antigo["total"] == 0 and antigo["itens"] == []


def test_exportacao_sem_permissao_formato_invalido_e_outra_organizacao(com_historico):
    client, auth, _ = com_historico
    for papel in ("medico", "enfermagem"):
        assert client.get("/audit/export", headers=auth(papel)).status_code == 403
    assert client.get("/audit/export").status_code == 401
    assert client.get("/audit/export", params={"formato": "xml"}, headers=auth("gestor")).status_code == 422
    doc = client.get("/audit/export", headers=outra_org_gestor(client)).json()
    minhas = {i["id"] for i in client.get("/audit/export", headers=auth("gestor")).json()["itens"]}
    assert not minhas & {i["id"] for i in doc["itens"]}  # nenhuma linha de outra organização
    with db_session.SessionLocal() as db:
        assert db.scalar(select(AuditLog.id).where(AuditLog.action == "audit.exportado")) is not None


# ---------- leituras ----------
def acoes(action):
    with db_session.SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


def test_leituras_repetidas_viram_um_registro_so(com_historico):
    client, auth, cid = com_historico
    h = auth("medico")
    for _ in range(4):
        assert client.get(f"/consultations/{cid}", headers=h).status_code == 200
    assert len(acoes("consulta.visualizada")) == 1
    for _ in range(3):
        assert client.get("/patients/cen6/timeline", headers=h).status_code == 200
        assert client.get("/patients/cen6", headers=h).status_code == 200
    assert len(acoes("paciente.timeline_visualizada")) == 1 and len(acoes("paciente.visualizado")) == 1
    outro = auth("enfermagem")  # outra pessoa lendo o mesmo recurso tem o próprio registro
    client.get("/patients/cen6/timeline", headers=outro)
    assert len(acoes("paciente.timeline_visualizada")) == 2


def test_leitura_volta_a_ser_registrada_depois_da_janela(com_historico):
    client, auth, cid = com_historico
    h = auth("medico")
    antes = clock._frozen
    try:
        clock.freeze(antes + timedelta(minutes=6))
        client.get(f"/consultations/{cid}", headers=h)
    finally:
        clock.freeze(antes)
    assert len(acoes("consulta.visualizada")) == 2


def test_nota_e_exportacao_ficam_auditadas(com_historico):
    client, auth, cid = com_historico
    h = auth("medico")
    say(client, h, cid, SCRIPT)
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    for _ in range(2):
        assert client.get(f"/consultations/{cid}/note/diff", headers=h).status_code == 200
    assert len(acoes("consulta.nota_visualizada")) == 1
    login(client, "medico")  # o login também entra na cadeia
    assert verify(client, auth)["integra"] is True
