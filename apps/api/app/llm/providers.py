"""Provedores de LLM substituíveis (#54).

- `anthropic` (padrão): implementado em app/llm/client.py (mantém o SDK e os testes existentes).
- `openai_compat`: qualquer servidor com POST {LLM_BASE_URL}/chat/completions (vLLM, Ollama, LM Studio, gateways).
  Saída estruturada: tenta `json_schema`; se o servidor recusar, cai em `json_object` e depois só em instrução no
  prompt. Em todos os casos quem chama (generate_structured) valida com Pydantic e nunca aceita saída sem validação.
- mock: `LLM_MODE=mock` (determinístico, não sai da máquina).

A validação da configuração roda na subida (`problemas_config`) e a de destino roda a cada chamada
(`checar_destino`), para a URL base não virar caminho para a rede interna (SSRF) quando o provedor é externo.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.llm.errors import LLMError

PROVEDORES = ("anthropic", "openai_compat")
MODO_DEFAULT_MODELO = "claude-opus-5-5"


@dataclass(frozen=True)
class ProviderInfo:
    modo: str  # real | mock
    provedor: str | None
    modelo: str | None
    regiao: str | None
    externo: bool  # o texto sai da rede da instituição
    local: bool

    def como_dict(self) -> dict:
        return {
            "modo": self.modo,
            "provedor": self.provedor,
            "modelo": self.modelo,
            "regiao": self.regiao,
            "externo": self.externo,
            "local": self.local,
        }


class LLMProvider(Protocol):
    """Contrato mínimo: devolve o TEXTO da resposta (JSON). A validação é sempre de quem chama."""

    name: str

    def complete(self, system: str, messages: list[dict], schema: dict, effort: str | None = None) -> str: ...


def info(s: Settings) -> ProviderInfo:
    if s.llm_mode != "real":
        return ProviderInfo("mock", None, None, None, externo=False, local=True)
    if s.llm_provider == "openai_compat":
        local = s.llm_on_premise
        return ProviderInfo(
            "real", "openai_compat", s.llm_model, (s.llm_region or "não informada"), externo=not local, local=local
        )
    return ProviderInfo("real", "Anthropic", s.llm_model, (s.llm_region or "não informada"), externo=True, local=False)


def regioes_permitidas(s: Settings) -> list[str]:
    return [r.strip().lower() for r in s.llm_allowed_regions.split(",") if r.strip()]


def checar_regiao(s: Settings) -> None:
    permitidas = regioes_permitidas(s)
    if permitidas and (s.llm_region or "").strip().lower() not in permitidas:
        raise LLMError(
            f"Região do provedor de IA ({s.llm_region or 'não informada'}) fora da política "
            f"LLM_ALLOWED_REGIONS ({', '.join(permitidas)}). A chamada foi bloqueada."
        )


# ---------- destino (SSRF) ----------
_METADADOS = {"169.254.169.254", "fd00:ec2::254", "100.100.100.200"}


def _ip(texto: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(texto.strip("[]"))
    except ValueError:
        return None


def _interno(ip: ipaddress._BaseAddress) -> bool:
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))
    )


def _analisar_url(url: str) -> tuple[str | None, str | None]:
    """(host, erro). Valida só o que se sabe sem rede."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - levanta ValueError se a porta for inválida
    except ValueError:
        return None, "LLM_BASE_URL não é uma URL válida"
    if parts.scheme not in ("http", "https") or not host:
        return None, "LLM_BASE_URL precisa começar com http:// ou https://"
    if parts.username or parts.password:
        return None, "LLM_BASE_URL não pode levar usuário e senha (use LLM_API_KEY)"
    return host.lower(), None


def checar_url_estatica(url: str, on_premise: bool) -> str | None:
    host, erro = _analisar_url(url)
    if erro:
        return erro
    assert host is not None
    ip = _ip(host)
    if host in _METADADOS or (ip is not None and str(ip) in _METADADOS):
        return "LLM_BASE_URL aponta para um serviço de metadados de nuvem (bloqueado)"
    if on_premise:
        if ip is not None and not _interno(ip):
            return "LLM_ON_PREMISE=true, mas LLM_BASE_URL é um IP público (o servidor não é local)"
        return None
    # provedor externo: só https e nunca rede interna
    if urlsplit(url).scheme != "https":
        return "provedor externo exige https:// em LLM_BASE_URL (para http use LLM_ON_PREMISE=true em servidor local)"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return "LLM_BASE_URL aponta para nome interno; com provedor externo isso é bloqueado (SSRF)"
    if ip is not None and _interno(ip):
        return "LLM_BASE_URL aponta para endereço interno; com provedor externo isso é bloqueado (SSRF)"
    return None


def checar_destino(url: str, on_premise: bool) -> None:
    """Na hora da chamada: estática + resolve o nome (DNS para rede interna, ou para fora no local)."""
    erro = checar_url_estatica(url, on_premise)
    if erro:
        raise LLMError(f"Destino da IA recusado: {erro}.")
    host, _ = _analisar_url(url)
    if host is None or _ip(host) is not None:
        return
    try:
        resolvidos = {i[4][0] for i in socket.getaddrinfo(host, None)}
    except OSError:
        return  # sem DNS: a própria requisição falha com erro de conexão
    for r in resolvidos:
        ip = _ip(r.split("%")[0])
        if ip is None:
            continue
        if str(ip) in _METADADOS:
            raise LLMError("Destino da IA recusado: o nome resolve para metadados de nuvem.")
        if not on_premise and _interno(ip):
            raise LLMError("Destino da IA recusado: o nome resolve para endereço interno (provedor externo).")
        if on_premise and not _interno(ip):
            raise LLMError("Destino da IA recusado: LLM_ON_PREMISE=true, mas o nome resolve para IP público.")


def problemas_config(s: Settings) -> list[str]:
    """Erros de configuração do provedor de IA, com mensagem clara. Vazia no modo mock."""
    if s.llm_mode != "real":
        return []
    out: list[str] = []
    permitidas = regioes_permitidas(s)
    if permitidas and (s.llm_region or "").strip().lower() not in permitidas:
        out.append(f"LLM_REGION ({s.llm_region or 'vazia'}) não está em LLM_ALLOWED_REGIONS ({', '.join(permitidas)})")
    if s.llm_provider == "openai_compat":
        if not s.llm_base_url.strip():
            out.append("LLM_PROVIDER=openai_compat exige LLM_BASE_URL (ex.: http://vllm.interno:8000/v1)")
        else:
            erro = checar_url_estatica(s.llm_base_url.strip(), s.llm_on_premise)
            if erro:
                out.append(erro)
        if "llm_model" not in s.model_fields_set or s.llm_model == MODO_DEFAULT_MODELO:
            out.append("LLM_PROVIDER=openai_compat exige LLM_MODEL com o nome do modelo do servidor")
        if not s.llm_on_premise and not s.llm_api_key.strip():
            out.append("provedor externo openai_compat exige LLM_API_KEY")
    else:
        if s.llm_base_url.strip():
            erro = checar_url_estatica(s.llm_base_url.strip(), False)
            if erro:
                out.append(erro)
        if s.llm_on_premise:
            out.append("LLM_ON_PREMISE só vale com LLM_PROVIDER=openai_compat (o Anthropic é sempre externo)")
    return out


# ---------- openai_compat ----------
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def extrair_json(texto: str) -> str:
    """Tira cerca de código e texto em volta do primeiro objeto JSON (modelos locais costumam enfeitar)."""
    m = _FENCE.match(texto)
    if m:
        texto = m.group(1)
    texto = texto.strip()
    if texto.startswith("{"):
        return texto
    i, j = texto.find("{"), texto.rfind("}")
    return texto[i : j + 1] if 0 <= i < j else texto


class _FormatoNaoSuportado(Exception):
    pass


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, s: Settings, transport: httpx.BaseTransport | None = None):
        self.s = s
        self.transport = transport
        self._modos = {
            "auto": ["json_schema", "json_object", "prompt"],
            "json_schema": ["json_schema"],
            "json_object": ["json_object"],
            "prompt": ["prompt"],
        }[s.llm_structured]
        self.modo_usado: str | None = None

    def complete(self, system: str, messages: list[dict], schema: dict, effort: str | None = None) -> str:
        checar_destino(self.s.llm_base_url.strip(), self.s.llm_on_premise)
        ultimo: Exception | None = None
        for modo in self._modos:
            try:
                texto = self._post(modo, system, messages, schema)
                self.modo_usado = modo
                return extrair_json(texto)
            except _FormatoNaoSuportado as exc:
                ultimo = exc
        raise LLMError("O servidor de IA não aceitou nenhum formato de saída estruturada.") from ultimo

    def _post(self, modo: str, system: str, messages: list[dict], schema: dict) -> str:
        s = self.s
        instrucao = system
        corpo: dict[str, Any] = {"model": s.llm_model, "max_tokens": s.llm_max_tokens, "stream": False}
        if modo == "json_schema":
            corpo["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "saida", "schema": schema, "strict": True},
            }
        else:
            if modo == "json_object":
                corpo["response_format"] = {"type": "json_object"}
            instrucao += (
                "\n\nResponda SOMENTE com um objeto JSON válido, sem texto fora dele, que siga este JSON Schema:\n"
                + json.dumps(schema, ensure_ascii=False)
            )
        corpo["messages"] = [{"role": "system", "content": instrucao}, *messages]
        headers = {"Content-Type": "application/json"}
        if s.llm_api_key.strip():
            headers["Authorization"] = f"Bearer {s.llm_api_key.strip()}"
        url = s.llm_base_url.strip().rstrip("/") + "/chat/completions"
        try:
            with httpx.Client(transport=self.transport, timeout=s.llm_timeout_s, follow_redirects=False) as http:
                resp = http.post(url, json=corpo, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError("A IA demorou demais para responder. Tente novamente.") from exc
        except httpx.HTTPError as exc:
            raise LLMError("Sem conexão com o servidor de IA (LLM_BASE_URL).") from exc

        if resp.status_code in (400, 404, 415, 422):
            if modo != self._modos[-1]:
                raise _FormatoNaoSuportado(f"HTTP {resp.status_code} em {modo}")
            raise LLMError(
                f"A requisição ao servidor de IA foi rejeitada ({resp.status_code}). Verifique LLM_MODEL, "
                "LLM_BASE_URL e LLM_STRUCTURED."
            )
        if resp.status_code in (401, 403):
            raise LLMError("Chave do servidor de IA inválida ou ausente (LLM_API_KEY).")
        if resp.status_code == 429:
            raise LLMError("Limite de uso do servidor de IA atingido. Tente novamente em instantes.")
        if resp.status_code >= 300:
            raise LLMError(f"Erro do servidor de IA ({resp.status_code}). Tente novamente.")
        try:
            dados = resp.json()
            escolha = dados["choices"][0]
            msg = escolha.get("message") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMError("O servidor de IA devolveu uma resposta fora do padrão.") from exc
        if msg.get("refusal") or escolha.get("finish_reason") == "content_filter":
            raise LLMError("A IA não gerou este conteúdo (recusa de segurança). Use os dados brutos da timeline.")
        if escolha.get("finish_reason") == "length":
            raise LLMError("A resposta da IA foi truncada (limite de tokens).")
        conteudo = msg.get("content")
        if isinstance(conteudo, list):
            conteudo = "".join(p.get("text", "") for p in conteudo if isinstance(p, dict))
        if not isinstance(conteudo, str) or not conteudo.strip():
            raise LLMError("A IA retornou uma resposta vazia.")
        return conteudo
