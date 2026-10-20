"""Contrato de um Smart App: o manifest (quem é, quando roda, de onde lê) e o contexto que `run(ctx)` recebe.

Um app é um módulo em `app/consult/apps/` com `MANIFEST` e `run(ctx)`. Quem escreve o app não mexe no engine, no
schema nem na tela: o registro (`__init__.py`) descobre o módulo, valida o manifest e a tela desenha pelo tipo de card.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, field_validator

from app.consult.schemas import AppItem, Insights, Segment

if TYPE_CHECKING:
    from app.consult.chart import ChartView
    from app.timeline.record import PatientRecord

Aba = Literal["vivo", "raciocinio", "medicamentos", "prontuario", "pedidos", "transicao"]
Card = Literal["lista", "tags", "tabela", "linha_do_tempo"]
FHIR_TYPE = re.compile(r"^[A-Z][A-Za-z]+$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
ID = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


class Gatilho(BaseModel):
    """Quando o app roda. Tudo que o gatilho não diz, vale para qualquer consulta."""

    tipos: list[Literal["consulta", "visita"]] = ["consulta", "visita"]
    internado: bool | None = None  # True: só com internação em andamento
    sexo: Literal["", "F", "M"] = ""
    idade_min: int | None = None
    idade_max: int | None = None


class BaseCurada(BaseModel):
    """CSV (ou JSON) curado em data/reference de onde o app lê, com a versão e a fonte declaradas."""

    arquivo: str
    versao: str
    fonte: str


class Saida(BaseModel):
    card: Card = "lista"
    colunas: list[str] = []  # card "tabela": cabeçalho; cada AppItem traz uma célula por coluna

    def model_post_init(self, _: object) -> None:
        if self.card == "tabela" and not self.colunas:
            raise ValueError("card 'tabela' precisa de colunas")


class Manifest(BaseModel):
    id: str
    nome: str = Field(min_length=3, max_length=60)
    aba: Aba
    gatilho: Gatilho = Gatilho()
    especialidades: list[str] = []  # vazio = todas
    recursos_fhir: list[str] = []  # tipos FHIR que o app lê (documentação e auditoria)
    base: BaseCurada | None = None
    camada: Literal["regra", "ia", "mista"] = "regra"
    saida: Saida = Saida()
    versao: str = "1.0.0"
    ordem: int = 100  # posição dentro da aba (menor primeiro)
    refs_obrigatorias: bool = True  # item sem referência válida é descartado (apps migrados já vêm validados)
    vazio: str = "Nada a apontar por enquanto."

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID.match(v):
            raise ValueError("id em minúsculas, números e _ (3 a 41 caracteres)")
        return v

    @field_validator("versao")
    @classmethod
    def _versao(cls, v: str) -> str:
        if not SEMVER.match(v):
            raise ValueError("versão no formato 1.0.0")
        return v

    @field_validator("recursos_fhir")
    @classmethod
    def _fhir(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if not FHIR_TYPE.match(t)]
        if bad:
            raise ValueError(f"tipo FHIR inválido: {bad}")
        return v


@dataclass
class AppContext:
    """O que um app lê: os Insights do engine, o prontuário (ChartView) e a conversa até agora."""

    ins: Insights
    chart: ChartView
    kind: str
    specialty: str
    segments: list[Segment] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def record(self) -> PatientRecord:
        return self.chart.r

    @property
    def now(self) -> datetime:
        return self.chart.now

    @property
    def internado(self) -> bool:
        h = self.record.hospitalization
        return bool(h and h.get("status") == "in-progress")

    @property
    def patient_ref(self) -> str:
        return f"Patient/{self.record.id}"


RunFn = Callable[[AppContext], "list[AppItem] | None"]


def sev_rank(severity: str) -> int:
    return {"critico": 0, "atencao": 1, "info": 2, "ok": 3}.get(severity, 4)


def in_age(age: int | None, low: str | int | None, high: str | int | None) -> bool:
    """A idade cai na faixa [low, high] da linha curada (célula vazia = sem limite)? Idade desconhecida não passa."""
    if age is None:
        return False
    return (not low or age >= int(low)) and (not high or age <= int(high))
