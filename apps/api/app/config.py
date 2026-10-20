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

    # Banco do HousIA
    database_url: str = "sqlite:///./housia.db"
    db_sslmode: str = ""  # Postgres: disable | require | verify-full ... (vazio: padrão do driver)

    # Fonte clínica: "hapi" lê um servidor FHIR R4; "local" monta os cenários em memória
    # (ver docs/decisions/0001-fonte-fhir-local.md)
    fhir_mode: Literal["hapi", "local"] = "local"
    fhir_base_url: str = "http://localhost:8080/fhir"

    # Auth
    jwt_secret: str = "dev-secret-housia-mvp-troque-em-producao"
    jwt_expires_minutes: int = 12 * 60

    # Consulta com IA
    # Transcrição no servidor (opcional): endpoint compatível com /v1/audio/transcriptions (ex.: Whisper on-premise).
    # Sem isso, a transcrição ao vivo usa o reconhecimento de fala do navegador (ver ADR 0009).
    stt_url: str = ""
    stt_api_key: str = ""
    stt_model: str = "whisper-1"
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
    timezone: str = "America/Sao_Paulo"
    cors_origins: str = "http://localhost:3000"
    seed_users: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
