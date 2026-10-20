"""Prontidão de produção (#32): subida recusada com configuração de demonstração, sessão em cookie httpOnly com CSRF,
checklist lido da configuração em vigor, compose de produção sem 8080/5432 e chart Helm coerente."""

import re
from pathlib import Path

import pytest
import yaml

from app.config import DEFAULT_JWT_SECRET, Settings, get_settings
from app.prontidao import ConfiguracaoInsegura, ambiente, checklist, exigir_producao_segura, problemas
from tests.conftest import login

ESCRITA = "/consultations/inexistente/marker"  # escrita autenticada; o corpo inválido é de outra camada
RAIZ = Path(__file__).resolve().parents[3]
BOA = {
    "env": "production",
    "jwt_secret": "x" * 40,
    "llm_mode": "real",
    "database_url": "postgresql+psycopg://housia:Zk93-forte-qq@db:5432/housia",
}


def cfg(**kw) -> Settings:
    return Settings(_env_file=None, **{**BOA, **kw})


# --- startup -----------------------------------------------------------------------------------------------------


def test_producao_com_segredo_padrao_nao_sobe():
    with pytest.raises(ConfiguracaoInsegura, match="JWT_SECRET"):
        exigir_producao_segura(cfg(jwt_secret=DEFAULT_JWT_SECRET))


@pytest.mark.parametrize(
    "segredo", ["", "curto", "x" * 31, " " * 40, "troque-por-um-segredo-longo-de-pelo-menos-32-caracteres"]
)
def test_producao_com_segredo_curto_nao_sobe(segredo):
    with pytest.raises(ConfiguracaoInsegura, match="JWT_SECRET"):
        exigir_producao_segura(cfg(jwt_secret=segredo))


def test_producao_com_mock_so_com_aceite():
    with pytest.raises(ConfiguracaoInsegura, match="LLM_MODE=mock"):
        exigir_producao_segura(cfg(llm_mode="mock"))
    exigir_producao_segura(cfg(llm_mode="mock", allow_mock=True))


@pytest.mark.parametrize("senha", ["housia", "postgres", "password", "123456", "admin", "", "Housia", "POSTGRES"])
def test_producao_com_senha_padrao_do_postgres_nao_sobe(senha):
    with pytest.raises(ConfiguracaoInsegura, match="Postgres"):
        exigir_producao_segura(cfg(database_url=f"postgresql+psycopg://housia:{senha}@db:5432/housia"))


def test_producao_correta_sobe_e_desenvolvimento_nao_exige_nada():
    assert problemas(cfg()) == []
    exigir_producao_segura(cfg())
    assert problemas(Settings(_env_file=None, jwt_secret=DEFAULT_JWT_SECRET, llm_mode="mock")) == []


def test_lista_todos_os_problemas_de_uma_vez():
    ruins = problemas(cfg(jwt_secret="a", llm_mode="mock", database_url="postgresql+psycopg://u:housia@db/x"))
    assert len(ruins) == 3


def test_app_nao_sobe_em_producao_com_segredo_padrao(monkeypatch):
    from app.main import create_app

    s = get_settings()
    monkeypatch.setattr(s, "env", "production")
    monkeypatch.setattr(s, "jwt_secret", DEFAULT_JWT_SECRET)
    with pytest.raises(ConfiguracaoInsegura):
        create_app()


# --- sessão em cookie ------------------------------------------------------------------------------------------------


def _entrar(client, role="medico"):
    email = {"medico": "medico@housia.dev", "gestor": "gestor@housia.dev"}[role]
    return client.post("/auth/login", json={"email": email, "password": "housia123"})


def test_login_define_cookie_httponly_samesite_strict_e_csrf_legivel(client):
    r = _entrar(client)
    cookies = r.headers.get_list("set-cookie")
    sessao = next(c for c in cookies if c.startswith("housia_session="))
    csrf = next(c for c in cookies if c.startswith("housia_csrf="))
    assert "httponly" in sessao.lower() and "samesite=strict" in sessao.lower()
    assert "httponly" not in csrf.lower() and "samesite=strict" in csrf.lower()


def test_cookie_secure_em_producao(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "env", "production")
    sessao = next(c for c in _entrar(client).headers.get_list("set-cookie") if c.startswith("housia_session="))
    assert "secure" in sessao.lower()


def test_cookie_autentica_leitura_sem_authorization(client):
    _entrar(client)
    assert client.get("/auth/me").json()["email"] == "medico@housia.dev"


def test_escrita_por_cookie_exige_csrf(client):
    _entrar(client)
    corpo = {}
    r = client.post(ESCRITA, json=corpo)
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]
    r = client.post(ESCRITA, json=corpo, headers={"X-CSRF-Token": "errado"})
    assert r.status_code == 403
    token = client.cookies.get("housia_csrf")
    r = client.post(ESCRITA, json=corpo, headers={"X-CSRF-Token": token})
    assert r.status_code != 403  # passou pelo CSRF (a validação do corpo é outra camada)


def test_csrf_vazio_nunca_vale(client):
    _entrar(client)
    client.cookies.set("housia_csrf", "")
    assert client.post(ESCRITA, json={}, headers={"X-CSRF-Token": ""}).status_code == 403


def test_bearer_continua_valendo_sem_csrf_e_token_invalido_nao_cai_no_cookie(client):
    h = login(client, "medico")
    r = client.post(ESCRITA, json={}, headers=h)
    assert r.status_code != 403
    r = client.get("/auth/me", headers={"Authorization": "Bearer lixo"})
    assert r.status_code == 401


def test_sem_credencial_401(client):
    assert client.get("/auth/me").status_code == 401
    assert client.post(ESCRITA, json={}).status_code == 401


def test_logout_apaga_a_sessao(client):
    _entrar(client)
    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/me").status_code == 401


def test_cookie_de_outro_usuario_adulterado_e_recusado(client):
    _entrar(client)
    client.cookies.set("housia_session", "eyJ.adulterado.x")
    assert client.get("/auth/me").status_code == 401


# --- checklist ---------------------------------------------------------------------------------------------------


def _por_id(s=None):
    return {i["id"]: i for i in checklist(s)}


def test_checklist_reflete_a_configuracao_em_vigor(client, monkeypatch):
    s = get_settings()
    base = _por_id()
    assert base["ia"]["estado"] == "ok" and "mock" in base["ia"]["texto"]
    monkeypatch.setattr(s, "llm_mode", "real")
    monkeypatch.setattr(s, "llm_model", "modelo-teste")
    monkeypatch.setattr(s, "llm_region", "sa-east-1")
    real = _por_id()["ia"]
    assert real["estado"] == "atencao" and "modelo-teste" in real["texto"] and "sa-east-1" in real["texto"]
    monkeypatch.setattr(s, "llm_region", "")
    assert "não informada" in _por_id()["ia"]["texto"]
    monkeypatch.setattr(s, "ehr_writeback", False)
    assert _por_id()["prontuario"]["estado"] == "info"
    monkeypatch.setattr(s, "ehr_writeback", True)
    assert _por_id()["prontuario"]["estado"] == "ok"
    monkeypatch.setattr(s, "transcript_retention_days", "30")
    assert _por_id()["retencao"]["estado"] == "ok" and "30 dia(s)" in _por_id()["retencao"]["texto"]
    monkeypatch.setattr(s, "transcript_retention_days", "")
    assert _por_id()["retencao"]["estado"] == "atencao"


def test_checklist_transcricao_e_criptografia(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "stt_url", "http://stt.interno/v1/audio/transcriptions")
    monkeypatch.setattr(s, "allow_browser_stt", False)
    assert "servidor" in _por_id()["transcricao"]["texto"] and _por_id()["transcricao"]["estado"] == "ok"
    monkeypatch.setattr(s, "allow_browser_stt", True)
    assert _por_id()["transcricao"]["estado"] == "atencao"
    monkeypatch.setattr(s, "data_encryption_key", "")
    assert _por_id()["criptografia"]["estado"] == "atencao"


def test_checklist_seguranca_acusa_valores_de_demonstracao(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "jwt_secret", DEFAULT_JWT_SECRET)
    assert _por_id()["seguranca"]["estado"] == "atencao"
    monkeypatch.setattr(s, "jwt_secret", "y" * 40)
    monkeypatch.setattr(s, "llm_mode", "real")
    assert _por_id()["seguranca"]["estado"] == "ok"


def test_ambiente_nao_vaza_segredos(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "anthropic_api_key", "chave-secreta-de-teste")
    monkeypatch.setattr(s, "jwt_secret", "segredo-jwt-de-teste-com-mais-de-32-chars")
    assert "chave-secreta-de-teste" not in str(ambiente()) and "segredo-jwt-de-teste" not in str(ambiente())


def test_consult_config_traz_ambiente_para_o_gestor(client):
    h = login(client, "gestor")
    amb = client.get("/consult/config", headers=h).json()["ambiente"]
    assert amb["env"] == "development" and amb["producao"] is False
    assert {i["id"] for i in amb["checklist"]} >= {"transcricao", "ia", "prontuario", "retencao", "criptografia"}
    assert client.get("/health").json()["env"] == "development"


# --- compose de produção e Helm ----------------------------------------------------------------------------------


def test_compose_de_producao_nao_expoe_8080_nem_5432():
    comp = yaml.safe_load((RAIZ / "docker-compose.prod.yml").read_text())
    for nome in ("db", "fhir", "api", "web"):
        assert "ports" not in comp["services"][nome], nome
    publicas = [p for s in comp["services"].values() for p in s.get("ports", [])]
    assert sorted(publicas) == ["443:443", "80:80"]
    assert not any("8080" in str(p) or "5432" in str(p) for p in publicas)
    assert comp["services"]["api"]["environment"]["ENV"] == "production"
    for nome, s in comp["services"].items():
        assert "healthcheck" in s or nome in ("api", "web"), nome  # api e web têm HEALTHCHECK no Dockerfile
    bruto = (RAIZ / "docker-compose.prod.yml").read_text()
    assert "housia:housia@" not in bruto and "POSTGRES_PASSWORD: housia" not in bruto
    assert "JWT_SECRET: ${JWT_SECRET:?" in bruto and "POSTGRES_PASSWORD:?" in bruto


def test_caddyfile_encaminha_api_e_web():
    c = (RAIZ / "deploy/caddy/Caddyfile").read_text()
    assert "reverse_proxy api:8000" in c and "reverse_proxy web:3000" in c and "handle_path /api/*" in c


def test_chart_helm_coerente_com_o_compose():
    chart = RAIZ / "deploy/helm/housia"
    assert yaml.safe_load((chart / "Chart.yaml").read_text())["name"] == "housia"
    vals = yaml.safe_load((chart / "values.yaml").read_text())
    assert vals["secrets"]["jwtSecret"] == "" and vals["secrets"]["postgresPassword"] == ""
    texto = "\n".join(p.read_text() for p in (chart / "templates").iterdir())
    assert "required" in texto and "value: production" in texto
    for svc in ("db", "fhir"):  # só ClusterIP: nada de NodePort/LoadBalancer
        assert f"{{{{ .Release.Name }}}}-{svc}" in texto
    assert not re.search(r"type:\s*(NodePort|LoadBalancer)", texto)
    assert texto.count("{{") == texto.count("}}")
    ci = (RAIZ / ".github/workflows/ci.yml").read_text()
    assert "helm lint deploy/helm/housia" in ci


def test_env_com_grafia_invalida_falha_fechado():
    with pytest.raises(ValueError):
        Settings(_env_file=None, env="Production")


def test_me_reemite_o_csrf_perdido(client):
    _entrar(client)
    client.cookies.delete("housia_csrf")
    assert client.post(ESCRITA, json={}).status_code == 401 or client.post(ESCRITA, json={}).status_code == 403
    assert client.get("/auth/me").status_code == 200
    assert client.cookies.get("housia_csrf")
    assert client.post(ESCRITA, json={}, headers={"X-CSRF-Token": client.cookies.get("housia_csrf")}).status_code != 403


@pytest.mark.parametrize(
    ("metodo", "rota"),
    [
        ("put", "/consultations/x/queixa-principal"),
        ("patch", "/consultations/x/segments/1"),
        ("delete", "/patients/x/consultation-data"),
    ],
)
def test_metodos_de_escrita_por_cookie_exigem_csrf(client, metodo, rota):
    _entrar(client)
    assert getattr(client, metodo)(rota).status_code == 403
