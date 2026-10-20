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
    assert after["nota"] == before["nota"]  # a nota e as refs continuam
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
