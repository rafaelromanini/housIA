"""Passagem de plantão falada com conferência ao vivo (#42): regras puras.

A conferência (`check.py`) roda a cada fala sobre o relato acumulado. Aqui ficam o que não depende do banco:
- de qual fala (`Fala/N`) vem cada "mencionado" e cada divergência;
- o roteiro SBAR que guia quem fala, com o que ainda falta dizer;
- a trava de segurança: o resultado da IA nunca rebaixa omissão ou divergência de fato crítico apontadas pelas regras.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field

from app.config import get_settings
from app.handoff.check import _says_stable, _sentences, fact_keywords
from app.handoff.schemas import Audience, Fact
from app.timeline.events import ClinicalEvent
from app.timeline.record import PatientRecord, normalize

MAX_TEXTO = 6000  # o mesmo teto do relato digitado (HandoffCheckRequest): acima disso a IA não entra, só as regras
MAX_TEXTO_REGRAS = 60000  # teto duro do relato acumulado (cada fala reprocessa tudo); vale também no WebSocket
MAX_SEGMENTOS = 200
ORDEM = {"mencionado": 0, "omitido": 1, "divergente": 2}
RANK = {"info": 0, "atencao": 1, "critico": 2}


class Segmento(BaseModel):
    n: int = Field(ge=1, le=100000)
    texto: str = Field(min_length=1, max_length=2000)
    t: float | None = None
    origem: Literal["digitado", "demo", "navegador", "servidor"] = "digitado"


class SegmentosIn(BaseModel):
    audience: Audience
    window_hours: int = Field(12, ge=1, le=72)
    segmentos: list[Segmento] = Field(min_length=1, max_length=MAX_SEGMENTOS)
    final: bool = Field(False, description="fim da passagem: roda a conferência completa (IA em modo real) e audita")
    ciente: bool = Field(description="quem fala sabe que a fala é transcrita e conferida; nada fica gravado")


SBAR = (
    {
        "sigla": "S",
        "titulo": "Situação",
        "guia": "Diga quem é o paciente, onde está, por que está internado e como está agora.",
        "palavras": ["leito", "internad", "admitid", "paciente", "quadro", "queixa", "pos-op", "pos op", "veio"],
    },
    {
        "sigla": "B",
        "titulo": "Histórico",
        "guia": "Diga antecedentes, alergias, o que mudou no período e as medicações importantes.",
        "palavras": ["antecedente", "alerg", "hipertens", "diabet", "cirurgia", "comorbid", "prescri", "medic"],
    },
    {
        "sigla": "A",
        "titulo": "Avaliação",
        "guia": "Diga sinais vitais, exames alterados, diurese, dispositivos e riscos (queda, lesão).",
        "palavras": ["pa ", "pressao", "fc ", "sat", "temperatura", "febr", "exame", "diurese", "urin", "dreno",
                     "acesso", "avp", "queda", "braden", "lesao", "dor", "glicemia", "potassio", "hemoglobina"],
    },
    {
        "sigla": "R",
        "titulo": "Recomendação",
        "guia": "Diga o que fica pendente, o que vigiar nas próximas horas e quando chamar o médico.",
        "palavras": ["pendente", "aguardando", "manter", "reavaliar", "vigiar", "atentar", "se piorar", "chamar",
                     "solicit", "precisa", "prox", "proxima", "falta"],
    },
)  # fmt: skip
# fato do prontuário -> etapa do SBAR em que ele deveria ser dito
ETAPA_DO_FATO = {
    "alergia": "B", "mudanca_prescricao": "B", "interacao": "B", "implante": "B", "transferencia": "S",
    "omissao": "S", "tarefa_aberta": "R", "exame_pendente": "R", "preocupacao_nao_respondida": "R",
    "alerta_consulta": "R", "proxima_dose": "R",
}  # fmt: skip


def _sentences_by_segment(segs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    return [(n, s) for n, text in segs for s in _sentences(text)]


def _fact_for(item: dict, facts: list[Fact]) -> Fact | None:
    refs = set(item["refs"])
    return next((f for f in facts if refs & set(f.refs)), None)


def atribuir_falas(
    items: list[dict],
    facts: list[Fact],
    record: PatientRecord,
    events_by_ref: dict[str, ClinicalEvent],
    segs: list[tuple[int, str]],
) -> list[dict]:
    """Cada item ganha `fala` (n) e `fala_ref` ("Fala/N"): a fala que o menciona ou que o contradiz. Omitido não tem."""
    sentences = _sentences_by_segment(segs)
    out = []
    for item in items:
        fala = None
        fact = _fact_for(item, facts)
        if fact is not None and item["status"] != "omitido":
            keys = fact_keywords(fact, record, events_by_ref)
            hits = [(n, s) for n, s in sentences if any(k in f" {s} " for k in keys)]
            if item["status"] == "divergente":
                stable = [n for n, s in hits if _says_stable(s)]
                fala = stable[-1] if stable else None
            elif hits:
                fala = hits[0][0]
        out.append(item | {"fala": fala, "fala_ref": f"Fala/{fala}" if fala else None})
    return out


def garantir_criticos(items: list[dict], regras: list[dict]) -> tuple[list[dict], int]:
    """A IA não rebaixa a regra: omissão ou divergência crítica apontada pelas regras continua no resultado, mesmo que o
    modelo a dê por mencionada ou a esqueça. Devolve os itens e quantas vezes a regra prevaleceu."""
    items = [dict(i) for i in items]
    forced = 0
    for r in regras:
        if r["severidade"] != "critico" or r["status"] == "mencionado":
            continue
        match = next((i for i in items if set(i["refs"]) & set(r["refs"])), None)
        if match is None:
            items.append(r | {"comentario": f"{r['comentario']} (regra de segurança)"})
            forced += 1
        elif ORDEM[match["status"]] < ORDEM[r["status"]] or RANK[match["severidade"]] < RANK["critico"]:
            match.update(
                status=max(match["status"], r["status"], key=ORDEM.__getitem__),
                severidade="critico",
                comentario=f"{r['comentario']} (mantido pela regra de segurança)",
            )
            forced += 1
    return items, forced


def roteiro(texto: str, items: list[dict], facts: list[Fact], final: bool) -> list[dict]:
    """Roteiro SBAR: o que já foi coberto e o que ainda falta dizer (fatos críticos ou de atenção não mencionados)."""
    t = f" {normalize(texto)} "
    pendentes: dict[str, list[str]] = {s["sigla"]: [] for s in SBAR}
    for item in items:
        if item["status"] != "omitido" or item["severidade"] == "info":
            continue
        fact = _fact_for(item, facts)
        sigla = ETAPA_DO_FATO.get(fact.tipo if fact else "", "A")
        pendentes[sigla].append(item["fato"])
    out = []
    for etapa in SBAR:
        coberto = any(p in t for p in etapa["palavras"])
        falta = pendentes[etapa["sigla"]]
        out.append(
            {
                "sigla": etapa["sigla"],
                "titulo": etapa["titulo"],
                "guia": etapa["guia"],
                "coberto": coberto,
                "faltam": falta,
                "estado": "completo" if coberto and not falta else ("falta_dizer" if falta else "a_dizer"),
            }
        )
    return out


def contagem(items: list[dict]) -> dict[str, int]:
    return {s: sum(i["status"] == s for i in items) for s in ("mencionado", "omitido", "divergente")}


@lru_cache
def demo_scripts() -> dict[str, dict]:
    """Roteiros de demonstração da passagem falada (data/handoff/*.json), por id do arquivo."""
    out: dict[str, dict] = {}
    for path in sorted((get_settings().data_dir / "handoff").glob("*.json")):
        with open(path, encoding="utf-8") as fh:
            out[path.stem] = json.load(fh) | {"id": path.stem}
    return out
