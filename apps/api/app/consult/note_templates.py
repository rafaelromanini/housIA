"""Modelos de nota por especialidade e tipo de atendimento, em dados (data/reference/note_templates/*.yaml).

Cada modelo é versionado e traz as seções {key, rótulo, LOINC, obrigatória, dica}. As nove seções clássicas
(queixa principal ... plano) continuam nos campos fixos da `ConsultNote`; as demais ficam em `secoes`, uma entrada
por `key`. A nota guarda uma cópia do modelo com que foi gerada (`modelo`), então mudar o YAML depois não reescreve
uma nota já revisada. Nota antiga, sem `modelo`, é convertida na leitura (`upgrade`).
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.consult.schemas import SecaoModelo
from app.consult.text import norm
from app.reference import _read_csv

if TYPE_CHECKING:  # só para tipos: evita ciclo com engine/schemas
    from app.consult.engine import Engine
    from app.consult.schemas import Insights

FIXED_SECTIONS = (
    "queixa_principal",
    "hda",
    "antecedentes",
    "medicacoes",
    "alergias",
    "exame_fisico",
    "exames_complementares",
    "avaliacao",
    "plano",
)
NO_RECORD = "Sem registro na consulta."
DEFAULT_TEMPLATE = "consulta_clinica"
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SectionSpec(_Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    rotulo: str = Field(min_length=3)
    soap: Literal["S", "O", "A", "P"]
    obrigatoria: bool = False
    dica: str = ""
    loinc: str | None = Field(None, pattern=r"^\d{1,7}-\d$")
    padrao: str | None = None  # texto com {{placeholders}} resolvidos pelo prontuário (FHIR), com a ref
    campo: str | None = None  # campo extra da especialidade lido da fala (anamnese.campos_especialidade)
    palavras: list[str] = []  # falas que mencionam estas palavras entram como rascunho da seção (com Fala/N)
    prefixo: str = ""


class LoincRef(_Strict):
    codigo: str = Field(pattern=r"^\d{1,7}-\d$")
    display: str


class NoteTemplate(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    versao: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    rotulo: str
    titulo: str
    tipo: Literal["consulta", "visita", "enfermagem", "ps", "aps"]
    especialidades: list[str] = []
    automatico: bool = True  # entra na escolha por especialidade e tipo; os demais só por troca manual na revisão
    loinc: LoincRef
    fonte: str
    secoes: list[SectionSpec] = Field(min_length=1)


def _folder():
    return get_settings().data_dir / "reference" / "note_templates"


@lru_cache
def templates() -> dict[str, NoteTemplate]:
    from app.consult import knowledge  # import tardio: knowledge também lê dados de referência

    out: dict[str, NoteTemplate] = {}
    for path in sorted(_folder().glob("*.yaml")):
        with open(path, encoding="utf-8") as fh:
            tpl = NoteTemplate.model_validate(yaml.safe_load(fh))
        if tpl.id != path.stem:
            raise ValueError(f"Modelo de nota {path.name}: id '{tpl.id}' difere do nome do arquivo")
        keys = [s.key for s in tpl.secoes]
        if len(set(keys)) != len(keys):
            raise ValueError(f"Modelo de nota {tpl.id}: seção repetida")
        if unknown := [e for e in tpl.especialidades if e not in knowledge.SPECIALTIES]:
            raise ValueError(f"Modelo de nota {tpl.id}: especialidade desconhecida {unknown}")
        missing = [k for k in ("avaliacao", "plano", "hda", "queixa_principal") if k not in keys]
        if missing:  # o que o aceite de itens e o "incluir em…" gravam precisa estar visível na revisão
            raise ValueError(f"Modelo de nota {tpl.id}: faltam as seções {missing}")
        out[tpl.id] = tpl
    if DEFAULT_TEMPLATE not in out:
        raise ValueError("Falta o modelo de nota padrão (consulta_clinica)")
    return out


def get(template_id: str) -> NoteTemplate | None:
    return templates().get(template_id)


def select(kind: str, specialty: str) -> NoteTemplate:
    """Modelo pela especialidade e pelo tipo de atendimento: o mais específico entre os automáticos."""
    best, best_score = templates()[DEFAULT_TEMPLATE], 0
    for tpl in templates().values():
        if not tpl.automatico:
            continue
        by_specialty = bool(tpl.especialidades) and specialty in tpl.especialidades
        by_kind = tpl.tipo == kind
        if tpl.especialidades and not by_specialty:
            continue
        if not tpl.especialidades and not by_kind:
            continue
        score = (2 if by_specialty else 0) + (1 if by_kind else 0)
        if score > best_score:
            best, best_score = tpl, score
    return best


def snapshot(tpl: NoteTemplate) -> dict:
    """O que a nota guarda do modelo: versão, rótulos e regras das seções (auditável mesmo se o YAML mudar)."""
    return {
        "id": tpl.id,
        "versao": tpl.versao,
        "rotulo": tpl.rotulo,
        "titulo": tpl.titulo,
        "tipo": tpl.tipo,
        "loinc": tpl.loinc.model_dump(),
        "secoes": [s.model_dump(include={"key", "rotulo", "soap", "obrigatoria", "dica", "loinc"}) for s in tpl.secoes],
    }


def listing(kind: str | None = None, specialty: str | None = None) -> list[dict]:
    suggested = select(kind or "consulta", specialty or "clinica_medica").id if (kind or specialty) else None
    return [
        snapshot(t) | {"fonte": t.fonte, "especialidades": t.especialidades, "sugerido": t.id == suggested}
        for t in templates().values()
    ]


def dynamic_keys(model: dict) -> list[str]:
    return [s["key"] for s in model.get("secoes", []) if s["key"] not in FIXED_SECTIONS]


# ---------- leitura e conversão ----------
def upgrade(note: dict | None, kind: str, specialty: str) -> dict | None:
    """Nota sem `modelo` (gerada antes dos modelos) é lida com o modelo que o atendimento escolheria hoje; as seções
    dinâmicas que a nota antiga não tem entram vazias. Não altera o dict recebido."""
    if note is None or "relatorio_cirurgico" in note:
        return note
    out = dict(note)
    if not out.get("modelo"):
        out["modelo"] = snapshot(select(kind, specialty))
    out["secoes"] = complete_sections(out.get("secoes"), out["modelo"])
    return out


def complete_sections(current: Any, model: dict) -> list[dict]:
    have = {s["key"]: s for s in (current or []) if isinstance(s, dict) and "key" in s}
    wanted = dynamic_keys(model)
    known = {s.key for t in templates().values() for s in t.secoes}  # chave inventada pelo cliente não é guardada
    kept = [
        {"key": k, "texto": str(have[k].get("texto", "")), "refs": list(have[k].get("refs", []))}
        for k in have
        if k not in wanted and k in known
    ]
    # seções de outro modelo ficam guardadas (a troca não perde texto); as do modelo vêm antes, na ordem dele
    return [
        {"key": k, "texto": str(have[k].get("texto", "")), "refs": list(have[k].get("refs", []))}
        if k in have
        else {"key": k, "texto": "", "refs": []}
        for k in wanted
    ] + kept


def section_text(note: dict, key: str) -> str:
    if key in FIXED_SECTIONS:
        return str((note.get(key) or {}).get("texto", ""))
    return next((str(s.get("texto", "")) for s in note.get("secoes") or [] if s.get("key") == key), "")


def is_empty(text: str) -> bool:
    t = (text or "").strip()
    return not t or norm(t).startswith("sem registro")


def missing_required(note: dict, model: dict) -> list[str]:
    """Rótulos das seções obrigatórias do modelo que estão vazias (em branco ou só "Sem registro na consulta")."""
    return [
        s["rotulo"] for s in model.get("secoes", []) if s.get("obrigatoria") and is_empty(section_text(note, s["key"]))
    ]


def sections_for_view(note: dict) -> list[tuple[str, str, str]]:
    """(key, rótulo, texto) na ordem do modelo, para o texto do prontuário."""
    model = note.get("modelo") or {}
    return [(s["key"], s["rotulo"], section_text(note, s["key"])) for s in model.get("secoes", [])]


def loinc_of(note: dict | None) -> tuple[str, str] | None:
    loinc = ((note or {}).get("modelo") or {}).get("loinc")
    return (loinc["codigo"], loinc["display"]) if loinc else None


def for_prompt(tpl: NoteTemplate) -> str:
    """Instruções do modelo para o LLM: uma linha por seção dinâmica (as fixas já têm campo próprio no schema)."""
    lines = [f"Modelo: {tpl.rotulo} (v{tpl.versao}). Preencha também `secoes`, uma entrada por `key` abaixo:"]
    for s in tpl.secoes:
        if s.key in FIXED_SECTIONS:
            continue
        flag = " [obrigatória]" if s.obrigatoria else ""
        lines.append(f"- {s.key}: {s.rotulo}{flag}. {s.dica}".rstrip())
    emphasis = [s for s in tpl.secoes if s.key in FIXED_SECTIONS and s.dica and s.obrigatoria]
    if emphasis:
        lines.append("Seções clássicas obrigatórias neste modelo: " + ", ".join(s.key for s in emphasis) + ".")
    return "\n".join(lines)


# ---------- preenchimento determinístico (mock, regras e seções que o LLM deixou vazias) ----------
def _fmt_num(value: float, decimals: int = 1) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


def _vital(engine: Engine, key: str):
    return engine.chart.latest_vital(key, max_hours=48)


def resolve(key: str, engine: Engine, ins: Insights) -> tuple[str, list[str]] | None:
    """Valor de um {{placeholder}} a partir do prontuário (FHIR) ou dos achados, com a ref de onde veio."""
    chart = engine.chart
    if key == "peso":
        w = chart.weight()
        return (f"{_fmt_num(w[0])} kg", [w[1]]) if w else None
    if key == "tfg":
        egfr = chart.egfr
        return (f"{round(egfr[0])} mL/min/1,73 m² (CKD-EPI 2021)", [egfr[1].source_ref]) if egfr else None
    if key == "alergias":
        if ins.alergias:
            return "; ".join(a.alergeno for a in ins.alergias), [r for a in ins.alergias for r in a.refs]
        return ("Nega alergias conhecidas", []) if chart.ctx.nega_alergias else None
    if key == "pa":
        e = _vital(engine, "pas")
        return (e.summary, [e.source_ref]) if e else None
    if key in ("fc", "fr", "spo2", "temp"):
        e = _vital(engine, key)
        return (e.summary, [e.source_ref]) if e else None
    if key == "ciap2":
        return _ciap2(engine, ins)
    return None  # estatura e demais: sem fonte estruturada neste ambiente


@lru_cache
def _ciap_rows() -> tuple[dict[str, str], ...]:
    return tuple(_read_csv("ciap2_cid10.csv"))


def ciap2_for(cid: str) -> dict[str, str] | None:
    """Correspondência CID-10 → CIAP-2 pelo prefixo mais longo (tabela curada de demonstração)."""
    cid = (cid or "").upper()
    rows = [r for r in _ciap_rows() if cid.startswith(r["cid_prefixo"])]
    return max(rows, key=lambda r: len(r["cid_prefixo"])) if rows else None


def _ciap2(engine: Engine, ins: Insights) -> tuple[str, list[str]] | None:
    found: dict[str, tuple[str, list[str]]] = {}
    for h in ins.hipoteses:
        if (row := ciap2_for(h.cid)) and row["ciap2"] not in found:
            found[row["ciap2"]] = (f"{row['ciap2']} {row['descricao']} (CID-10 {h.cid})", h.refs[:2])
    if not found:
        for icd, _, c in engine.chart.conditions:
            if (row := ciap2_for(icd)) and row["ciap2"] not in found:
                found[row["ciap2"]] = (f"{row['ciap2']} {row['descricao']} (CID-10 {icd})", [f"Condition/{c['id']}"])
    if not found:
        return None
    return "; ".join(t for t, _ in found.values()), [r for _, refs in found.values() for r in refs]


def _from_placeholders(spec: SectionSpec, engine: Engine, ins: Insights) -> tuple[str, list[str]] | None:
    parts, refs, resolved = [], [], 0
    for chunk in (c.strip() for c in (spec.padrao or "").split(";")):
        if not chunk:
            continue
        keys = _PLACEHOLDER.findall(chunk)
        values = [resolve(k, engine, ins) for k in keys]
        if keys and all(values):
            text = chunk
            for v in values:
                if v:
                    text = _PLACEHOLDER.sub(lambda _m, v=v: v[0], text, count=1)
                    refs += v[1]
            parts.append(text)
            resolved += 1
    return ("; ".join(parts), list(dict.fromkeys(refs))) if resolved else None


def _from_words(spec: SectionSpec, engine: Engine) -> tuple[str, list[str]] | None:
    # palavra com espaço no fim exige fim de palavra ("dor " não pega "dormir"); sem espaço vale como prefixo
    pats = [re.compile(r"\b" + re.escape(norm(w)) + (r"\b" if w.endswith(" ") else "")) for w in spec.palavras]
    hits = []
    for seg in engine.segments:
        if seg.origem == "marco" or seg.relevancia in ("social", "administrativa"):
            continue
        text = norm(seg.texto)
        if any(p.search(f" {text} ") for p in pats):
            hits.append(seg)
    hits = hits[:4]
    if not hits:
        return None
    return spec.prefixo + " ".join(s.texto.strip() for s in hits), [f"Fala/{s.n}" for s in hits]


def fill(spec: SectionSpec, engine: Engine, ins: Insights) -> dict:
    """Rascunho determinístico de uma seção dinâmica: dado do prontuário > campo da especialidade > falas citadas."""
    found = None
    if spec.padrao:
        found = _from_placeholders(spec, engine, ins)
    if found is None and spec.campo and ins.anamnese:
        field = next((c for c in ins.anamnese.campos_especialidade if c.campo == spec.campo and c.valor), None)
        if field:
            found = (field.valor, list(field.refs))
    if found is None and spec.palavras:
        found = _from_words(spec, engine)
    text, refs = found if found else (NO_RECORD, [])
    return {"key": spec.key, "texto": text, "refs": refs}


def fill_dynamic(tpl: NoteTemplate, engine: Engine, ins: Insights, only: set[str] | None = None) -> list[dict]:
    return [fill(s, engine, ins) for s in tpl.secoes if s.key not in FIXED_SECTIONS and (only is None or s.key in only)]


def complete(note: Any, tpl: NoteTemplate, engine: Engine, ins: Insights, valid: set[str]) -> None:
    """Depois do LLM: refs inválidas caem, e a seção dinâmica que faltou ou veio vazia recebe o rascunho das regras."""
    by_key = {s.key: s for s in note.secoes}
    rules = {f["key"]: f for f in fill_dynamic(tpl, engine, ins)}
    out = []
    for key, base in rules.items():
        ai = by_key.get(key)
        if ai is not None:
            ai.refs = [r for r in ai.refs if r in valid]
        out.append(ai if ai is not None and not is_empty(ai.texto) else SecaoModelo(**base))
    note.secoes = out
