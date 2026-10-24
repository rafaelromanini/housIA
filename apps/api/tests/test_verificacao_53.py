# ruff: noqa: F811
"""#53 — verificação adversarial: trava sem rota de fuga, quebra de vidro, cadeia sob concorrência e export."""

import csv
import hashlib
import hmac
import io
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError

from app import clock
from app.audit import chain
from app.audit.service import log_action, log_leitura
from app.db import session as db_session
from app.db.models import AuditLog, Organization, User
from app.db.tenancy import set_org
from tests.test_audit_integridade import com_historico, outra_org_gestor, verify  # noqa: F401
from tests.test_consult_acesso import cenario, linhas, segundo_medico  # noqa: F401
from tests.test_consultorio import consulta_cen6, lista

MOTIVO_OK = "Atender intercorrência do leito 4"


def quebrar(client, h, cid, motivo=MOTIVO_OK):
    return client.post(f"/consultations/{cid}/quebra-de-vidro", json={"motivo": motivo}, headers=h)


def outra_org_id() -> int:
    with db_session.SessionLocal() as db:
        org = Organization(nome="Clínica Estranha", tipo="consultorio")
        db.add(org)
        db.commit()
        return org.id


# ---------- (1)(2) a trava não tem rota de fuga e não derruba a rota ----------
@pytest.mark.parametrize("bruto", ["abc", "1e0", "0x1", "-1", "0", "99999999999999999999"])
def test_id_malformado_ou_fora_da_faixa_nao_vira_500(cenario, bruto):
    client, _, dono, outro, _ = cenario
    for h in (dono, outro):
        assert client.get(f"/consultations/{bruto}", headers=h).status_code in (404, 422)


@pytest.mark.parametrize("forma", ["{cid}", "+{cid}", " {cid}", "%2B{cid}", "{cid}.0", "0{cid}", "{cid}_0"])
def test_grafias_do_id_que_o_fastapi_aceita_passam_pela_trava(cenario, forma):
    """O que o FastAPI lê como o id da consulta também é lido assim pela trava: nenhuma grafia burla o vínculo."""
    client, _, dono, outro, cid = cenario
    url = "/consultations/" + forma.format(cid=cid)
    assert client.get(url, headers=outro).status_code in (403, 422, 404)
    assert client.get(url, headers=dono).status_code in (200, 422, 404)


def test_pre_consulta_nao_abre_a_conversa_de_consulta_sem_vinculo(cenario):
    client, _, dono, outro, cid = cenario
    pid = client.get(f"/consultations/{cid}", headers=dono).json()["patient_id"]
    assert client.get(f"/patients/{pid}/pre-consulta", params={"consulta": cid}, headers=dono).status_code == 200
    assert client.get(f"/patients/{pid}/pre-consulta", params={"consulta": cid}, headers=outro).status_code == 403
    assert client.get(f"/patients/{pid}/pre-consulta", headers=outro).status_code == 200  # sem o parâmetro, segue
    assert quebrar(client, outro, cid).status_code == 200
    assert client.get(f"/patients/{pid}/pre-consulta", params={"consulta": cid}, headers=outro).status_code == 200


def test_busca_global_nao_revela_o_cid_de_consulta_sem_vinculo(client, auth):
    dono = auth("medico")
    cid = consulta_cen6(client, dono)
    alvo = client.get(f"/consultations/{cid}", headers=dono).json()["nota"]["cids"][0]["codigo"]
    outro = segundo_medico(client)
    assert [c for c in lista(client, outro, cid=alvo)["itens"] if c["id"] == cid] == []  # não vira oráculo do CID
    linha = next(c for c in lista(client, outro)["itens"] if c["id"] == cid)
    assert linha["cids"] == [] and linha["sem_vinculo"] is True
    assert cid not in [c["id"] for c in lista(client, outro, q=alvo)["itens"]]
    # o dono e a enfermagem seguem vendo; depois da quebra de vidro o outro médico também
    assert [c for c in lista(client, dono, cid=alvo)["itens"] if c["id"] == cid][0]["sem_vinculo"] is False
    assert any(c["id"] == cid for c in lista(client, auth("enfermagem"), cid=alvo)["itens"])
    assert quebrar(client, outro, cid).status_code == 200
    assert any(c["id"] == cid and c["cids"] for c in lista(client, outro, cid=alvo)["itens"])


# ---------- (3) quebra de vidro ----------
@pytest.mark.parametrize(
    "motivo",
    [
        "​" * 20,
        "ab​c‍d﻿e",  # menos de 10 letras de verdade
        "⠀" * 12,  # Braille vazio
        "ㅤ" * 12,  # preenchedor de Hangul
        " " * 15,
        "a" * 40,  # uma letra repetida
        "..........!!!!",
        "1111111111",
        "‮" + "‭" * 12,
        "          ",
        "",
    ],
)
def test_motivo_sem_conteudo_nao_e_justificativa(cenario, motivo):
    client, _, _, outro, cid = cenario
    assert quebrar(client, outro, cid, motivo).status_code == 422
    assert client.get(f"/consultations/{cid}", headers=outro).status_code == 403
    assert not linhas("consulta.quebra_de_vidro")


def test_motivo_gravado_sem_controle_de_texto_nem_inversao_de_direcao(cenario):
    client, _, _, outro, cid = cenario
    assert quebrar(client, outro, cid, "Atender‮ intercorrência​ do\tleito\n4").status_code == 200
    motivo = linhas("consulta.quebra_de_vidro")[0].payload_json["motivo"]
    assert motivo == "Atender intercorrência do leito 4"


def test_quebra_expirada_pode_ser_renovada_e_cada_uma_fica_na_trilha(cenario):
    client, _, _, outro, cid = cenario
    antes = clock._frozen
    try:
        assert quebrar(client, outro, cid).status_code == 200
        clock.freeze(antes + timedelta(hours=9))
        assert client.get(f"/consultations/{cid}", headers=outro).status_code == 403
        assert quebrar(client, outro, cid, "Retorno do paciente na UTI, conferir a conduta").status_code == 200
        assert client.get(f"/consultations/{cid}", headers=outro).status_code == 200
        assert len(linhas("consulta.quebra_de_vidro")) == 2
    finally:
        clock.freeze(antes)


def test_quebras_simultaneas_do_mesmo_usuario_ficam_validas_e_a_cadeia_integra(cenario):
    client, auth, _, outro, cid = cenario
    codigos = []

    def pedir():
        codigos.append(quebrar(client, outro, cid).status_code)

    ts = [threading.Thread(target=pedir) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert set(codigos) == {200}
    assert client.get(f"/consultations/{cid}", headers=outro).status_code == 200
    assert verify(client, auth)["integra"] is True


def test_quebra_de_um_nao_serve_a_outro_medico_nem_a_consulta_de_outra_org(cenario):
    client, _, _, outro, cid = cenario
    assert quebrar(client, outro, cid).status_code == 200
    terceiro = segundo_medico(client, email="dr.terceiro@housia.dev")
    assert client.get(f"/consultations/{cid}", headers=terceiro).status_code == 403
    estranho = segundo_medico(client, org_id=outra_org_id(), email="dr.estranho@outra.test")
    assert quebrar(client, estranho, cid).status_code == 404
    assert client.get(f"/consultations/{cid}", headers=estranho).status_code == 404


# ---------- (4) cadeia ----------
def test_escritas_simultaneas_nao_bifurcam_a_cadeia(client, auth):
    auth("medico")
    erros = []

    def escrever(n):
        try:
            for i in range(12):
                with db_session.SessionLocal() as db:
                    u = db.scalar(select(User).where(User.email == "medico@housia.dev"))
                    log_action(db, u, "teste.corrida", "t", i, {"n": n})
        except Exception as exc:  # noqa: BLE001
            erros.append(repr(exc))

    ts = [threading.Thread(target=escrever, args=(n,)) for n in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not erros
    with db_session.SessionLocal() as db:
        out = chain.verificar(db, db.scalar(select(User.organization_id).where(User.email == "medico@housia.dev")))
        assert out["integra"] is True, out["primeira_quebra"]
        assert len(list(db.scalars(select(AuditLog).where(AuditLog.action == "teste.corrida")))) == 48


def test_rollback_nao_deixa_buraco_nem_elo_para_linha_inexistente(client, auth):
    auth("medico")
    with db_session.SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == "medico@housia.dev"))
        log_action(db, u, "teste.a", "t", 1)
        log_action(db, u, "teste.perdida", "t", 2, commit=False)
        db.rollback()
        log_action(db, u, "teste.b", "t", 3)
        assert chain.verificar(db, u.organization_id)["integra"] is True
        assert db.scalar(select(AuditLog).where(AuditLog.action == "teste.perdida")) is None


def test_organizacao_so_no_carimbo_da_sessao_entra_no_hash(client, auth):
    """Usuário legado sem organização numa sessão com organização: o carimbo do flush não pode mudar o assinado."""
    auth("medico")
    with db_session.SessionLocal() as db:
        org = db.scalar(select(User.organization_id).where(User.email == "medico@housia.dev"))
        legado = User(name="Legado", email="legado@housia.dev", role="medico", password_hash="x", organization_id=None)
        db.add(legado)
        db.commit()
        set_org(db, org)
        log_action(db, legado, "teste.legado", "t", 1)
        assert db.scalar(select(AuditLog.organization_id).where(AuditLog.action == "teste.legado")) == org
        assert chain.verificar(db, org)["integra"] is True


def test_cadeias_de_organizacoes_diferentes_sao_independentes(com_historico):
    client, auth, _ = com_historico
    outro = outra_org_gestor(client)
    assert client.get("/audit/verify", headers=outro).json()["integra"] is True
    assert verify(client, auth)["integra"] is True
    por_org: dict = {}
    with db_session.SessionLocal() as db:
        for a in db.scalars(select(AuditLog).where(AuditLog.hash.is_not(None)).order_by(AuditLog.id)):
            por_org.setdefault(a.organization_id, []).append(a)
    assert len(por_org) >= 2
    for da_org in por_org.values():  # nenhuma linha encadeia a hash de outra organização
        hashes = {a.hash for a in da_org}
        assert all(a.prev_hash is None or a.prev_hash in hashes for a in da_org)


def test_verify_de_cadeia_grande_e_linear(client, auth):
    auth("medico")
    with db_session.SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == "medico@housia.dev"))
        for i in range(4000):
            log_action(db, u, "teste.volume", "t", i, {"n": i}, commit=False)
        db.commit()
        t0 = time.perf_counter()
        out = chain.verificar(db, u.organization_id)
        assert out["integra"] is True and out["total"] >= 4000
        assert time.perf_counter() - t0 < 5


def test_assinatura_do_export_cobre_periodo_total_e_cabeca(com_historico):
    client, auth, _ = com_historico
    doc = client.get("/audit/export", headers=auth("gestor")).json()
    selado = {k: doc[k] for k in ("organizacao", "periodo", "total", "conteudo_sha256", "cabeca")}
    esperado = hmac.new(chain.chave(), chain.canonico_simples(selado), hashlib.sha256).hexdigest()
    assert doc["assinatura_hmac"] == esperado and doc["cabeca"] == doc["cadeia"]["cabeca"]
    for campo, valor in (("total", 1), ("organizacao", 99), ("cabeca", "0" * 64), ("periodo", {})):
        adulterado = dict(selado) | {campo: valor}
        assert hmac.new(chain.chave(), chain.canonico_simples(adulterado), hashlib.sha256).hexdigest() != esperado


def test_csv_neutraliza_formula_em_qualquer_coluna_de_texto(com_historico):
    client, auth, _ = com_historico
    with db_session.SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == "medico@housia.dev"))
        for i, ev in enumerate(("=1+1", "+cmd|' /C calc'!A0", "-2+3", "@SUM(1)", "\t=1", "\r=1")):
            log_action(db, u, f"teste.inj{i}", ev, ev, {"x": ev})
    corpo = client.get("/audit/export", params={"formato": "csv"}, headers=auth("gestor")).content.decode("utf-8-sig")
    vistas = 0
    for linha in csv.reader(io.StringIO(corpo), delimiter=";"):
        if linha[4].startswith("teste.inj"):
            vistas += 1
            assert linha[5][:1] not in ("=", "+", "-", "@", "\t", "\r") and linha[6][:1] not in ("=", "+", "-", "@")
    assert vistas == 6


def test_export_nao_vaza_a_chave_nem_dado_de_acesso(com_historico):
    client, auth, _ = com_historico
    for formato in ("json", "csv"):
        corpo = client.get("/audit/export", params={"formato": formato}, headers=auth("gestor")).text
        assert chain.chave().decode() not in corpo and "password" not in corpo.lower() and "@housia.dev" not in corpo


# ---------- (5) deduplicação de leituras ----------
def test_dedupe_nao_esconde_leitura_de_outro_usuario_nem_de_outro_documento(cenario):
    client, _, dono, outro, cid = cenario
    quebrar(client, outro, cid)
    client.get(f"/consultations/{cid}", headers=dono)
    client.get(f"/consultations/{cid}", headers=outro)
    client.get(f"/consultations/{cid}", headers=outro)
    quem = [a.user_id for a in linhas("consulta.visualizada")]
    assert len(quem) == 2 and len(set(quem)) == 2
    with db_session.SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == "medico@housia.dev"))
        assert log_leitura(db, u, "consulta.documento_visualizado", "consultation", cid, {"tipo": "receita"})
        assert log_leitura(db, u, "consulta.documento_visualizado", "consultation", cid, {"tipo": "atestado"})
        assert not log_leitura(db, u, "consulta.documento_visualizado", "consultation", cid, {"tipo": "receita"})


# ---------- (7) migração ----------
def test_migracao_c304_sobe_desce_e_sobe_com_os_gatilhos(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from app.config import get_settings

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "app/db/migrations"))
    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    command.upgrade(cfg, "c303")
    eng = create_engine(url)
    with eng.begin() as conn:  # linha antiga, anterior ao encadeamento
        conn.execute(
            text(
                "INSERT INTO audit_log (action, entity, payload_json, created_at) "
                "VALUES ('antiga','x','{}',CURRENT_TIMESTAMP)"
            )
        )
    command.upgrade(cfg, "c304")
    assert {"prev_hash", "hash"} <= {c["name"] for c in inspect(eng).get_columns("audit_log")}
    for stmt in ("UPDATE audit_log SET action='y'", "DELETE FROM audit_log"):
        with pytest.raises(DatabaseError), eng.begin() as conn:
            conn.execute(text(stmt))
    command.downgrade(cfg, "c303")
    assert not {"prev_hash", "hash"} & {c["name"] for c in inspect(eng).get_columns("audit_log")}
    with eng.begin() as conn:  # sem os gatilhos, a tabela volta a aceitar alteração
        conn.execute(text("UPDATE audit_log SET action='y'"))
    command.upgrade(cfg, "c304")  # sobe de novo sem erro


def test_sql_do_postgres_tem_a_forma_esperada():
    from app.audit import trigger

    up = " ".join(trigger.statements("postgresql"))
    assert "RETURNS trigger" in up and "BEFORE UPDATE OR DELETE" in up and "BEFORE TRUNCATE" in up
    down = trigger.statements("postgresql", up=False)
    assert [s.split()[0:2] for s in down] == [["DROP", "TRIGGER"], ["DROP", "TRIGGER"], ["DROP", "FUNCTION"]]
