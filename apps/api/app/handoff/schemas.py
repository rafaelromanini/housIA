from typing import Literal

from pydantic import BaseModel, Field

Audience = Literal["medico", "enfermagem"]
Severity = Literal["info", "atencao", "critico"]


class ItemRef(BaseModel):
    texto: str
    refs: list[str] = Field(description="Identificadores de fonte, ex.: Observation/abc")


class Risco(BaseModel):
    texto: str
    horizonte_h: int = Field(description="Em quantas horas o risco pode se manifestar (1-24)")
    severidade: Severity
    refs: list[str]


class Pendencia(BaseModel):
    texto: str
    horario: str = Field(description="HH:MM local ou 'sem horário'")
    refs: list[str]


class HandoffOutput(BaseModel):
    """Saída estruturada da passagem de plantão (validada antes de exibir)."""

    headline: str
    situacao: str
    situacao_refs: list[str]
    mudancas_no_periodo: list[ItemRef]
    riscos_proximas_horas: list[Risco]
    pendencias: list[Pendencia]
    perguntas_para_quem_sai: list[str]


class Fact(BaseModel):
    """Fato duro calculado deterministicamente antes do LLM."""

    tipo: str
    texto: str
    severidade: Severity = "info"
    refs: list[str]
    publico: list[Audience] = ["medico", "enfermagem"]
    horario: str | None = None
    horizonte_h: int | None = None


class HandoffRequest(BaseModel):
    audience: Audience
    window_hours: int = Field(12, ge=1, le=72)
    to_location: str | None = None
    force: bool = False
