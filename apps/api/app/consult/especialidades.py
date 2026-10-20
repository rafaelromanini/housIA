"""Pacotes de especialidade (#27): data/reference/specialties/*.yaml.

Um pacote reúne o que muda de uma especialidade para outra: rótulo e foco, perguntas extras, Smart Apps padrão, modelo
de nota, escalas, glossário popular → técnico e hipóteses priorizadas. Esta é a fonte de `SPECIALTIES` e
`SPECIALTY_FOCUS` (knowledge.py). Os pacotes são validados aqui pelo schema (campo desconhecido é erro) e, nos testes,
contra o registro de Smart Apps, os modelos de nota, as escalas e a tabela CID-10.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import get_settings

ID = r"^[a-z][a-z0-9_]{2,40}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PerguntaPacote(_Strict):
    id: str = Field(pattern=r"^pk_[a-z0-9_]{3,30}$")
    pergunta: str = Field(min_length=10)
    motivo: str = Field(min_length=10)
    ja_perguntado: list[str] = Field(min_length=1)  # palavras (sem acento) que mostram que o profissional já perguntou
    prioridade: Literal["alta", "media", "baixa"] = "media"
    fonte: str = Field(min_length=5)
    idade_min: int | None = Field(None, ge=0, le=120)
    idade_max: int | None = Field(None, ge=0, le=120)


class Pacote(_Strict):
    id: str = Field(pattern=ID)
    ordem: int = Field(ge=1)
    rotulo: str = Field(min_length=3)
    foco: str = Field(min_length=10)
    modelo_nota: str = Field(pattern=ID)
    smart_apps: list[str] = Field(min_length=1)
    escalas: list[str] = []
    hipoteses_prioritarias: list[str] = []  # CID-10 reais; sobem na lista quando a confiança empata
    glossario: dict[str, str] = {}  # termo popular → termo técnico (vale na nota e no prompt do STT)
    perguntas: list[PerguntaPacote] = []

    @field_validator("hipoteses_prioritarias")
    @classmethod
    def _cid(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if not re.fullmatch(r"[A-Z]\d{2}(\.\d{1,2})?", c)]
        if bad:
            raise ValueError(f"CID-10 em formato inválido: {bad}")
        return v

    @field_validator("glossario")
    @classmethod
    def _glossario(cls, v: dict[str, str]) -> dict[str, str]:
        if any(not k.strip() or not t.strip() for k, t in v.items()):
            raise ValueError("glossário com termo vazio")
        return v


@lru_cache
def pacotes() -> dict[str, Pacote]:
    """Pacotes por id, na ordem do campo `ordem`."""
    folder = get_settings().data_dir / "reference" / "specialties"
    found: list[Pacote] = []
    for path in sorted(folder.glob("*.yaml")):
        with open(path, encoding="utf-8") as fh:
            pack = Pacote.model_validate(yaml.safe_load(fh))
        if pack.id != path.stem:
            raise ValueError(f"Pacote {path.name}: id '{pack.id}' difere do nome do arquivo")
        found.append(pack)
    ordens = [p.ordem for p in found]
    if len(set(ordens)) != len(ordens):
        raise ValueError("Pacotes de especialidade com a mesma ordem")
    ids = [q.id for p in found for q in p.perguntas]
    if len(set(ids)) != len(ids):
        raise ValueError("Pergunta de pacote repetida")
    return {p.id: p for p in sorted(found, key=lambda p: p.ordem)}
