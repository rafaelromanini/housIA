"""Codificação (#18): CID-10 com especificidade, compatibilidade de sexo e idade, CIAP-2 e procedimento do SUS (SIGTAP).

Tudo determinístico e com fonte: cada código sai com a frase literal que o justifica (a fala, ou o registro do
prontuário) e com o que falta para fechar a especificidade. A IA pode propor códigos, mas o que ela propõe passa pelo
mesmo funil (`enrich`, `secondaries`): código inexistente, de outro sexo ou comorbidade que ninguém citou não chega
a nota.

Tabelas (data/reference): `cid_especificidade.csv`, `cid_idade.csv`, `ciap2.csv`, `ciap2_cid10_map.csv`,
`sigtap_procedimentos.csv` e `sigtap_proc_cid.csv` (scripts/build_sigtap.py). Os códigos SIGTAP desta distribuição são
DEMO-*; uma competência real do DATASUS entra pelo mesmo script, sem mudar o código.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from app.consult import knowledge, text
from app.consult.schemas import CidSuggestion, CodigoEspecifico, Highlight, ProcedimentoSus, Segment
from app.reference import _read_csv, icd_entry, icd_problem, norm_icd

if TYPE_CHECKING:
    from app.consult.chart import ChartView
    from app.consult.schemas import Insights, SurgicalReport

FRASE_MAX = 220
CONDITION_REF = re.compile(r"Condition/[\w.-]+")
FAMILY = re.compile(
    r"\b(?:pai|mae|irma|irmao|irmaos|avo|avos|tio|tia|filho|filha|marido|esposa|sogra|sogro|familia|familiar|parente|vizinh[oa])\b"
)
HEDGE = re.compile(r"^(?:se|caso)\b|\b(?:sera que|talvez|quem sabe|e se)\b")
NEGATORS = {"nao", "nunca", "nem", "sem", "nega", "negou", "nenhum", "nenhuma", "jamais", "ausencia", "afastou"}
NEG_BREAKERS = {
    "com",
    "e",
    "mas",
    "tambem",
    "tive",
    "teve",
    "apresenta",
}  # "sem abscesso e com peritonite": nega só o 1º
DENIED_AFTER = re.compile(r"^(?: \w+){0,2}? (?:descartad|afastad|excluid|negativ|ausente|improvavel|nao confirmad)")
SECONDARY_DROP = "comorbidade não citada na consulta e sem efeito na conduta"


# ---------- tabelas ----------
@dataclass(frozen=True)
class SpecificityRule:
    prefix: str
    dimension: str
    terms: tuple[str, ...]
    code: str
    label: str
    missing: str
    source: str


@lru_cache
def specificity_rules() -> tuple[SpecificityRule, ...]:
    return tuple(
        SpecificityRule(
            r["cid_prefixo"],
            r["dimensao"],
            tuple(t for t in r["termos"].split("|") if t),
            r["codigo"],
            r["rotulo"],
            r["falta"].strip(),
            r["fonte"],
        )
        for r in _read_csv("cid_especificidade.csv")
    )


@lru_cache
def age_limits() -> tuple[tuple[str, float | None, float | None], ...]:
    def num(v: str) -> float | None:
        return float(v) if v else None

    return tuple(
        (r["cid_prefixo"], num(r["idade_min_anos"]), num(r["idade_max_anos"])) for r in _read_csv("cid_idade.csv")
    )


@lru_cache
def ciap2_table() -> dict[str, dict[str, str]]:
    return {r["codigo"]: r for r in _read_csv("ciap2.csv")}


@lru_cache
def ciap2_map() -> tuple[dict[str, str], ...]:
    return tuple(_read_csv("ciap2_cid10_map.csv"))


def ciap2_for(cid: str) -> dict[str, str] | None:
    """CIAP-2 mapeado à CID-10 pelo prefixo mais longo: {cid_prefixo, ciap2, descricao, fonte}."""
    cid = (cid or "").upper()
    rows = [r for r in ciap2_map() if cid.startswith(r["cid_prefixo"])]
    if not rows:
        return None
    row = max(rows, key=lambda r: len(r["cid_prefixo"]))
    item = ciap2_table().get(row["ciap2"])
    return {**row, "descricao": item["descricao"] if item else "", "fonte": row["fonte"]} if item else None


@dataclass(frozen=True)
class SigtapProc:
    code: str
    key: str
    description: str
    sex: str
    age_min: float | None
    age_max: float | None
    competence: str
    source: str


@lru_cache
def sigtap_procedures() -> dict[str, SigtapProc]:
    def num(v: str) -> float | None:
        return float(v) if v else None

    return {
        r["chave"]: SigtapProc(
            r["codigo"],
            r["chave"],
            r["descricao"],
            r["sexo"],
            num(r["idade_min_anos"]),
            num(r["idade_max_anos"]),
            r["competencia"],
            r["fonte"],
        )
        for r in _read_csv("sigtap_procedimentos.csv")
    }


@lru_cache
def sigtap_cids() -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for r in _read_csv("sigtap_proc_cid.csv"):
        out.setdefault(r["codigo"], []).append(r["cid_prefixo"])
    return {k: tuple(v) for k, v in out.items()}


# ---------- frase que justifica o código ----------
def _trim(t: str) -> str:
    t = " ".join(t.split())
    return t if len(t) <= FRASE_MAX else t[: FRASE_MAX - 1].rstrip() + "…"


def phrase(refs: list[str], segments: list[Segment], chart: ChartView | None = None) -> str:
    """A fala literal (primeira `Fala/N` citada) ou, sem fala, o texto do diagnóstico registrado no prontuário."""
    by_n = {s.n: s for s in segments}
    for ref in refs:
        if ref.startswith("Fala/") and ref[5:].isdigit() and (s := by_n.get(int(ref[5:]))):
            return _trim(s.texto)
    if chart is not None:
        for _, label, c in chart.conditions:
            if f"Condition/{c['id']}" in refs and label:
                return f"Prontuário: {_trim(label)}"
    return ""


def _statements(segments: list[Segment], split: bool = False) -> list[tuple[Segment, list[str]]]:
    """Falas afirmativas (sem pergunta nem marco), já divididas em orações normalizadas."""
    out = []
    for s in segments:
        if s.falante == "desconhecido" or s.marco or s.expurgada:
            continue
        if not split and ("?" in s.texto or text.is_question(s.texto)):
            continue  # secundário: fala que pergunta nada afirma (conservador)
        # especificidade: "Tem abscesso? Não. É apendicite sem perfuração." mantém só as frases afirmativas
        asserted = [x for x in re.split(r"(?<=[.!?])\s+", s.texto) if x and "?" not in x and not text.is_question(x)]
        if asserted:
            out.append((s, [c for x in asserted for c in text.clauses(x)]))
    return out


def _says(clauses: list[str], terms: tuple[str, ...]) -> bool:
    """Alguma oração diz um dos termos sem negá-lo? Termos que já começam com "sem"/"não" são a própria negação."""
    for clause in clauses:
        if FAMILY.search(clause) or HEDGE.search(clause):  # "minha mãe tem diabetes" é do familiar; "se for" é hipótese
            continue
        hit = text.find(clause, terms)
        if hit and (hit[0].startswith(("sem ", "nao ")) or not _denied(clause, hit[1], hit[2])):
            return True
    return False


def _denied(clause: str, start: int, end: int) -> bool:
    """O termo está negado ("não tenho", "não há sinais de", "não dói nos...") ou descartado ("... descartada")?"""
    if text.negated_span(clause, start, end) or DENIED_AFTER.match(clause[end:]):
        return True
    for word in reversed(clause[:start].split()[-4:]):
        if word in NEGATORS:
            return True
        if word in NEG_BREAKERS:
            break
    return False


# ---------- especificidade ----------
def refine(code: str, segments: list[Segment], with_gaps: bool = True) -> tuple[list[CodigoEspecifico], list[str]]:
    """(códigos mais específicos sustentados pelo ditado, o que falta para fechar a especificidade).

    Um refinamento por dimensão (complicação, lateralidade, agudo × crônico): a primeira regra da tabela que casa numa
    fala afirmativa. Sem nenhuma regra casada e com o código ainda genérico (categoria ou ".9"), a dimensão vira lacuna.
    """
    code = norm_icd(code)
    statements = _statements(segments, split=True)
    found: list[CodigoEspecifico] = []
    gaps: list[str] = []
    dimensions: dict[tuple[str, str], list[SpecificityRule]] = {}
    for rule in specificity_rules():
        if code.startswith(rule.prefix):
            dimensions.setdefault((rule.prefix, rule.dimension), []).append(rule)
    generic = len(code) <= 3 or code.endswith(".9")
    for rules in dimensions.values():
        # o que o ditado afirma vale mais que "sem X": "sem abscesso, com peritonite generalizada" não fecha em K35.8
        missing = next((r.missing for r in rules if r.missing), "")
        rules = sorted(rules, key=lambda r: r.terms[0].startswith(("sem ", "nao ")))
        hit = next(
            (
                (rule, s)
                for rule in rules
                for s, clauses in statements
                if (entry := icd_entry(rule.code)) is not None and entry and _says(clauses, rule.terms)
            ),
            None,
        )
        if hit:
            rule, seg = hit
            entry = icd_entry(rule.code)
            if entry and entry.code != code:
                found.append(
                    CodigoEspecifico(
                        codigo=entry.code, descricao=entry.description, frase=_trim(seg.texto), refs=[f"Fala/{seg.n}"]
                    )
                )
        elif with_gaps and generic:
            if missing:
                gaps.append(missing)
    return found, gaps


# ---------- sexo e idade ----------
def compat_problem(code: str, sex: str, age: float | None) -> str | None:
    """Motivo pelo qual o código não combina com o paciente (sexo da tabela do DATASUS, idade da curadoria) ou None."""
    problem = icd_problem(code, sex)
    if problem and problem != "CID inexistente":
        return problem
    c = norm_icd(code)
    if c and age is not None:
        for prefix, lo, hi in age_limits():
            if c.startswith(prefix) and ((lo is not None and age < lo) or (hi is not None and age > hi)):
                faixa = (
                    f"{int(lo)} a {int(hi)} anos" if lo is not None and hi is not None else f"até {int(hi or 0)} ano(s)"
                )
                return f"CID fora da faixa etária esperada ({faixa}), paciente com {age:g} ano(s)"
    return None


def compat_alerts(items: list[tuple[str, str, list[str]]], sex: str, age: float | None) -> list[Highlight]:
    """Alerta para cada (código, texto, refs) que não combina com o sexo ou a idade do paciente."""
    out = []
    for code, label, refs in items:
        problem = compat_problem(code, sex, age)
        if problem and refs:
            out.append(
                Highlight(
                    texto=f"CID {norm_icd(code) or code} ({label}): {problem}. Conferir o registro.",
                    severidade="atencao",
                    refs=refs,
                    categoria="codificacao",
                )
            )
    return out


# ---------- CID secundário ----------
def _conduct_conditions(ins: Insights) -> dict[str, str]:
    """Condition/id citada por um item que muda a conduta (alerta, medicamento, hipótese, protocolo, pedido)."""
    out: dict[str, str] = {}
    for field, why in (
        ("sugestoes_medicamentos", "alerta de medicamento"),
        ("alertas", "alerta"),
        ("hipoteses", "hipótese"),
        ("protocolos", "protocolo de acompanhamento"),
        ("pedidos", "pedido"),
    ):
        for item in getattr(ins, field, []) or []:
            for ref in CONDITION_REF.findall(json.dumps(item.model_dump(), ensure_ascii=False)):
                out.setdefault(ref, why)
    return out


def _terms_for(icd: str, label: str) -> tuple[str, ...]:
    terms = [t for d in knowledge.conditions() if d.cid[:3] == icd[:3] for t in d.terms]
    if label:
        terms.append(text.norm(label))
    return tuple(dict.fromkeys(t for t in terms if len(t) >= 4))


def secondaries(chart: ChartView, ins: Insights, segments: list[Segment], hypotheses: set[str]) -> list[CidSuggestion]:
    """CID secundário = comorbidade ativa que alguém citou na consulta ou que muda a conduta.
    Comorbidade do prontuário que não entrou na conversa nem mexe na conduta fica fora da nota."""
    conduct = _conduct_conditions(ins)
    statements = _statements(segments)
    out: list[CidSuggestion] = []
    seen: set[str] = set()
    for icd, label, c in chart.conditions:
        code = norm_icd(icd)
        ref = f"Condition/{c['id']}"
        if not code or code in seen or code[:3] in hypotheses:
            continue
        terms = _terms_for(code, label)
        cited = next((s for s, clauses in statements if _says(clauses, terms)), None)
        if cited is None and ref not in conduct:
            continue
        seen.add(code)
        refs = ([f"Fala/{cited.n}"] if cited else []) + [ref]
        out.append(
            CidSuggestion(
                codigo=icd,
                descricao=label,
                tipo="secundario",
                refs=refs,
                frase=_trim(cited.texto) if cited else f"Prontuário: {_trim(label)} (muda a conduta: {conduct[ref]})",
            )
        )
    return out


def allowed_secondary(chart: ChartView, ins: Insights, segments: list[Segment], hypotheses: set[str]) -> set[str]:
    return {norm_icd(s.codigo) for s in secondaries(chart, ins, segments, hypotheses)}


# ---------- enriquecimento ----------
def enrich(
    cids: list[CidSuggestion],
    segments: list[Segment],
    chart: ChartView,
    *,
    ciap: bool = False,
) -> list[CidSuggestion]:
    """Frase literal, códigos mais específicos, lacunas e (na APS) CIAP-2 em cada CID da nota."""
    out = []
    for c in cids:
        found, gaps = refine(c.codigo, segments, with_gaps=c.tipo != "secundario")
        update: dict = {
            "frase": c.frase or phrase(c.refs, segments, chart),
            "mais_especificos": found,
            "lacunas": gaps,
        }
        if ciap and (row := ciap2_for(c.codigo)):
            update["ciap2"] = f"{row['ciap2']} {row['descricao']}"
        out.append(c.model_copy(update=update))
    return out


def drop_unsupported_secondaries(
    cids: list[CidSuggestion], allowed: set[str]
) -> tuple[list[CidSuggestion], list[dict]]:
    """Tira da nota o secundário só do prontuário (sem fala que o cite) que não é citado nem muda a conduta. O que a
    conversa trouxe (tem `Fala/N`) fica: o profissional pode ter ouvido um diagnóstico novo."""
    kept, dropped = [], []
    for c in cids:
        spoken = any(r.startswith("Fala/") for r in c.refs)
        if c.tipo == "secundario" and norm_icd(c.codigo) not in allowed and not spoken:
            dropped.append(
                {"onde": "cids", "texto": c.descricao, "refs_invalidas": [], "motivo": SECONDARY_DROP, "cid": c.codigo}
            )
        else:
            kept.append(c)
    return kept, dropped


# ---------- procedimento SIGTAP no relatório cirúrgico ----------
def attach_sigtap(report: SurgicalReport, sex: str, age: float | None) -> None:
    """Procedimento SIGTAP do relatório: só se constar na tabela. CID principal fora da relação do procedimento (ou
    paciente fora do sexo/idade dele) vira pendência. Recalcula do zero: pode ser chamado depois de cada edição."""
    report.pendencias = [p for p in report.pendencias if p.categoria != "sigtap"]
    report.procedimento_sus = None
    proc = sigtap_procedures().get((report.modelo.id if report.modelo else "") or "")
    if proc is None:
        return
    allowed = sigtap_cids().get(proc.code, ())
    principal = next((c for c in report.cids if c.tipo == "principal"), report.cids[0] if report.cids else None)
    reasons: list[str] = []
    if principal is not None and allowed and not any(norm_icd(principal.codigo).startswith(p) for p in allowed):
        reasons.append(
            f"CID {principal.codigo} não consta na relação do procedimento {proc.code} ({proc.description}); "
            f"compatíveis: {', '.join(allowed)}"
        )
    if proc.sex and sex in ("F", "M") and proc.sex != sex:
        reasons.append(f"procedimento restrito ao sexo {'feminino' if proc.sex == 'F' else 'masculino'}")
    if age is not None and (
        (proc.age_min is not None and age < proc.age_min) or (proc.age_max is not None and age > proc.age_max)
    ):
        reasons.append("paciente fora da faixa etária do procedimento")
    refs = principal.refs if principal else []
    report.procedimento_sus = ProcedimentoSus(
        codigo=proc.code,
        descricao=proc.description,
        compativel=not reasons,
        motivo="; ".join(reasons),
        competencia=proc.competence,
        fonte=proc.source,
        refs=refs,
    )
    if reasons:
        report.pendencias.append(
            Highlight(
                texto=f"Procedimento SIGTAP {proc.code} ({proc.description}) incompatível: {'; '.join(reasons)}.",
                severidade="atencao",
                refs=refs,
                categoria="sigtap",
            )
        )
