"""#54 — provedor de LLM substituível: openai_compat contra servidor simulado (httpx.MockTransport), política de
região, SSRF, configuração na subida e recibo no audit_log. Nenhuma rede real."""

import json
import socket

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.consult.ask import AskOutput
from app.db import session as db_session
from app.db.models import AuditLog
from app.llm import client as llm_client
from app.llm import providers, recibo
from app.llm.client import LLMError, generate_structured
from app.prontidao import ConfiguracaoInsegura, exigir_producao_segura

BASE = "https://llm.exemplo.test/v1"
FALA = "minha vizinha Cláudia mora na Rua das Flores e toma losartana. CPF 123.456.789-09"
VARS = {"tipo": "consulta", "especialidade": "clínica", "pergunta": FALA, "prontuario": "{}", "regras": "[]"}


def ok_json(texto="resposta"):
    return json.dumps({"frases": [{"texto": texto, "refs": ["Fala/1"]}]})


def completion(content, finish="stop", **msg):
    return {"choices": [{"finish_reason": finish, "message": {"content": content, **msg}}]}


class Servidor:
    """Servidor OpenAI-compatível falso: guarda o que recebeu e responde o que o teste mandar."""

    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.pedidos: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        r = self.respostas.pop(0) if len(self.respostas) > 1 else self.respostas[0]
        if isinstance(r, Exception):
            raise r
        if isinstance(r, httpx.Response):
            return r
        return httpx.Response(200, json=r)

    def corpo(self, i=0) -> dict:
        return json.loads(self.pedidos[i].content)


@pytest.fixture
def cfg(monkeypatch):
    s = get_settings()
    base = {
        "llm_mode": "real",
        "llm_provider": "openai_compat",
        "llm_base_url": BASE,
        "llm_api_key": "chave-sintetica",
        "llm_model": "modelo-local-7b",
        "llm_region": "br-sp",
        "llm_on_premise": False,
        "llm_allowed_regions": "",
        "llm_structured": "auto",
        "llm_timeout_s": 5.0,
    }
    for k, v in base.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(llm_client, "_http_transport", None)
    monkeypatch.setattr(llm_client, "_client", None)
    return s


def servir(monkeypatch, *respostas) -> Servidor:
    srv = Servidor(*respostas)
    monkeypatch.setattr(llm_client, "_http_transport", httpx.MockTransport(srv))
    return srv


def gerar():
    return generate_structured("consult_ask", dict(VARS), AskOutput, mock=lambda v: {})


# ---------- funcionamento ----------
def test_openai_compat_funciona_contra_servidor_simulado(cfg, monkeypatch):
    srv = servir(monkeypatch, completion(ok_json("[NOME_1] mora em [ENDERECO_1]")))
    res = gerar()
    # a resposta volta reidentificada pelo mapa local
    assert res.output.frases[0].texto == "Cláudia mora em Rua das Flores"
    assert res.mode == "real" and res.provider == "openai_compat" and res.external is True
    corpo = srv.corpo()
    assert srv.pedidos[0].url == httpx.URL(BASE + "/chat/completions")
    assert srv.pedidos[0].headers["authorization"] == "Bearer chave-sintetica"
    assert corpo["model"] == "modelo-local-7b" and corpo["response_format"]["type"] == "json_schema"
    enviado = json.dumps(corpo, ensure_ascii=False)
    for vazou in ("Cláudia", "Flores", "123.456.789-09"):
        assert vazou not in enviado
    assert "[NOME_1]" in enviado and "[ENDERECO_1]" in enviado and "[CPF_1]" in enviado
    assert res.entities == {"estruturado": 0, "regex": 1, "regras_ner": 2}


def test_sem_suporte_a_json_schema_cai_em_json_object_e_depois_em_prompt(cfg, monkeypatch):
    srv = servir(monkeypatch, httpx.Response(400, json={"error": "response_format"}), completion(ok_json()))
    assert gerar().output.frases[0].texto == "resposta"
    assert [srv.corpo(i).get("response_format", {}).get("type") for i in range(2)] == ["json_schema", "json_object"]
    assert "JSON Schema" in srv.corpo(1)["messages"][0]["content"]

    srv = servir(
        monkeypatch, httpx.Response(422), httpx.Response(400), completion("```json\n" + ok_json("c") + "\n```")
    )
    assert gerar().output.frases[0].texto == "c"  # cerca de código removida, saída ainda validada
    assert "response_format" not in srv.corpo(2)


def test_todos_os_formatos_recusados_vira_erro_amigavel(cfg, monkeypatch):
    servir(monkeypatch, httpx.Response(400))
    with pytest.raises(LLMError, match="rejeitada"):
        gerar()


def test_json_invalido_ganha_um_reparo_e_depois_erro(cfg, monkeypatch):
    srv = servir(monkeypatch, completion("isto não é json"), completion(ok_json("ok")))
    res = gerar()
    assert res.repaired and len(srv.pedidos) == 2
    servir(monkeypatch, completion("{}"), completion('{"frases": "errado"}'))
    with pytest.raises(LLMError, match="formato inválido"):
        gerar()


def test_resposta_valida_mas_fora_do_schema_nunca_e_aceita(cfg, monkeypatch):
    servir(monkeypatch, completion(json.dumps({"frases": [{"texto": 1}], "extra": True})))
    with pytest.raises(LLMError):
        gerar()


@pytest.mark.parametrize(
    ("resposta", "trecho"),
    [
        (httpx.TimeoutException("lento"), "demorou demais"),
        (httpx.ConnectError("recusou"), "Sem conexão"),
        (httpx.Response(401), "LLM_API_KEY"),
        (httpx.Response(429), "Limite de uso"),
        (httpx.Response(503), "503"),
        (httpx.Response(200, text="<html>"), "fora do padrão"),
        (httpx.Response(200, json={"choices": []}), "fora do padrão"),
        (completion("", finish="stop"), "vazia"),
        (completion(ok_json(), finish="length"), "truncada"),
        (completion(None, finish="stop", refusal="não posso"), "recusa"),
        (completion(ok_json(), finish="content_filter"), "recusa"),
        (httpx.Response(302, headers={"location": "http://10.0.0.1/"}), "302"),
    ],
)
def test_erros_do_servidor_viram_mensagem_amigavel(cfg, monkeypatch, resposta, trecho):
    servir(monkeypatch, resposta)
    with pytest.raises(LLMError, match=trecho):
        gerar()


def test_token_forjado_pelo_provedor_nao_vaza_nem_quebra(cfg, monkeypatch):
    servir(monkeypatch, completion(ok_json('[NOME_9] [CPF_5] [nome 1] e "aspas"')))
    texto = gerar().output.frases[0].texto
    assert texto == '[dado omitido] [dado omitido] Cláudia e "aspas"'


# ---------- política de região, local e SSRF ----------
def test_regiao_nao_permitida_bloqueia_a_chamada(cfg, monkeypatch):
    srv = servir(monkeypatch, completion(ok_json()))
    monkeypatch.setattr(cfg, "llm_allowed_regions", "br-sp, br-rj")
    assert gerar().output.frases
    monkeypatch.setattr(cfg, "llm_region", "us-east")
    with pytest.raises(LLMError, match="LLM_ALLOWED_REGIONS"):
        gerar()
    assert len(srv.pedidos) == 1  # a segunda chamada nem saiu


def test_provedor_local_pula_a_saida_externa_mas_audita(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "llm_on_premise", True)
    monkeypatch.setattr(cfg, "llm_base_url", "http://vllm.interno:8000/v1")
    monkeypatch.setattr(cfg, "llm_api_key", "")
    srv = servir(monkeypatch, completion(ok_json()))
    res = gerar()
    assert res.external is False and "Cláudia" in json.dumps(srv.corpo(), ensure_ascii=False)
    assert "authorization" not in srv.pedidos[0].headers
    r = recibo.ultimo.get()
    assert r["externo"] is False and r["local"] is True and r["pseudonimizacao_ativa"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://llm.exemplo.test/v1",  # http com provedor externo
        "https://127.0.0.1:8000/v1",
        "https://localhost/v1",
        "https://10.1.2.3/v1",
        "https://192.168.0.9/v1",
        "https://[::1]/v1",
        "https://[::ffff:10.0.0.1]/v1",
        "https://169.254.169.254/latest/meta-data",
        "https://llm.interno/v1".replace("llm.interno", "api.internal"),
        "https://user:senha@llm.exemplo.test/v1",
        "ftp://llm.exemplo.test/v1",
        "file:///etc/passwd",
        "https:///sem-host",
    ],
)
def test_url_base_maliciosa_e_recusada_para_provedor_externo(cfg, monkeypatch, url):
    srv = servir(monkeypatch, completion(ok_json()))
    monkeypatch.setattr(cfg, "llm_base_url", url)
    assert providers.problemas_config(cfg)  # barrada já na configuração
    with pytest.raises(LLMError, match="Destino da IA recusado"):
        gerar()
    assert srv.pedidos == []


def test_dns_que_aponta_para_rede_interna_e_barrado_na_chamada(cfg, monkeypatch):
    srv = servir(monkeypatch, completion(ok_json()))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))])
    with pytest.raises(LLMError, match="endereço interno"):
        gerar()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 0))])
    with pytest.raises(LLMError, match="metadados"):
        gerar()
    assert srv.pedidos == []


def test_local_declarado_que_resolve_para_ip_publico_e_barrado(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "llm_on_premise", True)
    monkeypatch.setattr(cfg, "llm_base_url", "http://modelo.interno:8000/v1")
    srv = servir(monkeypatch, completion(ok_json()))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 0))])
    with pytest.raises(LLMError, match="IP público"):
        gerar()
    assert srv.pedidos == []
    assert providers.problemas_config(cfg) == []
    monkeypatch.setattr(cfg, "llm_base_url", "http://8.8.8.8:8000/v1")
    assert any("IP público" in p for p in providers.problemas_config(cfg))


# ---------- configuração na subida ----------
def test_config_na_subida_tem_mensagens_claras(cfg, monkeypatch):
    assert providers.problemas_config(cfg) == []
    monkeypatch.setattr(cfg, "llm_api_key", "")
    assert any("LLM_API_KEY" in p for p in providers.problemas_config(cfg))  # chave ausente, provedor externo
    monkeypatch.setattr(cfg, "llm_base_url", "")
    assert any("LLM_BASE_URL" in p for p in providers.problemas_config(cfg))
    monkeypatch.setattr(cfg, "llm_model", "claude-opus-5-5")
    assert any("LLM_MODEL" in p for p in providers.problemas_config(cfg))
    monkeypatch.setattr(cfg, "llm_allowed_regions", "eu")
    assert any("LLM_ALLOWED_REGIONS" in p for p in providers.problemas_config(cfg))
    with pytest.raises(ConfiguracaoInsegura, match="provedor de IA inválida"):
        exigir_producao_segura(cfg)


def test_padrao_anthropic_e_mock_nao_exigem_nada_novo(monkeypatch):
    s = get_settings()
    assert s.llm_provider == "anthropic" and s.llm_mode == "mock"
    assert providers.problemas_config(s) == []
    monkeypatch.setattr(s, "llm_mode", "real")
    assert providers.problemas_config(s) == []  # sem LLM_BASE_URL nem chave nova
    monkeypatch.setattr(s, "llm_on_premise", True)
    assert any("openai_compat" in p for p in providers.problemas_config(s))


def test_mock_nunca_chama_provedor(monkeypatch):
    s = get_settings()
    assert s.llm_mode == "mock"
    monkeypatch.setattr(llm_client, "_call", lambda *a, **k: pytest.fail("não deve chamar"))
    res = generate_structured("consult_ask", dict(VARS), AskOutput, mock=lambda v: {"frases": []})
    assert res.mode == "mock" and res.provider == ""


# ---------- Anthropic (padrão) também passa pela barreira ----------
def test_anthropic_tambem_recebe_o_texto_pseudonimizado_em_camadas(monkeypatch):
    from types import SimpleNamespace

    s = get_settings()
    monkeypatch.setattr(s, "llm_mode", "real")
    chamadas = []

    def create(**kw):
        chamadas.append(kw)
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=ok_json("[NOME_1] ok"))]
        )

    fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm_client, "_client", fake)
    try:
        res = gerar()
    finally:
        monkeypatch.setattr(llm_client, "_client", None)
    enviado = json.dumps(chamadas[0], ensure_ascii=False, default=str)
    assert "Cláudia" not in enviado and "Flores" not in enviado
    assert res.output.frases[0].texto == "Cláudia ok" and res.provider == "Anthropic"


# ---------- recibo no audit_log ----------
def test_recibo_da_chamada_vai_para_o_audit_log_sem_texto(client, cfg, monkeypatch):
    servir(monkeypatch, completion(ok_json()))
    recibo.iniciar()
    gerar()
    with db_session.SessionLocal() as db:
        db.info["org_id"] = 1
        recibo.descarregar(db)
        linhas = db.scalars(select(AuditLog).where(AuditLog.action == "llm.chamada")).all()
    assert len(linhas) == 1
    p = linhas[0].payload_json
    assert p["provedor"] == "openai_compat" and p["modelo"] == "modelo-local-7b" and p["regiao"] == "br-sp"
    assert p["entidades"] == {"estruturado": 0, "regex": 1, "regras_ner": 2} and p["resultado"] == "ok"
    bruto = json.dumps(p, ensure_ascii=False)
    for pii in ("Cláudia", "Flores", "123.456", "losartana"):
        assert pii not in bruto


def test_recibo_registra_tambem_a_chamada_que_falhou(cfg, monkeypatch):
    servir(monkeypatch, httpx.Response(500))
    recibo.iniciar()
    with pytest.raises(LLMError):
        gerar()
    r = recibo.ultimo.get()
    assert r["resultado"] == "erro" and r["entidades"]["regras_ner"] == 2


def test_dossie_mostra_provedor_ativo_e_fluxo_sem_afirmar_conformidade(client, auth, cfg):
    d = client.get("/compliance/dossie", headers=auth("gestor")).json()["calculado"]["provedor_de_llm"]
    assert d["provedor"] == "openai_compat" and d["modelo"] == "modelo-local-7b" and d["regiao"] == "br-sp"
    assert d["externo"] is True and "não cobre tudo" in d["pseudonimizacao"]
    assert "conforme" not in d["pseudonimizacao"].lower().replace("conformidade", "")
    md = client.get("/compliance/dossie", params={"formato": "md"}, headers=auth("gestor")).text
    assert "Fluxo de dados da IA" in md and "openai_compat" in md


# ---------- verificação #54 ----------
@pytest.mark.parametrize(
    "url",
    [
        "https://127.1/v1",  # IPv4 abreviado
        "https://2130706433/v1",  # decimal
        "https://0x7f.0.0.1/v1",  # hexadecimal
        "https://017700000001/v1",  # octal
        "https://１２７.0.0.1/v1",  # dígitos largos (NFKC)
        "https://localhost./v1",  # ponto final
        "https://intranet/v1",  # nome de rótulo único
        "https://kubernetes.default.svc/v1",
        "https://[64:ff9b::7f00:1]/v1",
        "https://100.100.100.200/v1",
    ],
)
def test_verificacao_54_hosts_ofuscados_sao_barrados_na_configuracao(cfg, url):
    monkey = providers.checar_url_estatica(url, False)
    assert monkey, url


def test_verificacao_54_host_numerico_curto_nao_e_local_publico_no_on_premise():
    assert providers.checar_url_estatica("http://0x08080808:8000/v1", True)  # 8.8.8.8 disfarçado
    assert providers.checar_url_estatica("http://127.1:8000/v1", True) is None


def test_verificacao_54_conexao_fixa_no_ip_validado_contra_dns_rebinding(cfg, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.4.4", 0))])
    ips = providers.checar_destino(BASE, False)
    assert ips == ["8.8.4.4"]
    url, extra, ext = providers.fixar_destino(BASE + "/chat/completions", ips)
    assert url == "https://8.8.4.4/v1/chat/completions"
    assert extra == {"Host": "llm.exemplo.test"} and ext == {"sni_hostname": "llm.exemplo.test"}
    v6 = providers.fixar_destino("https://llm.exemplo.test:8443/v1/x", ["2606:4700::1111"])
    assert v6[0] == "https://[2606:4700::1111]:8443/v1/x" and v6[1] == {"Host": "llm.exemplo.test:8443"}
    # host que já é IP não muda
    assert providers.fixar_destino("http://10.0.0.5:8000/v1", ["10.0.0.5"]) == ("http://10.0.0.5:8000/v1", {}, {})


def test_verificacao_54_redirect_nao_e_seguido_e_chave_nao_vai_para_o_erro(cfg, monkeypatch):
    srv = servir(monkeypatch, httpx.Response(302, headers={"location": "http://10.0.0.5/steal"}))
    with pytest.raises(LLMError) as exc:
        gerar()
    assert len(srv.pedidos) == 1 and "chave-sintetica" not in str(exc.value)
    assert "10.0.0.5" not in str(exc.value)


def test_verificacao_54_resposta_aninhada_e_reidentificada_e_valida_no_schema(cfg, monkeypatch):
    corpo = json.dumps(
        {
            "frases": [
                {"texto": '[NOME_1] mora em [ENDERECO_1]; "aspas" e\nquebra', "refs": ["Fala/1", "Observation/9"]},
                {"texto": "ligar para [TELEFONE_1] ou falar com [NOME_1]", "refs": ["Fala/2"]},
                {"texto": "[NOME_42] e [cpf 9] viram omitido", "refs": []},
            ]
        }
    )
    srv = servir(monkeypatch, completion(corpo))
    vars_ = dict(VARS, pergunta=FALA + " Ligue 11 91234-5678")
    res = generate_structured("consult_ask", vars_, AskOutput, mock=lambda v: {})
    f = res.output.frases
    assert f[0].texto.startswith("Cláudia mora em Rua das Flores") and "\nquebra" in f[0].texto
    assert f[0].refs == ["Fala/1", "Observation/9"] and f[1].refs == ["Fala/2"]  # citações intactas
    assert "11 91234-5678" in f[1].texto and "Cláudia" in f[1].texto
    assert f[2].texto.count("[dado omitido]") == 2
    enviado = json.dumps(srv.corpo(), ensure_ascii=False)
    assert "91234" not in enviado and "Cláudia" not in enviado


def test_verificacao_54_log_e_mensagem_de_reparo_nao_levam_pii(cfg, monkeypatch, caplog):
    # o 1º retorno falha no schema com o valor (já reidentificado) no "input" do erro do Pydantic
    ruim = json.dumps({"frases": [{"texto": ["[NOME_1] mora em [ENDERECO_1]"], "refs": []}]})
    srv = servir(monkeypatch, completion(ruim), completion(ok_json()))
    with caplog.at_level("DEBUG"):
        gerar()
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "Cláudia" not in log and "Flores" not in log
    reparo = json.dumps(srv.corpo(1), ensure_ascii=False)
    assert "Cláudia" not in reparo and "Flores" not in reparo


def test_verificacao_54_chave_da_api_nao_aparece_no_recibo_nem_no_log(cfg, monkeypatch, caplog):
    servir(monkeypatch, httpx.Response(500))
    recibo.iniciar()
    with caplog.at_level("DEBUG"), pytest.raises(LLMError):
        gerar()
    tudo = json.dumps(recibo.ultimo.get(), ensure_ascii=False) + "\n".join(r.getMessage() for r in caplog.records)
    assert "chave-sintetica" not in tudo and "Cláudia" not in tudo


def test_verificacao_54_recibos_nao_vazam_entre_requisicoes_concorrentes(cfg, monkeypatch):
    import asyncio

    servir(monkeypatch, completion(ok_json()))

    async def requisicao(i: int):
        recibo.iniciar()
        fila = recibo._pendentes.get()
        await asyncio.sleep(0.01 * (3 - i))
        await asyncio.to_thread(gerar)  # to_thread copia o contexto: a thread grava na fila DESTA requisição
        await asyncio.sleep(0.01)
        return fila, recibo._pendentes.get()

    async def main():
        return await asyncio.gather(*(asyncio.create_task(requisicao(i)) for i in range(3)))

    resultados = asyncio.run(main())
    filas = [id(f) for f, _ in resultados]
    assert len(set(filas)) == 3
    for fila, atual in resultados:
        assert fila is atual and len(fila) == 1


def test_verificacao_54_pool_de_threads_leva_o_contexto_do_recibo(cfg, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from app.consult.ai import submit_em_contexto

    servir(monkeypatch, completion(ok_json()))
    recibo.iniciar()
    fila = recibo._pendentes.get()
    with ThreadPoolExecutor(max_workers=2) as pool:
        [f.result() for f in [submit_em_contexto(pool, gerar), submit_em_contexto(pool, gerar)]]
    assert len(fila) == 2
