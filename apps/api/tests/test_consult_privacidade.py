"""#31 retenção configurável, criptografia de campo, expurgo, exclusão pelo titular e áudio que não sai do hospital."""

import base64
import json

import pytest
from sqlalchemy import select, text

from app.config import get_settings
from app.consult import privacidade
from app.consult import service as consult_service
from app.db import crypto
from app.db import session as db_session
from app.db.models import AuditLog, Consultation
from tests.test_consult import approve, start
from tests.test_consult_transcricao_consentimento import post_segments

KEY = "k2:" + base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
OLD_KEY = "k1:segredo-antigo"


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(get_settings(), "data_encryption_key", KEY)


def consulta_enviada(client, h, falas=None):
    cid = start(client, h)["id"]
    post_segments(
        client,
        h,
        cid,
        falas or [("medico", "Qual remédio a senhora toma?"), ("paciente", "Tomo glibenclamida e estou com tontura")],
    )
    client.post(f"/consultations/{cid}/finish", headers=h)
    approve(client, h, cid)
    res = client.post(f"/consultations/{cid}/send", json={"justificativa": "Risco discutido com a paciente"}, headers=h)
    assert res.status_code == 200, res.text
    return cid


def raw_columns(cid):
    with db_session.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT segments_json, insights_json, note_json, ai_meta_json, note_meta_json "
                "FROM consultations WHERE id=:i"
            ),
            {"i": cid},
        ).one()
    return row


def audit(action):
    with db_session.SessionLocal() as db:
        return [r.payload_json for r in db.scalars(select(AuditLog).where(AuditLog.action == action))]


# ---------- criptografia ----------
def test_select_direto_nao_mostra_o_texto_e_o_app_continua_lendo(client, auth, key):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [("paciente", "Tomo glibenclamida todo dia")])
    client.post(f"/consultations/{cid}/finish", headers=h)
    cols = raw_columns(cid)
    assert all(c is None or c.startswith("enc:v1:k2:") for c in cols)
    assert "glibenclamida" not in "".join(c for c in cols if c)
    got = client.get(f"/consultations/{cid}", headers=h).json()
    assert got["segmentos"][0]["texto"] == "Tomo glibenclamida todo dia"
    assert got["nota"] is not None


def test_rotacao_de_chave_le_dado_antigo_e_cifra_com_a_nova(client, auth, monkeypatch):
    h = auth("medico")
    monkeypatch.setattr(get_settings(), "data_encryption_key", OLD_KEY)
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [("paciente", "Tomo glibenclamida")])
    assert raw_columns(cid)[0].startswith("enc:v1:k1:")
    monkeypatch.setattr(get_settings(), "data_encryption_key", f"{KEY},{OLD_KEY}")
    assert client.get(f"/consultations/{cid}", headers=h).json()["segmentos"][0]["texto"] == "Tomo glibenclamida"
    post_segments(client, h, cid, [("paciente", "e losartana")])
    assert raw_columns(cid)[0].startswith("enc:v1:k2:")
    monkeypatch.setattr(get_settings(), "data_encryption_key", "k3:outra")
    with pytest.raises(ValueError):
        crypto.decrypt(raw_columns(cid)[0])


def test_adulterar_o_valor_cifrado_falha(key):
    blob = crypto.encrypt({"a": 1})
    with pytest.raises(Exception):  # noqa: B017
        crypto.decrypt(blob[:-4] + "AAAA")
    assert crypto.decrypt(blob) == {"a": 1}


def test_sem_chave_continua_em_json_e_le_dado_antigo(client, auth, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_encryption_key", "")
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [("paciente", "Tomo glibenclamida")])
    assert json.loads(raw_columns(cid)[0])[0]["texto"] == "Tomo glibenclamida"
    assert not privacidade.panel()["criptografia_ativa"]


def test_migration_cifra_os_dados_existentes(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    monkeypatch.setattr(get_settings(), "data_encryption_key", "")
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "app/db/migrations")
    command.upgrade(cfg, "d201")
    import sqlalchemy as sa

    eng = sa.create_engine(url)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO consultations (patient_id, kind, specialty, patient_language, status, segments_json, "
                "extra_json, created_at) "
                "VALUES ('p', 'consulta', 'clinica_medica', 'pt-BR', 'em_andamento', :s, '{}', '2026-01-01')"
            ),
            {"s": json.dumps([{"n": 1, "texto": "glibenclamida"}])},
        )
    monkeypatch.setattr(get_settings(), "data_encryption_key", KEY)
    command.upgrade(cfg, "head")
    with eng.connect() as conn:
        raw = conn.execute(text("SELECT segments_json FROM consultations")).scalar_one()
    assert raw.startswith("enc:v1:k2:") and "glibenclamida" not in raw
    assert crypto.decrypt(raw)[0]["texto"] == "glibenclamida"
    command.downgrade(cfg, "d201")
    with eng.connect() as conn:
        assert "glibenclamida" in conn.execute(text("SELECT segments_json FROM consultations")).scalar_one()


# ---------- retenção e expurgo ----------
def test_retencao_por_tipo(monkeypatch):
    monkeypatch.setattr(get_settings(), "transcript_retention_days", "90")
    assert privacidade.retention_days() == {"consulta": 90, "visita": 90, "cirurgia": 90}
    monkeypatch.setattr(get_settings(), "transcript_retention_days", "consulta=0, cirurgia=365,lixo=3,visita=x")
    assert privacidade.retention_days() == {"consulta": 0, "visita": None, "cirurgia": 365}
    monkeypatch.setattr(get_settings(), "transcript_retention_days", "")
    assert privacidade.retention_days() == {"consulta": None, "visita": None, "cirurgia": None}


def test_purge_com_n_zero_apaga_o_texto_e_a_nota_mantem_as_refs_com_hash(client, auth, key, monkeypatch):
    monkeypatch.setattr(get_settings(), "ehr_writeback", True)
    h = auth("medico")
    cid = consulta_enviada(client, h)
    before = client.get(f"/consultations/{cid}", headers=h).json()
    textos = {s["n"]: s["texto"] for s in before["segmentos"]}
    refs = {r for sec in before["nota"].values() for r in _refs(sec)}
    assert refs

    monkeypatch.setattr(get_settings(), "transcript_retention_days", "consulta=0")
    from app.consult import purge

    assert purge.main() == 1
    assert purge.main() == 0  # idempotente

    after = client.get(f"/consultations/{cid}", headers=h).json()
    assert all(s["texto"] == "" and s["expurgada"] for s in after["segmentos"])
    assert "Tomo glibenclamida" not in json.dumps(after, ensure_ascii=False)
    # a nota e as refs continuam; só a frase literal copiada da fala (#18) sai do CID, junto com o texto das falas
    sem_frase = {
        **before["nota"],
        "cids": [
            c | ({"frase": ""} if any(r.startswith("Fala/") for r in c["refs"]) else {}) for c in before["nota"]["cids"]
        ],
    }
    assert any(c["frase"] for c in before["nota"]["cids"]) and after["nota"] == sem_frase
    with db_session.SessionLocal() as db:
        hashes = db.get(Consultation, cid).extra_json["falas_hash"]
    cited = {r for r in refs if r.startswith("Fala/")}
    assert cited and set(hashes) == cited
    for ref, digest in hashes.items():
        assert digest == privacidade.sha256(textos[int(ref.split("/")[1])])
    [entry] = audit("consulta.expurgada")
    assert entry["tipo"] == "consulta" and "glibenclamida" not in json.dumps(entry)


def test_purge_respeita_o_prazo_e_nao_toca_consulta_em_andamento(client, auth, monkeypatch):
    monkeypatch.setattr(get_settings(), "ehr_writeback", True)
    h = auth("medico")
    cid = consulta_enviada(client, h)
    aberta = start(client, h)["id"]
    post_segments(client, h, aberta, [("paciente", "Tomo glibenclamida")])
    monkeypatch.setattr(get_settings(), "transcript_retention_days", "30")
    with db_session.SessionLocal() as db:
        assert privacidade.run(db) == 0  # enviada agora: ainda dentro do prazo
    monkeypatch.setattr(get_settings(), "transcript_retention_days", "visita=0,cirurgia=0")
    with db_session.SessionLocal() as db:
        assert privacidade.run(db) == 0  # outro tipo
    monkeypatch.setattr(get_settings(), "transcript_retention_days", "consulta=0")
    with db_session.SessionLocal() as db:
        assert privacidade.run(db) == 1
    assert client.get(f"/consultations/{aberta}", headers=h).json()["segmentos"][0]["texto"] != ""
    assert client.get(f"/consultations/{cid}", headers=h).json()["privacidade"]["expurgada_em"]


def test_purge_roda_no_startup(client, auth, monkeypatch):
    monkeypatch.setattr(get_settings(), "ehr_writeback", True)
    h = auth("medico")
    cid = consulta_enviada(client, h)
    monkeypatch.setattr(get_settings(), "transcript_retention_days", "0")
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()):
        pass
    assert client.get(f"/consultations/{cid}", headers=h).json()["segmentos"][0]["texto"] == ""


def _refs(node) -> list[str]:
    if isinstance(node, str):
        return [node] if node.startswith("Fala/") else []
    if isinstance(node, dict):
        return [r for v in node.values() for r in _refs(v)]
    if isinstance(node, list):
        return [r for v in node for r in _refs(v)]
    return []


# ---------- exclusão pelo titular ----------
def test_exclusao_pelo_titular_so_o_gestor_apaga_e_audita(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, [("paciente", "Tomo glibenclamida")])
    outra = start(client, h, pid="cen1")["id"]
    assert client.delete("/patients/cen6/consultation-data", headers=h).status_code == 403
    g = auth("gestor")
    res = client.delete("/patients/cen6/consultation-data", headers=g)
    assert res.status_code == 200 and res.json()["consultas_excluidas"] == 1
    assert client.get(f"/consultations/{cid}", headers=h).status_code == 404
    assert client.get(f"/consultations/{outra}", headers=h).status_code == 200
    [entry] = audit("consulta.dados_excluidos")
    assert entry["ids"] == [cid] and "glibenclamida" not in json.dumps(entry)
    with db_session.SessionLocal() as db:
        assert db.scalars(select(Consultation).where(Consultation.patient_id == "cen6")).first() is None


# ---------- áudio que não sai do hospital + painel ----------
def test_config_sem_stt_do_navegador_e_painel_reflete_a_configuracao(client, auth, monkeypatch):
    h = auth("medico")
    assert client.get("/consult/config", headers=h).json()["stt_navegador"] is True
    monkeypatch.setattr(get_settings(), "allow_browser_stt", False)
    monkeypatch.setattr(get_settings(), "transcript_retention_days", "consulta=45")
    cfg = client.get("/consult/config", headers=h).json()
    assert cfg["stt_navegador"] is False
    assert "não sai do hospital" in cfg["privacidade"]["voz"]
    got = client.get(f"/consultations/{start(client, h)['id']}", headers=h).json()
    assert got["privacidade"]["retencao_dias"] == 45 and "45 dia" in got["privacidade"]["retencao"]
    monkeypatch.setattr(get_settings(), "allow_browser_stt", True)
    assert "navegador" in privacidade.panel()["voz"]


# ---------- verificação cética (#31): chave, adulteração, rekey, vazamento no expurgo ----------
FALAS_SENSIVEIS = [
    ("medico", "Qual remédio a senhora toma e como dorme?"),
    ("paciente", "Tomo glibenclamida e estou com tontura zebra azul"),
    ("paciente", "tenho alergia a penicilina, deu urticária gigante na perna zebrada"),
    ("paciente", "minha mãe teve infarto aos cinquenta anos de idade em Xique-Xique"),
    ("paciente", "estou muito ansiosa e com medo de morrer dormindo sozinha"),
    ("paciente", "eu moro sozinha e não tenho ninguém que me ajude em casa Pimentão"),
]
TRECHOS = ["zebra azul", "zebrada", "Xique-Xique", "dormindo sozinha", "Pimentão", "como dorme", "aos cinquenta"]


def test_sem_a_chave_ou_com_a_chave_errada_nao_decifra(monkeypatch):
    monkeypatch.setattr(get_settings(), "data_encryption_key", KEY)
    cifrado = crypto.encrypt({"texto": "glibenclamida"})
    monkeypatch.setattr(get_settings(), "data_encryption_key", "")
    with pytest.raises(ValueError):  # chave ausente: erro claro, nunca o texto cifrado devolvido como se fosse dado
        crypto.decrypt(cifrado)
    outra = "k2:" + base64.urlsafe_b64encode(b"ffffffffffffffffffffffffffffffff").decode()  # mesmo id, outro segredo
    monkeypatch.setattr(get_settings(), "data_encryption_key", outra)
    with pytest.raises(Exception):  # noqa: B017 - InvalidTag
        crypto.decrypt(cifrado)


def test_trocar_o_id_do_valor_cifrado_falha(monkeypatch):
    # o id da chave entra como dado autenticado: apontar o mesmo texto cifrado para outra chave não decifra
    monkeypatch.setattr(get_settings(), "data_encryption_key", f"{KEY},k9:{KEY.split(':', 1)[1]}")
    blob = crypto.encrypt({"a": 1})
    assert blob.startswith("enc:v1:k2:")
    with pytest.raises(Exception):  # noqa: B017
        crypto.decrypt(blob.replace("enc:v1:k2:", "enc:v1:k9:", 1))


@pytest.mark.parametrize("valor", ["k1:", ":segredo", "k1:a,k1:b"])
def test_chave_malformada_e_recusada(valor, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_encryption_key", valor)
    with pytest.raises(ValueError):
        crypto.keys()


def test_formato_adulterado_nao_passa_como_json(key):
    for adulterado in ["enc:v2:k2:AAAA", "enc:v1:k2:", "enc:v1:k2:@@@"]:
        with pytest.raises(Exception):  # noqa: B017
            crypto.decrypt(adulterado)


def test_extra_json_e_acoes_tambem_ficam_cifrados(client, auth, key, monkeypatch):
    monkeypatch.setattr(get_settings(), "ehr_writeback", True)
    h = auth("medico")
    cid = consulta_enviada(client, h)
    with db_session.engine.connect() as conn:
        extra, acoes = conn.execute(
            text("SELECT extra_json, insight_actions_json FROM consultations WHERE id=:i"), {"i": cid}
        ).one()
    assert extra.startswith("enc:v1:") and "glibenclamida" not in extra and "nota_original" not in extra
    assert acoes is None or acoes.startswith("enc:v1:")
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 200


def test_rekey_cifra_o_que_ficou_em_json_puro_e_recifra_chave_antiga(client, auth, monkeypatch):
    from app.db import rekey

    monkeypatch.setattr(get_settings(), "ehr_writeback", True)
    h = auth("medico")
    monkeypatch.setattr(get_settings(), "data_encryption_key", "")
    cid = consulta_enviada(client, h)  # gravada sem chave
    assert "glibenclamida" in raw_columns(cid)[0]
    monkeypatch.setattr(get_settings(), "data_encryption_key", OLD_KEY)
    with db_session.SessionLocal() as db:
        assert rekey.run(db) == 1
        assert rekey.run(db) == 0  # idempotente
    assert raw_columns(cid)[0].startswith("enc:v1:k1:")
    assert "glibenclamida" not in "".join(c for c in raw_columns(cid) if c)
    # rotação: a nova na frente; depois do rekey a antiga pode sair
    monkeypatch.setattr(get_settings(), "data_encryption_key", f"{KEY},{OLD_KEY}")
    with db_session.SessionLocal() as db:
        assert rekey.run(db) == 1
    monkeypatch.setattr(get_settings(), "data_encryption_key", KEY)
    got = client.get(f"/consultations/{cid}", headers=h).json()
    assert any("glibenclamida" in s["texto"] for s in got["segmentos"])
    assert all(c is None or c.startswith("enc:v1:k2:") for c in raw_columns(cid))


def test_rekey_pula_linha_adulterada_sem_derrubar_as_outras(client, auth, monkeypatch, caplog):
    from app.db import rekey

    h = auth("medico")
    monkeypatch.setattr(get_settings(), "data_encryption_key", "")
    ruim, boa = start(client, h)["id"], start(client, h, pid="cen1")["id"]
    post_segments(client, h, ruim, [("paciente", "Tomo glibenclamida")])
    monkeypatch.setattr(get_settings(), "data_encryption_key", KEY)
    with db_session.engine.begin() as conn:
        conn.execute(text("UPDATE consultations SET note_json='enc:v1:k2:AAAAAAAA' WHERE id=:i"), {"i": ruim})
    with db_session.SessionLocal() as db:
        assert rekey.run(db) == 2
    assert raw_columns(boa)[0].startswith("enc:v1:k2:")
    assert "glibenclamida" not in caplog.text


def test_coluna_adulterada_no_banco_falha_fechado_e_o_expurgo_das_outras_segue(client, auth, key, monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "ehr_writeback", True)
    h = auth("medico")
    ruim = consulta_enviada(client, h)
    boa = consulta_enviada(client, h, [("paciente", "Tomo losartana todo dia de manhã")])
    with db_session.engine.begin() as conn:
        raw = conn.execute(text("SELECT segments_json FROM consultations WHERE id=:i"), {"i": ruim}).scalar_one()
        conn.execute(
            text("UPDATE consultations SET segments_json=:v WHERE id=:i"), {"v": raw[:-6] + "AAAAAA", "i": ruim}
        )
    monkeypatch.setattr(get_settings(), "transcript_retention_days", "consulta=0")
    with db_session.SessionLocal() as db:
        assert privacidade.run(db) == 1  # a adulterada fica de fora, a outra é expurgada
    assert client.get(f"/consultations/{boa}", headers=h).json()["segmentos"][0]["texto"] == ""
    assert "glibenclamida" not in caplog.text


def varrer(obj, texts):
    blob = json.dumps(obj, ensure_ascii=False)
    return [t for t in texts if t in blob]


def test_expurgo_nao_deixa_trecho_em_insights_metadados_copiloto_auditoria_nem_logs(
    client, auth, key, monkeypatch, caplog
):
    import logging

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(get_settings(), "ehr_writeback", True)
    h = auth("medico")
    cid = consulta_enviada(client, h, FALAS_SENSIVEIS)
    ask = client.post(
        f"/consultations/{cid}/ask", json={"pergunta": "ela disse zebra azul, posso dar dipirona?"}, headers=h
    )
    assert ask.status_code in (200, 409)
    with db_session.SessionLocal() as db:
        c = db.get(Consultation, cid)
        antes = json.dumps([c.insights_json, {k: v for k, v in c.note_meta_json.items() if k != "rascunho_ia"}])
        assert any(t in antes for t in TRECHOS)  # o cenário de fato guarda citações: o teste não é vácuo

    monkeypatch.setattr(get_settings(), "transcript_retention_days", "consulta=0")
    from app.consult import purge

    assert purge.main() == 1
    with db_session.SessionLocal() as db:
        c = db.get(Consultation, cid)
        for col in ("segments_json", "insights_json", "ai_meta_json", "note_meta_json", "insight_actions_json"):
            valor = getattr(c, col)
            if col == "note_meta_json":  # o rascunho da IA espelha a nota, que fica
                valor = {k: v for k, v in valor.items() if k != "rascunho_ia"}
            assert varrer(valor, TRECHOS) == [], col
        extra = {k: v for k, v in c.extra_json.items() if k not in ("nota_original", "versoes_nota")}
        assert varrer(extra, TRECHOS) == []
        assert c.note_json  # a nota fica
        audits = [r.payload_json for r in db.scalars(select(AuditLog))]
    assert varrer(audits, TRECHOS) == []
    assert not any(t in caplog.text for t in TRECHOS)
    after = client.get(f"/consultations/{cid}", headers=h).json()
    assert varrer(after["insights"], TRECHOS) == [] and varrer(after["segmentos"], TRECHOS) == []
    # reanalisar falas vazias apagaria os insights que ficam: recusado; a leitura e a exportação seguem
    assert client.post(f"/consultations/{cid}/insights?force=true", headers=h).status_code == 409
    assert client.post(f"/consultations/{cid}/translate", headers=h).status_code == 409
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 200


def test_expurgo_apaga_a_pergunta_do_copiloto_em_consulta_aberta_e_finalizada(client, auth, key, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    post_segments(client, h, cid, FALAS_SENSIVEIS)
    r = client.post(
        f"/consultations/{cid}/ask", json={"pergunta": "ela disse zebra azul, posso dar dipirona?"}, headers=h
    )
    assert r.status_code == 200
    client.post(f"/consultations/{cid}/finish", headers=h)
    monkeypatch.setattr(get_settings(), "transcript_retention_days", "consulta=0")
    with db_session.SessionLocal() as db:
        assert privacidade.run(db) == 1
        c = db.get(Consultation, cid)
        msgs = c.extra_json["copiloto"]["mensagens"]
    assert msgs and all(m["pergunta"] == "" for m in msgs)


def test_exclusao_so_o_gestor_com_login_e_so_do_paciente_pedido(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    outra = start(client, h, pid="cen1")["id"]
    assert client.delete("/patients/cen6/consultation-data").status_code == 401
    assert client.delete("/patients/cen6/consultation-data", headers=auth("enfermagem")).status_code == 403
    assert client.get(f"/consultations/{cid}", headers=h).status_code == 200
    g = auth("gestor")
    assert client.delete("/patients/inexistente/consultation-data", headers=g).json()["consultas_excluidas"] == 0
    assert client.get(f"/consultations/{outra}", headers=h).status_code == 200
    assert client.delete("/patients/cen6/consultation-data", headers=g).json()["consultas_excluidas"] == 1
    assert client.delete("/patients/cen6/consultation-data", headers=g).json()["consultas_excluidas"] == 0
    assert client.get(f"/consultations/{outra}", headers=h).status_code == 200
    with db_session.SessionLocal() as db:
        ids = [r.entity_id for r in db.scalars(select(AuditLog).where(AuditLog.action == "consulta.dados_excluidos"))]
    assert ids.count("cen6") == 2 and "inexistente" in ids


def test_painel_avisa_quando_o_log_de_prompts_esta_ligado(monkeypatch):
    monkeypatch.setattr(get_settings(), "log_prompts", True)
    assert "LOG_PROMPTS" in privacidade.panel()["ia"]
