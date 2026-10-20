"""Prontidão de produção (#32): o app não sobe em produção com configuração de demonstração,
e a tela Ambiente mostra a configuração em vigor."""

from sqlalchemy.engine import make_url

from app.config import DEFAULT_JWT_SECRET, Settings, get_settings

JWT_MIN_CHARS = 32
SENHAS_PADRAO_BANCO = {"", "housia", "postgres", "password", "123456", "admin"}  # "" = sem senha


class ConfiguracaoInsegura(RuntimeError):
    """Configuração de demonstração em ENV=production."""


def problemas(s: Settings) -> list[str]:
    """Lista o que impede a subida em produção (vazia fora de produção)."""
    if s.env != "production":
        return []
    out: list[str] = []
    segredo = s.jwt_secret.strip()
    if segredo == DEFAULT_JWT_SECRET or "troque" in segredo.lower():  # inclui o texto de exemplo do .env.example
        out.append("JWT_SECRET está com o valor padrão ou de exemplo")
    elif len(segredo) < JWT_MIN_CHARS:
        out.append(f"JWT_SECRET tem menos de {JWT_MIN_CHARS} caracteres")
    if s.llm_mode == "mock" and not s.allow_mock:
        out.append("LLM_MODE=mock em produção (defina ALLOW_MOCK=true para aceitar só regras locais)")
    try:
        url = make_url(s.database_url)
    except Exception:  # noqa: BLE001
        url = None
    if (
        url is not None
        and url.get_backend_name() == "postgresql"
        and (url.password or "").lower() in SENHAS_PADRAO_BANCO
    ):
        out.append("a senha do Postgres em DATABASE_URL é a padrão de demonstração")
    return out


def exigir_producao_segura(s: Settings | None = None) -> None:
    ruins = problemas(s or get_settings())
    if ruins:
        raise ConfiguracaoInsegura("Configuração insegura para ENV=production: " + "; ".join(ruins))


def cookie_seguro(s: Settings | None = None) -> bool:
    s = s or get_settings()
    return s.env == "production" if s.cookie_secure is None else s.cookie_secure


def checklist(s: Settings | None = None) -> list[dict]:
    """Checklist de transparência lido da configuração em vigor (não de texto fixo)."""
    from app.consult import privacidade, stt
    from app.db import crypto

    s = s or get_settings()
    painel = privacidade.panel()
    if s.allow_browser_stt:  # mesma precedência do painel de privacidade
        transcricao = ("atencao", "Transcrição no navegador: o áudio é processado pelo fabricante do navegador.")
    elif stt.enabled():
        transcricao = ("ok", "Transcrição no servidor (STT configurado); o áudio não sai do hospital.")
    else:
        transcricao = ("ok", "Sem captura de voz: só digitar ou simular; nenhum áudio sai do hospital.")
    if s.llm_mode == "real":
        ia = (
            "atencao",
            f"IA externa: provedor Anthropic, modelo {s.llm_model}, região {s.llm_region or 'não informada'}. "
            "Só com consentimento e texto pseudonimizado.",
        )
    else:
        ia = ("ok", "IA em modo mock: só regras locais; nenhum texto sai do servidor.")
    prontuario = (
        ("ok", "Nota aprovada enviada ao prontuário (DocumentReference FHIR).")
        if s.ehr_writeback
        else ("info", "Envio ao prontuário desligado (EHR_WRITEBACK=false).")
    )
    prazos = {k: d for k, d in privacidade.retention_days().items() if d is not None}
    retencao = (
        (
            "ok",
            "Texto das falas apagado após o envio da nota: "
            + ", ".join(f"{k} {d} dia(s)" for k, d in prazos.items())
            + ".",
        )
        if prazos
        else ("atencao", painel["retencao"])
    )
    cripto = ("ok", painel["criptografia"]) if crypto.enabled() else ("atencao", painel["criptografia"])
    seg = problemas(s.model_copy(update={"env": "production"}))
    return [
        {"id": "transcricao", "titulo": "Transcrição", "estado": transcricao[0], "texto": transcricao[1]},
        {"id": "ia", "titulo": "Inteligência artificial", "estado": ia[0], "texto": ia[1]},
        {"id": "prontuario", "titulo": "Envio ao prontuário", "estado": prontuario[0], "texto": prontuario[1]},
        {"id": "retencao", "titulo": "Retenção", "estado": retencao[0], "texto": retencao[1]},
        {"id": "criptografia", "titulo": "Criptografia", "estado": cripto[0], "texto": cripto[1]},
        {
            "id": "seguranca",
            "titulo": "Segredos e acesso",
            "estado": "ok" if not seg else "atencao",
            "texto": "Segredos próprios, sem valores de demonstração."
            if not seg
            else "Valores de demonstração em uso: " + "; ".join(seg) + ".",
        },
    ]


def ambiente(s: Settings | None = None) -> dict:
    s = s or get_settings()
    return {
        "env": s.env,
        "producao": s.env == "production",
        "llm_mode": s.llm_mode,
        "llm": {
            "provedor": "Anthropic" if s.llm_mode == "real" else None,
            "modelo": s.llm_model if s.llm_mode == "real" else None,
            "regiao": (s.llm_region or None) if s.llm_mode == "real" else None,
        },
        "fhir_mode": s.fhir_mode,
        "checklist": checklist(s),
    }
