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

    # Fonte clínica: "hapi" lê um servidor FHIR R4; "local" monta os cenários em memória
    # (ver docs/decisions/0001-fonte-fhir-local.md)
    fhir_mode: Literal["hapi", "local"] = "local"
    fhir_base_url: str = "http://localhost:8080/fhir"

    # Auth
    jwt_secret: str = "dev-secret-housia-mvp-troque-em-producao"
    jwt_expires_minutes: int = 12 * 60

    data_dir: Path = Field(default_factory=_default_data_dir)
    timezone: str = "America/Sao_Paulo"
    cors_origins: str = "http://localhost:3000"
    seed_users: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
