from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    # No repositório: <raiz>/apps/api/app/config.py -> <raiz>/data. Na imagem Docker, DATA_DIR=/data.
    here = Path(__file__).resolve()
    if len(here.parents) > 3:
        return here.parents[3] / "data"
    return Path("/data")


DEFAULT_JWT_SECRET = "dev-secret-housia-mvp-troque-em-producao"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5-5"
    llm_mode: Literal["real", "mock"] = "mock"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_timeout_s: float = 120.0
    llm_max_tokens: int = 16000
    log_prompts: bool = False
    # Provedor de IA substituível (#54): "anthropic" (padrão) ou "openai_compat" (servidor com /chat/completions,
    # ex.: vLLM ou Ollama). LLM_BASE_URL troca o endpoint (regional/proxy no Anthropic; obrigatório no openai_compat).
    llm_provider: Literal["anthropic", "openai_compat"] = "anthropic"
    llm_base_url: str = ""
    llm_api_key: str = ""  # chave do openai_compat (a do Anthropic continua em ANTHROPIC_API_KEY)
    # true: servidor próprio na rede da instituição. Pula a pseudonimização de saída, mantém a auditoria.
    llm_on_premise: bool = False
    # Regiões aceitas (lista separada por vírgula). Vazia: sem política. Com valor, LLM_REGION precisa estar na lista.
    llm_allowed_regions: str = ""
    # Saída estruturada no openai_compat: auto tenta json_schema, depois json_object, depois só instrução no prompt.
    llm_structured: Literal["auto", "json_schema", "json_object", "prompt"] = "auto"

    # Banco do HousIA
    database_url: str = "sqlite:///./housia.db"
    db_sslmode: str = ""  # Postgres: disable | require | verify-full ... (vazio: padrão do driver)

    # Fonte clínica: "hapi" lê um servidor FHIR R4; "local" monta os cenários em memória
    # (ver docs/decisions/0001-fonte-fhir-local.md)
    fhir_mode: Literal["hapi", "local"] = "local"
    fhir_base_url: str = "http://localhost:8080/fhir"

    llm_region: str = ""  # região do provedor de IA, exibida na tela Ambiente (#32)

    # Ambiente (#32): com ENV=production o app recusa subir com configuração de demonstração (app/prontidao.py)
    env: Literal["development", "production"] = "development"
    allow_mock: bool = False  # produção com LLM_MODE=mock só com este aceite explícito

    # Auth
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_expires_minutes: int = 12 * 60
    cookie_secure: bool | None = None  # None: ligado em produção
    # Chave do HMAC do encadeamento do audit_log (#53). Vazia: usa JWT_SECRET (trocar a chave quebra a verificação das
    # linhas antigas: gire só com a exportação assinada guardada).
    audit_chain_key: str = ""

    # E-mail (#33): verificação de cadastro, recuperação de senha e convites. Sem SMTP_HOST, a mensagem fica só no
    # log e na caixa de saída em memória (testes); no compose de dev o mailpit recebe tudo (UI 8025, API /api/v1).
    smtp_host: str = ""
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False
    smtp_from: str = "HousIA <nao-responda@housia.dev>"
    app_base_url: str = "http://localhost:3000"  # base dos links enviados por e-mail

    # Consulta com IA
    # Transcrição no servidor (opcional): endpoint compatível com /v1/audio/transcriptions (ex.: Whisper on-premise).
    # Sem isso, a transcrição ao vivo usa o reconhecimento de fala do navegador (ver ADR 0009).
    stt_url: str = ""
    stt_api_key: str = ""
    stt_model: str = "whisper-1"
    # Transmissão em fluxo (#11): "" usa o serviço de STT_URL; "fake" liga o motor determinístico (testes e demonstração
    # sem baixar pesos). Janela deslizante, sobreposição e silêncio que fecha a frase, em segundos.
    stt_engine: str = ""
    # true: o /consult/config só anuncia o servidor de fala quando ele responde (compose com perfil `stt` opcional)
    stt_probe: bool = False
    stt_janela_s: float = 8.0
    stt_sobreposicao_s: float = 1.0
    stt_silencio_s: float = 0.7
    # Transcrição que conhece o paciente (#38): abaixo desta confiança (0 a 1) a palavra é "de baixa confiança";
    # medicamento, dose ou alergia nessa situação pedem "confirmar o que foi ouvido". WER máximo aceito no CI.
    stt_confianca_minima: float = 0.6
    stt_wer_max: float = 0.15
    # Diarização acústica (#40): "off" (padrão, comportamento antigo), "heuristico" (local, determinístico) ou "externo"
    # (serviço on-premise em DIARIZATION_URL que devolve segmentos {inicio,fim,falante_id,embedding}).
    diarization_backend: str = "off"
    diarization_url: str = ""
    diarization_api_key: str = ""
    diarization_on_premise: bool = True  # a voz não sai da rede da instituição: padrão exige destino interno
    diarization_timeout_s: float = 10.0
    diarization_limiar: float = 0.75  # similaridade cosseno mínima com o perfil para ser o profissional
    diarization_margem: float = 0.10  # faixa abaixo do limiar em que a fala fica "incerta"
    # Autorização prévia (#56): vazio = tabelas de EXEMPLO do repositório; a instituição aponta para as importadas
    autorizacao_regras_csv: str = ""
    autorizacao_operadoras_csv: str = ""
    diarization_pausa_s: float = 0.4  # pausa que separa turnos no backend heurístico
    # Vocabulário institucional do STT (CSV termo,tipo,observacao); vazio: data/reference/vocab_institucional.csv
    vocab_institucional_path: Path | None = None
    # Envio da nota aprovada ao prontuário (FHIR DocumentReference). Desligado por padrão; ligado no compose demo.
    ehr_writeback: bool = False

    # Privacidade (#31): retenção da transcrição, criptografia de campo e captura de voz
    # "90" vale para todos os tipos; "consulta=90,visita=30,cirurgia=365" por tipo; vazio: não expurga
    transcript_retention_days: str = ""
    # Chaves `id:segredo` (a primeira cifra, todas decifram). Vazia: colunas clínicas sem criptografia
    data_encryption_key: str = ""
    # false: o áudio não vai ao fabricante do navegador (some o modo Microfone); sobra servidor, digitar e simular
    allow_browser_stt: bool = True

    data_dir: Path = Field(default_factory=_default_data_dir)
    # Protocolos de APS (#28): pasta de sobrescrita local (padrão: data/reference/protocols/local)
    protocolos_locais_dir: Path | None = None
    timezone: str = "America/Sao_Paulo"
    cors_origins: str = "http://localhost:3000"
    seed_users: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
