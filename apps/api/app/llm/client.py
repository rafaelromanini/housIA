"""Wrapper do Claude para gerações estruturadas.

- Prompts versionados em app/llm/prompts/*.md (cabeçalho `version:`); a versão vai junto de cada resultado.
- Saída sempre validada com Pydantic; JSON inválido ganha 1 tentativa de reparo e depois vira erro amigável.
- LLM_MODE=mock usa geradores determinísticos (fixtures) — roda sem chave, usado no CI.
- O texto enviado já chega pseudonimizado (app/llm/pseudonymize.py); com LOG_PROMPTS=false nada é logado.
- Provedor substituível (#54): LLM_PROVIDER=anthropic (padrão) ou openai_compat (app/llm/providers.py). Para provedor
  externo, uma segunda barreira em camadas (app/llm/camadas.py) troca nomes e endereços ditos por tokens locais e
  reidentifica a resposta; cada chamada deixa um recibo (app/llm/recibo.py) sem texto clínico.
"""

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.llm import providers, recibo
from app.llm.camadas import Escudo
from app.llm.errors import LLMError
from app.llm.schema import strict_schema

logger = logging.getLogger("housia.llm")
PROMPTS_DIR = Path(__file__).parent / "prompts"

# Recusa de classificador de segurança: o servidor tenta outro modelo automaticamente.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    system: str
    user: str


@dataclass
class LLMResult:
    output: BaseModel
    model: str
    prompt_version: str
    mode: str
    repaired: bool = False
    provider: str = ""
    region: str = ""
    external: bool = False
    entities: dict | None = None  # contagem por camada de pseudonimização (sem PII)


@lru_cache
def load_prompt(name: str) -> Prompt:
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"Prompt {name} sem cabeçalho de versão")
    _, header, body = text.split("---", 2)
    meta = dict(line.split(":", 1) for line in header.strip().splitlines() if ":" in line)
    version = meta.get("version", "").strip()
    if not version:
        raise ValueError(f"Prompt {name} sem `version:`")
    system, _, user = body.partition("# user")
    system = system.replace("# system", "", 1).strip()
    return Prompt(name=name, version=version, system=system, user=user.strip())


_client_lock = threading.Lock()
_client: anthropic.Anthropic | None = None


def _anthropic() -> anthropic.Anthropic:
    global _client
    with _client_lock:
        if _client is None:
            settings = get_settings()
            # Timeout configurável e 1 retry (o SDK repete 408/409/429/5xx e falhas de conexão)
            extra = {"base_url": settings.llm_base_url.strip()} if settings.llm_base_url.strip() else {}
            _client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key or None,
                timeout=settings.llm_timeout_s,
                max_retries=1,
                **extra,
            )
        return _client


def _call(system: str, messages: list[dict], schema: dict, effort: str | None = None) -> str:
    """Despacha para o provedor configurado (LLM_PROVIDER)."""
    settings = get_settings()
    if settings.llm_provider == "openai_compat":
        return providers.OpenAICompatProvider(settings, transport=_http_transport).complete(
            system, messages, schema, effort
        )
    if settings.llm_base_url.strip():
        providers.checar_destino(settings.llm_base_url.strip(), False)
    return _call_anthropic(system, messages, schema, effort)


# Transporte HTTP injetável (testes usam httpx.MockTransport; produção deixa None)
_http_transport = None


def _call_anthropic(system: str, messages: list[dict], schema: dict, effort: str | None = None) -> str:
    settings = get_settings()
    try:
        response = _anthropic().beta.messages.create(
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            system=system,
            messages=messages,
            output_config={
                "effort": effort or settings.llm_effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise LLMError("Chave da API do Claude inválida ou ausente (ANTHROPIC_API_KEY).") from exc
    except anthropic.RateLimitError as exc:
        raise LLMError("Limite de uso da API do Claude atingido. Tente novamente em instantes.") from exc
    except anthropic.APITimeoutError as exc:
        raise LLMError("A IA demorou demais para responder. Tente novamente.") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError("Sem conexão com a API do Claude.") from exc
    except anthropic.BadRequestError as exc:
        logger.error("Requisição rejeitada pela API do Claude: %s", exc.message)
        raise LLMError("A requisição à IA foi rejeitada. Verifique LLM_MODEL e a configuração.") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"Erro da API do Claude ({exc.status_code}). Tente novamente.") from exc

    if response.stop_reason == "refusal":
        raise LLMError("A IA não gerou este conteúdo (recusa de segurança). Use os dados brutos da timeline.")
    if response.stop_reason == "max_tokens":
        raise LLMError("A resposta da IA foi truncada (limite de tokens).")
    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise LLMError("A IA retornou uma resposta vazia.")
    return text


def generate_structured[T: BaseModel](
    prompt_name: str,
    variables: dict[str, Any],
    schema: type[T],
    mock: Callable[[dict[str, Any]], dict[str, Any]],
    effort: str | None = None,
    rules_only: bool = False,
) -> LLMResult:
    """`effort` sobrescreve LLM_EFFORT (ex.: "low" para as análises em tempo real da consulta).

    `rules_only` (paciente recusou a IA externa) nunca chama o LLM: devolve a saída determinística do `mock`.
    """
    settings = get_settings()
    prompt = load_prompt(prompt_name)

    if settings.llm_mode == "mock" or rules_only:
        return LLMResult(
            output=schema.model_validate(mock(variables)),
            model="mock-deterministico" if settings.llm_mode == "mock" else "regras-deterministicas",
            prompt_version=prompt.version,
            mode="mock",
        )

    providers.checar_regiao(settings)
    prov = providers.info(settings)
    escudo = Escudo(ativo=prov.externo)
    rendered = {
        k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1) for k, v in variables.items()
    }
    user = escudo.proteger(Template(prompt.user).safe_substitute(rendered))
    system = escudo.proteger(Template(prompt.system).safe_substitute(rendered), contar_estruturado=False)
    if settings.log_prompts:
        logger.info("Prompt %s v%s (pseudonimizado):\n%s", prompt_name, prompt.version, user)

    json_schema = strict_schema(schema)
    messages: list[dict] = [{"role": "user", "content": user}]
    estado = {"resultado": "erro", "reparado": False}
    try:
        text = _call(system, messages, json_schema, effort)
        try:
            out = schema.model_validate_json(escudo.reidentificar_json(text))
            estado["resultado"] = "ok"
            return _resultado(out, settings, prompt, prov, escudo, False)
        except ValidationError as first_error:
            logger.warning("Saída inválida do LLM em %s; tentando reparo: %s", prompt_name, first_error.errors()[:3])
            estado["reparado"] = True
            erros = json.dumps(
                [{k: v for k, v in e.items() if k in ("type", "loc", "msg")} for e in first_error.errors()[:5]],
                ensure_ascii=False,
                default=str,
            )
            messages += [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "A resposta anterior não passou na validação do schema. Erros: "
                        f"{erros}. Devolva o JSON completo corrigido, sem texto adicional."
                    ),
                },
            ]
            repaired = _call(system, messages, json_schema, effort)
            try:
                out = schema.model_validate_json(escudo.reidentificar_json(repaired))
            except ValidationError as exc:
                raise LLMError("A IA retornou um formato inválido duas vezes. Tente regenerar.") from exc
            estado["resultado"] = "ok"
            return _resultado(out, settings, prompt, prov, escudo, True)
    finally:
        recibo.registrar(
            {
                "prompt": prompt.name,
                "prompt_version": prompt.version,
                "provedor": prov.provedor,
                "modelo": prov.modelo,
                "regiao": prov.regiao,
                "externo": prov.externo,
                "local": prov.local,
                "pseudonimizacao_ativa": prov.externo,
                "entidades": dict(escudo.contagem),
                "resultado": estado["resultado"],
                "reparado": estado["reparado"],
            }
        )


def _resultado(out: BaseModel, settings, prompt: Prompt, prov, escudo: Escudo, repaired: bool) -> LLMResult:
    return LLMResult(
        output=out,
        model=settings.llm_model,
        prompt_version=prompt.version,
        mode="real",
        repaired=repaired,
        provider=prov.provedor or "",
        region=prov.regiao or "",
        external=prov.externo,
        entities=dict(escudo.contagem),
    )
