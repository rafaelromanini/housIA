"""Modelos do relatório cirúrgico em dados (data/reference/surgical_templates/*.yaml).

O hospital edita os YAML (cada um com `versao`); aqui a estrutura é validada, o modelo de um procedimento é
resolvido (base + regras da especialidade + procedimento) e o procedimento é reconhecido pelo ditado ou pelo
agendamento. Cada campo carrega sinônimos, obrigatoriedade e o motivo com a fonte.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.consult import knowledge
from app.consult.text import norm

DEFAULT_ANCHOR = "tecnica"  # campos próprios do procedimento entram depois da técnica, salvo `depois_de`


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldSpec(_Strict):
    campo: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    rotulo: str = Field(min_length=3)
    sinonimos: list[str] = Field(min_length=1)
    obrigatorio: bool
    motivo: str = Field(min_length=10)
    fonte: str
    granular: bool = False
    valor: str | None = None
    grupo: str | None = None
    somente_no_modelo: bool = False
    depois_de: str | None = None


class Requirement(_Strict):
    obrigatorio: bool
    motivo: str = Field(min_length=10)
    fonte: str


class GroupSpec(_Strict):
    sinonimos: list[str] = Field(min_length=1)
    ausente: str


class ControlExam(_Strict):
    descricao: str
    sinonimos: list[str] = []


class SpecialtyRules(_Strict):
    obrigatoriedade: dict[str, Requirement] = {}


class BaseTemplate(_Strict):
    versao: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    id: Literal["base"]
    rotulo: str
    fontes: dict[str, str]
    grupos: dict[str, GroupSpec] = {}
    campos: list[FieldSpec] = Field(min_length=1)
    especialidades: dict[str, SpecialtyRules] = {}
    exame_controle: ControlExam


class ProcedureTemplate(_Strict):
    versao: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    rotulo: str
    especialidade: str
    detectar: list[str] = Field(min_length=1)
    sitio: list[str] = []
    profilaxia: list[str] = []
    exame_controle: ControlExam | None = None
    campos: list[FieldSpec] = []
    obrigatoriedade: dict[str, Requirement] = {}


# ---------- sinônimos ----------
def syn(word: str) -> str:
    """Normaliza o sinônimo (sem acento, minúsculo) e preserva o espaço final, que exige a palavra inteira."""
    return norm(word) + (" " if word.endswith(" ") else "")


def _pattern(words: tuple[str, ...]) -> re.Pattern | None:
    if not words:
        return None
    parts = [re.escape(w.strip()) + (r"(?![a-z0-9])" if w.endswith(" ") else "") for w in words]
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + ")")


def first_hit(text: str, words: tuple[str, ...]) -> str | None:
    """Primeiro sinônimo presente no texto já normalizado, a partir do início de uma palavra."""
    pat = _pattern(words)
    m = pat.search(text) if pat else None
    return m.group(0) if m else None


# ---------- modelo resolvido ----------
@dataclass(frozen=True)
class ResolvedField:
    campo: str
    rotulo: str
    words: tuple[str, ...]
    obrigatorio: bool
    motivo: str
    fonte: str
    granular: bool
    valor: re.Pattern | None
    grupo: str | None


@dataclass(frozen=True)
class ResolvedTemplate:
    id: str
    rotulo: str
    especialidade: str
    versao: str
    versao_base: str
    fields: tuple[ResolvedField, ...]
    sitio: tuple[str, ...] = ()
    profilaxia: tuple[tuple[str, ...], ...] = ()  # opções recomendadas; cada uma pode combinar fármacos
    exame_controle: str = ""
    exame_controle_words: tuple[str, ...] = ()
    grupos: dict[str, tuple[tuple[str, ...], str]] = field(default_factory=dict)

    def get(self, campo: str) -> ResolvedField | None:
        return next((f for f in self.fields if f.campo == campo), None)

    @property
    def lateral(self) -> bool:
        f = self.get("lateralidade")
        return bool(f and f.obrigatorio)


@dataclass(frozen=True)
class TemplateSet:
    base: BaseTemplate
    procedures: dict[str, ProcedureTemplate]

    def source(self, key: str) -> str:
        return self.base.fontes.get(key, key)

    def resolve(self, proc_id: str | None = None, specialty: str | None = None) -> ResolvedTemplate:
        proc = self.procedures.get(proc_id or "")
        spec = proc.especialidade if proc else specialty
        overrides: dict[str, Requirement] = {}
        if spec and spec in self.base.especialidades:
            overrides |= self.base.especialidades[spec].obrigatoriedade
        if proc:
            overrides |= proc.obrigatoriedade
        extras = proc.campos if proc else []
        specs: list[FieldSpec] = []
        placed: set[str] = set()

        def emit(f: FieldSpec) -> None:
            specs.append(f)
            for e in extras:  # campos do procedimento logo depois do campo âncora, na ordem do YAML
                if (e.depois_de or DEFAULT_ANCHOR) == f.campo and e.campo not in placed:
                    placed.add(e.campo)
                    emit(e)

        for f in self.base.campos:
            if not f.somente_no_modelo or f.campo in overrides:
                emit(f)
        specs += [e for e in extras if e.campo not in placed]
        fields = []
        for f in specs:
            req = overrides.get(f.campo)
            fields.append(
                ResolvedField(
                    campo=f.campo,
                    rotulo=f.rotulo,
                    words=tuple(syn(w) for w in f.sinonimos),
                    obrigatorio=req.obrigatorio if req else f.obrigatorio,
                    motivo=req.motivo if req else f.motivo,
                    fonte=self.source(req.fonte if req else f.fonte),
                    granular=f.granular,
                    valor=re.compile(f.valor) if f.valor else None,
                    grupo=f.grupo,
                )
            )
        exam = proc.exame_controle if proc and proc.exame_controle else None
        base_exam = self.base.exame_controle
        return ResolvedTemplate(
            id=proc.id if proc else (f"especialidade:{spec}" if spec in self.base.especialidades else "base"),
            rotulo=proc.rotulo if proc else self.base.rotulo,
            especialidade=spec or "",
            versao=proc.versao if proc else self.base.versao,
            versao_base=self.base.versao,
            fields=tuple(fields),
            sitio=tuple(syn(w) for w in (proc.sitio if proc else [])),
            profilaxia=tuple(tuple(opt.split("+")) for opt in (proc.profilaxia if proc else [])),
            exame_controle=(exam.descricao if exam else base_exam.descricao),
            exame_controle_words=tuple(syn(w) for w in [*base_exam.sinonimos, *(exam.sinonimos if exam else [])]),
            grupos={k: (tuple(syn(w) for w in g.sinonimos), g.ausente) for k, g in self.base.grupos.items()},
        )

    def detect(self, text: str) -> tuple[ProcedureTemplate, str] | None:
        """Procedimento citado num texto já normalizado: o sinônimo mais longo (mais específico) vence."""
        best: tuple[int, ProcedureTemplate, str] | None = None
        for p in self.procedures.values():
            hit = first_hit(text, tuple(syn(w) for w in p.detectar))
            if hit and (best is None or len(hit) > best[0]):
                best = (len(hit), p, hit)
        return (best[1], best[2]) if best else None


# ---------- carga e validação ----------
def _folder():
    return get_settings().data_dir / "reference" / "surgical_templates"


def problems(base: BaseTemplate, procedures: dict[str, ProcedureTemplate]) -> list[str]:
    """Erros de conteúdo que o schema sozinho não pega (fontes, campos repetidos, chaves desconhecidas)."""
    out: list[str] = []
    base_fields = {f.campo for f in base.campos}
    prophylaxis = knowledge.prophylaxis()

    def check_field(where: str, f: FieldSpec) -> None:
        if f.fonte not in base.fontes:
            out.append(f"{where}/{f.campo}: fonte desconhecida '{f.fonte}'")
        if f.grupo and f.grupo not in base.grupos:
            out.append(f"{where}/{f.campo}: grupo desconhecido '{f.grupo}'")
        if f.valor:
            try:
                re.compile(f.valor)
            except re.error as exc:
                out.append(f"{where}/{f.campo}: expressão `valor` inválida ({exc})")

    def check_reqs(where: str, reqs: dict[str, Requirement], known: set[str]) -> None:
        for key, req in reqs.items():
            if key not in known:
                out.append(f"{where}: obrigatoriedade de campo inexistente '{key}'")
            if req.fonte not in base.fontes:
                out.append(f"{where}/{key}: fonte desconhecida '{req.fonte}'")

    if len(base_fields) != len(base.campos):
        out.append("base: campos repetidos")
    for f in base.campos:
        check_field("base", f)
    for spec, rules in base.especialidades.items():
        if spec not in knowledge.SPECIALTIES:
            out.append(f"base: especialidade desconhecida '{spec}'")
        check_reqs(f"base/especialidades/{spec}", rules.obrigatoriedade, base_fields)
    seen_words: dict[str, str] = {}
    for pid, p in procedures.items():
        own = {f.campo for f in p.campos}
        if p.id != pid:
            out.append(f"{pid}: id '{p.id}' difere do nome do arquivo")
        if p.especialidade not in knowledge.SPECIALTIES:
            out.append(f"{pid}: especialidade desconhecida '{p.especialidade}'")
        if len(own) != len(p.campos) or own & base_fields:
            out.append(f"{pid}: campo repetido (no procedimento ou já existente na base)")
        for f in p.campos:
            check_field(pid, f)
            if f.depois_de and f.depois_de not in base_fields | own:
                out.append(f"{pid}/{f.campo}: depois_de aponta para campo inexistente '{f.depois_de}'")
        check_reqs(pid, p.obrigatoriedade, base_fields | own)
        for option in p.profilaxia:
            for drug in option.split("+"):
                if drug not in prophylaxis:
                    out.append(f"{pid}: profilaxia '{drug}' fora de surgical_prophylaxis.csv")
        for w in p.detectar:
            key = syn(w).strip()
            if key in seen_words and seen_words[key] != pid:
                out.append(f"{pid}: sinônimo de detecção '{w}' repetido em {seen_words[key]}")
            seen_words[key] = pid
    return out


@lru_cache
def surgical_templates() -> TemplateSet:
    folder = _folder()
    with open(folder / "_base.yaml", encoding="utf-8") as fh:
        base = BaseTemplate.model_validate(yaml.safe_load(fh))
    procedures: dict[str, ProcedureTemplate] = {}
    for path in sorted(folder.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        with open(path, encoding="utf-8") as fh:
            procedures[path.stem] = ProcedureTemplate.model_validate(yaml.safe_load(fh))
    errors = problems(base, procedures)
    if errors:
        raise ValueError("Modelos cirúrgicos inválidos: " + "; ".join(errors))
    return TemplateSet(base, procedures)


def surgical_specialties() -> list[str]:
    """Especialidades com modelo cirúrgico (o seletor da tela mostra só estas no modo cirúrgico)."""
    ts = surgical_templates()
    found = {p.especialidade for p in ts.procedures.values()} | set(ts.base.especialidades)
    return [k for k in knowledge.SPECIALTIES if k in found]
