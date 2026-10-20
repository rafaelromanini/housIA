"""Motor anti-glosa em dados e prontidão para faturamento do relatório cirúrgico.

As regras NÃO ficam no código: vivem em data/reference/glosa_rules/ (CSV versionados, editáveis pelo hospital):
- `convenios.csv`: convênios (o genérico, um de demonstração e o SUS) e o arquivo de regras de cada um;
- `generico.csv`, `convenio_demo.csv`, `sus_demo.csv`: regras `regra, tipo, condicao, severidade, peso, mensagem, fonte,
  tipo_fonte` (a fonte é um manual, um contrato ou um motivo de glosa da terminologia TISS). Um convênio herda o
  genérico e pode sobrescrever uma regra pelo mesmo id ou somar regras novas;
- `tabela_procedimentos.csv` (auxiliares, duração e anestesias usuais, exigência de autorização, códigos DEMO),
  `tabela_cid_procedimento.csv`, `tabela_opme_procedimento.csv`, `tabela_anestesia.csv`;
- `parametros.yaml`: limiar de "pronto", teto do score com pendência crítica e pesos padrão.

O código só sabe AVALIAR cada `tipo` de regra (os avaliadores abaixo). TUSS/CBHPM/SIGTAP não são licenciados aqui:
os códigos de procedimento são DEMO-TUSS-* e DEMO-SIGTAP-*, e as tabelas CID × procedimento e OPME × procedimento são
curadoria de demonstração, com a coluna `fonte` dizendo isso.

O motor avalia o `SurgicalReport` (campos, OPME, CID, modelo): por isso roda ao vivo a cada fala e de novo na revisão,
depois que o cirurgião edita o relatório. O score é a fração do peso das verificações aplicáveis que passaram
(verificação ainda impossível de avaliar não conta); com pendência crítica ele não passa do teto dos parâmetros.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache

import yaml

from app.config import get_settings
from app.consult import knowledge
from app.consult.schemas import BillingItem, Readiness, Segment, SurgicalReport
from app.consult.surgical_checks import (
    DAY,
    END_WORDS,
    INCISION_WORDS,
    dictated_duration,
    event_clock,
    fmt_h,
    hhmm,
)
from app.consult.templates import surgical_templates
from app.consult.text import norm
from app.reference import _read_csv

FOLDER = "glosa_rules"
RULE_TYPES = {
    "campo",
    "completude_modelo",
    "cid_presente",
    "cid_procedimento",
    "procedimento_codificado",
    "opme_procedimento",
    "opme_conciliacao",
    "opme_rastreio",
    "auxiliares",
    "horario_coerencia",
    "duracao_faixa",
    "anestesia_procedimento",
    "equipe_papel",
    "guia_autorizacao",
}
SEVERITIES = ("critico", "atencao", "info")
SOURCE_KINDS = ("manual", "contrato", "motivo_glosa_tiss")
ROLES = {
    # papel: (palavras na equipe, falante que o confirma)
    "anestesista": (("anestesista", "anestesiologista"), "anestesista"),
    "cirurgiao": (("cirurgiao", "cirurgia:"), "medico"),
    "instrumentador": (("instrumentador",), "instrumentador"),
}
TITLES = re.compile(r"\b(?:dr|dra|doutor|doutora|enf|enfermeiro|enfermeira)\b")
COUNT_WORDS = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4}
ORDINALS = r"(?:primeiro|segundo|terceiro|quarto|1o|2o|3o|4o)"
DURATION_TOLERANCE_MIN = 15
NO_AUXILIAR = re.compile(
    r"(?<![a-z])(?:sem|nao (?:ha|houve|teve|tem|foi|contou|precisou)(?: de)?|nenhum)\s+(?:\w+\s+){0,2}?auxiliar(?:es)?"
    r"|auxiliar(?:es)?\s*[:\-]?\s*(?:nenhum|nenhuma|ninguem|nao)(?![a-z])"
)


# ---------- dados ----------
@dataclass(frozen=True)
class Rule:
    regra: str
    tipo: str
    condicao: str
    severidade: str
    peso: int
    mensagem: str
    fonte: str
    tipo_fonte: str


@dataclass(frozen=True)
class Convenio:
    id: str
    rotulo: str
    arquivo: str
    codigos: str  # tuss_demo | sigtap_demo


@dataclass(frozen=True)
class ProcRow:
    id: str
    descricao: str
    tuss: str
    sigtap: str
    aux_min: int
    aux_max: int
    dur_min: int
    dur_max: int
    anestesias: tuple[str, ...]
    exige_autorizacao: bool
    fonte: str


@lru_cache
def parametros() -> dict:
    with open(get_settings().data_dir / "reference" / FOLDER / "parametros.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache
def convenios() -> dict[str, Convenio]:
    return {
        r["convenio"]: Convenio(r["convenio"], r["rotulo"], r["arquivo"], r["codigos"])
        for r in _read_csv(f"{FOLDER}/convenios.csv")
    }


def _rules_file(name: str) -> list[Rule]:
    default = parametros()["pesos_padrao"]
    return [
        Rule(
            r["regra"],
            r["tipo"],
            r["condicao"],
            r["severidade"],
            int(r["peso"] or default[r["severidade"]]),
            r["mensagem"],
            r["fonte"],
            r["tipo_fonte"],
        )
        for r in _read_csv(f"{FOLDER}/{name}")
    ]


@lru_cache
def rules_for(convenio: str) -> tuple[Rule, ...]:
    """Regras do convênio: as do genérico, com as do convênio sobrescrevendo pelo id e somando as novas."""
    merged = {r.regra: r for r in _rules_file(convenios()["generico"].arquivo)}
    if convenio != "generico":
        merged |= {r.regra: r for r in _rules_file(convenios()[convenio].arquivo)}
    return tuple(merged.values())


def rules_for_kinds(convenio: str, kinds: list[str]) -> list[str]:
    """Ids das regras de conciliação de OPME do convênio que avaliam algum dos tipos de problema dados."""
    wanted = set(kinds)
    if convenio not in convenios():
        convenio = "generico"
    return [
        r.regra for r in rules_for(convenio) if r.tipo == "opme_conciliacao" and wanted & set(r.condicao.split("|"))
    ]


@lru_cache
def procedures() -> dict[str, ProcRow]:
    return {
        r["procedimento"]: ProcRow(
            r["procedimento"],
            r["descricao"],
            r["codigo_tuss_demo"],
            r["codigo_sigtap_demo"],
            int(r["auxiliares_min"]),
            int(r["auxiliares_max"]),
            int(r["duracao_min"]),
            int(r["duracao_max"]),
            tuple(r["anestesias"].split("|")),
            r["exige_autorizacao"] == "sim",
            r["fonte"],
        )
        for r in _read_csv(f"{FOLDER}/tabela_procedimentos.csv")
    }


@lru_cache
def cid_table() -> dict[str, tuple[tuple[str, ...], str]]:
    return {
        r["procedimento"]: (tuple(r["cid_prefixos"].split("|")), r["fonte"])
        for r in _read_csv(f"{FOLDER}/tabela_cid_procedimento.csv")
    }


@lru_cache
def opme_table() -> tuple[tuple[str, tuple[str, ...], str, str], ...]:
    return tuple(
        (r["procedimento"], tuple(r["opme"].split("|")), r["relacao"], r["fonte"])
        for r in _read_csv(f"{FOLDER}/tabela_opme_procedimento.csv")
    )


@lru_cache
def anesthesia_types() -> dict[str, tuple[str, tuple[str, ...]]]:
    return {
        r["tipo"]: (r["rotulo"], tuple(w for w in r["palavras"].split("|") if w))
        for r in _read_csv(f"{FOLDER}/tabela_anestesia.csv")
    }


def clear_cache() -> None:
    for fn in (parametros, convenios, rules_for, procedures, cid_table, opme_table, anesthesia_types):
        fn.cache_clear()


def convenio_list() -> list[dict]:
    return [{"value": c.id, "label": c.rotulo, "codigos": c.codigos} for c in convenios().values()]


def problems() -> list[str]:
    """Consistência dos dados (os testes exigem lista vazia): tipos, severidades, campos, procedimentos e códigos."""
    out: list[str] = []
    ts = surgical_templates()
    fields = {f.campo for tpl_id in [None, *ts.procedures] for f in ts.resolve(tpl_id).fields}
    catalog = {d.code for d in knowledge.opme_catalog()}
    kinds = set(anesthesia_types())
    for conv in convenios().values():
        for r in _rules_file(conv.arquivo):
            where = f"{conv.arquivo}:{r.regra}"
            if r.tipo not in RULE_TYPES:
                out.append(f"{where}: tipo desconhecido {r.tipo}")
            if r.severidade not in SEVERITIES:
                out.append(f"{where}: severidade inválida {r.severidade}")
            if r.tipo_fonte not in SOURCE_KINDS:
                out.append(f"{where}: tipo_fonte inválido {r.tipo_fonte}")
            if not r.fonte or not r.mensagem:
                out.append(f"{where}: regra sem fonte ou sem mensagem")
            if r.tipo == "campo" and r.condicao not in fields:
                out.append(f"{where}: campo {r.condicao} não existe em nenhum modelo")
            if r.tipo == "equipe_papel" and r.condicao not in ROLES:
                out.append(f"{where}: papel desconhecido {r.condicao}")
    for pid, row in procedures().items():
        if pid not in ts.procedures:
            out.append(f"tabela_procedimentos: {pid} não é um modelo cirúrgico")
        if not set(row.anestesias) <= kinds:
            out.append(f"tabela_procedimentos: anestesia desconhecida em {pid}")
        if not (row.tuss.startswith("DEMO-") and row.sigtap.startswith("DEMO-")):
            out.append(f"tabela_procedimentos: código de {pid} deve ser DEMO-* (sem tabela licenciada)")
        if row.aux_min > row.aux_max or row.dur_min > row.dur_max:
            out.append(f"tabela_procedimentos: faixa invertida em {pid}")
    missing = set(ts.procedures) - set(procedures())
    if missing:
        out.append(f"tabela_procedimentos: sem linha para {sorted(missing)}")
    for pid, (prefixes, fonte) in cid_table().items():
        if pid not in ts.procedures or not fonte or not all(re.fullmatch(r"[A-Z]\d{0,2}", p) for p in prefixes):
            out.append(f"tabela_cid_procedimento: linha inválida {pid}")
    for pid, codes_, relation, fonte in opme_table():
        if pid not in ts.procedures or relation not in ("incompativel", "esperado") or not fonte:
            out.append(f"tabela_opme_procedimento: linha inválida {pid}")
        out += [f"tabela_opme_procedimento: {c} fora do catálogo" for c in codes_ if c not in catalog]
    return out


# ---------- leitura do relatório ----------
def count_auxiliares(text: str) -> int:
    """Auxiliares ditados na equipe: "dois auxiliares", "primeiro e segundo auxiliar", "auxiliares: Dr. X e Dra. Y"."""
    t = norm(text)
    t = re.sub(r"\b(dr|dra|sr|sra|enf)\.", r"\1", t)
    t = NO_AUXILIAR.sub(" ", t)  # "sem auxiliar", "não houve auxiliar", "auxiliares: nenhum"
    if "auxiliar" not in t:
        return 0
    found = len(set(re.findall(rf"({ORDINALS})\s+auxiliar", t)))
    for m in re.finditer(r"(\d+|um|uma|dois|duas|tres|quatro)\s+auxiliares?", t):
        found = max(found, int(m.group(1)) if m.group(1).isdigit() else COUNT_WORDS[m.group(1)])
    for m in re.finditer(r"auxiliar(?:es)?\s*(?:cirurgic[oa]s?\s*)?[:\-]?\s*([^.;]*)", t):
        tail = re.split(
            r"\b(?:anestesista|anestesiologista|instrumentador\w*|circulante|cirurgiao|cirurgia)\b", m.group(1)
        )[0]
        names = [n for n in re.split(r",|\be\b", tail) if TITLES.sub("", n).strip(" .:")]
        found = max(found, len(names))
    return max(found, 1)


def _pseudo_segment(text: str) -> list[Segment]:
    return [Segment(n=1, t=0, falante="medico", texto=text)]


def clock_summary(horario_text: str, dictation: list[Segment] | tuple = ()) -> dict:
    """Início, término e duração (minutos). O campo do relatório vale primeiro; o ditado completa o que faltar."""
    segs = [*_pseudo_segment(horario_text), *dictation]
    start = event_clock(segs, INCISION_WORDS, END_WORDS)
    end = event_clock(segs, END_WORDS, INCISION_WORDS)
    said = dictated_duration(segs)
    computed = (end[0] - start[0]) % DAY if start and end else None
    return {
        "inicio": start[0] if start else None,
        "fim": end[0] if end else None,
        "duracao_dita": said[0] if said else None,
        "duracao_calculada": computed,
        "duracao": computed if computed else (said[0] if said else None),
    }


# ---------- avaliação ----------
@dataclass
class Ctx:
    report: SurgicalReport
    segments: list[Segment]
    convenio: Convenio
    numero_guia: str | None
    covered: set[str] = field(default_factory=set)  # campos já tratados por uma regra `campo`

    def __post_init__(self) -> None:
        self.speakers = {s.falante for s in self.segments}
        self.fields = {f.campo: f for f in self.report.campos}
        pid = self.report.modelo.id if self.report.modelo else None
        self.proc: ProcRow | None = procedures().get(pid or "")
        self.lines = {line.codigo: line for line in self.report.opme}

    def text(self, campo: str) -> str:
        f = self.fields.get(campo)
        return f.texto.strip() if f else ""

    def refs(self, campo: str) -> list[str]:
        f = self.fields.get(campo)
        return list(f.refs) if f else []


@dataclass
class Check:
    ok: bool
    vars: dict = field(default_factory=dict)
    refs: list[str] = field(default_factory=list)
    campo: str | None = None
    fonte: str | None = None  # fonte própria (linha da tabela ou do campo) no lugar da fonte da regra


Evaluator = Callable[[Ctx, Rule], list[Check]]


def _campo(ctx: Ctx, rule: Rule) -> list[Check]:
    f = ctx.fields.get(rule.condicao)
    return [Check(bool(f.texto.strip()), {"rotulo": f.rotulo}, f.refs, f.campo)] if f else []


def _completude(ctx: Ctx, rule: Rule) -> list[Check]:
    return [
        Check(bool(f.texto.strip()), {"rotulo": f.rotulo, "motivo": f.motivo or ""}, f.refs, f.campo, f.fonte)
        for f in ctx.report.campos
        if f.obrigatorio and f.campo not in ctx.covered
    ]


def _cid_presente(ctx: Ctx, rule: Rule) -> list[Check]:
    return [Check(bool(ctx.report.cids), {}, [r for c in ctx.report.cids for r in c.refs])]


def _codificado(ctx: Ctx, rule: Rule) -> list[Check]:
    return [Check(ctx.proc is not None)]


def _cid_procedimento(ctx: Ctx, rule: Rule) -> list[Check]:
    if not ctx.proc or not ctx.report.cids or ctx.proc.id not in cid_table():
        return []
    prefixes, fonte = cid_table()[ctx.proc.id]
    principal = next((c for c in ctx.report.cids if c.tipo == "principal"), ctx.report.cids[0])
    ok = any(principal.codigo.upper().startswith(p) for p in prefixes)
    return [
        Check(
            ok,
            {"cid": principal.codigo, "procedimento": ctx.proc.descricao, "compativeis": ", ".join(prefixes)},
            principal.refs,
            fonte=fonte,
        )
    ]


def _in_use(ctx: Ctx) -> bool:
    """Há OPME citada ou lida: sem isso a conciliação ainda não tem o que avaliar (não conta no score)."""
    return any(line.citado or line.consumido for line in ctx.lines.values())


def _opme_procedimento(ctx: Ctx, rule: Rule) -> list[Check]:
    if not ctx.proc or (rule.condicao == "incompativel" and not _in_use(ctx)):
        return []
    used = {code: line for code, line in ctx.lines.items() if line.citado or line.consumido}
    checks: list[Check] = []
    for pid, codes_, relation, fonte in opme_table():
        if pid != ctx.proc.id or relation != rule.condicao:
            continue
        names = {d.code: d.description for d in knowledge.opme_catalog()}
        for code in codes_:
            line = used.get(code)
            if relation == "incompativel":
                if line:
                    checks.append(
                        Check(
                            False,
                            {"material": line.descricao, "procedimento": ctx.proc.descricao},
                            line.refs,
                            fonte=fonte,
                        )
                    )
            elif not line:
                checks.append(
                    Check(False, {"material": names.get(code, code), "procedimento": ctx.proc.descricao}, fonte=fonte)
                )
        if relation == "incompativel" and not checks and ctx.lines:
            checks.append(Check(True))
        if relation == "esperado" and not checks:
            checks.append(Check(True))
    return checks


def _opme_conciliacao(ctx: Ctx, rule: Rule) -> list[Check]:
    from app.consult import opme

    if not _in_use(ctx):
        return []
    wanted = set(rule.condicao.split("|"))
    tracking = any(line.consumido or line.lotes for line in ctx.lines.values())
    checks = [
        Check(False, {"material": line.descricao, "detalhe": text}, refs)
        for line in ctx.lines.values()
        for kind, text, _sev, refs in opme.issues(line, tracking)
        if kind in wanted
    ]
    return checks or [Check(True)]


def _opme_rastreio(ctx: Ctx, rule: Rule) -> list[Check]:
    implants = [line for line in ctx.lines.values() if line.implantavel and (line.citado or line.consumido)]
    if not implants:
        return []
    return [Check(bool(line.lotes), {"material": line.descricao}, line.refs) for line in implants]


def _auxiliares(ctx: Ctx, rule: Rule) -> list[Check]:
    text = ctx.text("equipe")
    if not ctx.proc or not text:
        return []
    n = count_auxiliares(text)
    p = ctx.proc
    refs = ctx.refs("equipe")
    if n < p.aux_min:
        detail = (
            f"Auxiliares: {n} identificado(s) na equipe × {p.aux_min} previsto(s) para {p.descricao}. Identificar "
            "o(s) auxiliar(es) ou o honorário é glosado."
        )
        return [Check(False, {"detalhe": detail}, refs, fonte=p.fonte)]
    if n > p.aux_max:
        detail = (
            f"Auxiliares: {n} identificado(s) na equipe × máximo de {p.aux_max} previsto(s) para {p.descricao}. "
            "Justificar o excedente ou ele é glosado."
        )
        return [Check(False, {"detalhe": detail}, refs, fonte=p.fonte)]
    return [Check(True, {}, refs, fonte=p.fonte)]


def _horario(ctx: Ctx, rule: Rule) -> list[Check]:
    """`completo`: início e término ditados; `coerencia`: término depois do início e duração que bate."""
    text = ctx.text("horario")
    if not text:
        return []
    c = clock_summary(text, ctx.segments)
    refs = ctx.refs("horario")
    partial = (c["inicio"] is None) != (c["fim"] is None)
    if rule.condicao == "completo":
        if not partial:
            return [Check(True, {}, refs)]
        which = "início" if c["inicio"] is None else "término"
        return [Check(False, {"detalhe": f"Horário de {which} não ditado: sem os dois a duração não fecha."}, refs)]
    if partial:
        return []
    if c["inicio"] is not None and c["duracao_calculada"] == 0:
        detail = f"Horários incoerentes: início e término iguais ({hhmm(c['inicio'])})."
        return [Check(False, {"detalhe": detail}, refs)]
    said, computed = c["duracao_dita"], c["duracao_calculada"]
    if said and computed and abs(said - computed) > DURATION_TOLERANCE_MIN:
        detail = (
            f"Duração ditada ({fmt_h(said)}) não bate com início e término ({hhmm(c['inicio'])}–{hhmm(c['fim'])}, "
            f"{fmt_h(computed)}): conferir os horários."
        )
        return [Check(False, {"detalhe": detail}, refs)]
    return [Check(True, {}, refs)] if c["duracao"] is not None else []


def _duracao_faixa(ctx: Ctx, rule: Rule) -> list[Check]:
    text = ctx.text("horario")
    if not ctx.proc or not text:
        return []
    duration = clock_summary(text, ctx.segments)["duracao"]
    if duration is None:
        return []
    p = ctx.proc
    ok = p.dur_min <= duration <= p.dur_max
    detail = (
        f"Duração de {fmt_h(duration)} fora da faixa usual de {fmt_h(p.dur_min)} a {fmt_h(p.dur_max)} para "
        f"{p.descricao}: conferir os horários (glosa por tempo)."
    )
    return [Check(ok, {"detalhe": detail}, ctx.refs("horario"), fonte=p.fonte)]


def _anestesia(ctx: Ctx, rule: Rule) -> list[Check]:
    text = norm(ctx.text("anestesia"))
    if not ctx.proc or not text:
        return []
    said = [
        k
        for k, (_label, words) in anesthesia_types().items()
        if any(re.search(rf"(?<![a-z]){re.escape(w)}", text) for w in words)
    ]
    if not said:
        return []
    p = ctx.proc
    ok = bool(set(said) & set(p.anestesias))
    labels = anesthesia_types()
    detail = (
        f"Anestesia ({', '.join(labels[k][0].lower() for k in said)}) não é usual em {p.descricao} (usuais: "
        f"{', '.join(labels[k][0].lower() for k in p.anestesias)}): conferir o registro anestésico."
    )
    return [Check(ok, {"detalhe": detail}, ctx.refs("anestesia"), fonte=p.fonte)]


def _equipe_papel(ctx: Ctx, rule: Rule) -> list[Check]:
    words, speaker = ROLES[rule.condicao]
    text = norm(ctx.text("equipe"))
    ok = speaker in ctx.speakers or any(w in text for w in words)
    return [Check(ok, {}, ctx.refs("equipe"))]


def _guia(ctx: Ctx, rule: Rule) -> list[Check]:
    if not ctx.proc or not ctx.proc.exige_autorizacao:
        return []
    ok = bool(ctx.numero_guia) or any(line.autorizado for line in ctx.lines.values())
    return [Check(ok, {"procedimento": ctx.proc.descricao}, fonte=ctx.proc.fonte)]


EVALUATORS: dict[str, Evaluator] = {
    "campo": _campo,
    "completude_modelo": _completude,
    "cid_presente": _cid_presente,
    "procedimento_codificado": _codificado,
    "cid_procedimento": _cid_procedimento,
    "opme_procedimento": _opme_procedimento,
    "opme_conciliacao": _opme_conciliacao,
    "opme_rastreio": _opme_rastreio,
    "auxiliares": _auxiliares,
    "horario_coerencia": _horario,
    "duracao_faixa": _duracao_faixa,
    "anestesia_procedimento": _anestesia,
    "equipe_papel": _equipe_papel,
    "guia_autorizacao": _guia,
}


class _Vars(dict):
    def __missing__(self, key: str) -> str:
        return ""


def evaluate(
    report: SurgicalReport,
    *,
    segments: list[Segment] | tuple = (),
    convenio: str = "generico",
    numero_guia: str | None = None,
) -> Readiness:
    """Aplica as regras do convênio ao relatório: pendências com fonte e o score de prontidão (0–100)."""
    if convenio not in convenios():
        convenio = "generico"
    conv = convenios()[convenio]
    rules = rules_for(convenio)
    ctx = Ctx(report, list(segments), conv, numero_guia, {r.condicao for r in rules if r.tipo == "campo"})
    params = parametros()
    pending: list[BillingItem] = []
    total = earned = n_checks = n_ok = 0
    for rule in rules:
        for chk in EVALUATORS[rule.tipo](ctx, rule):
            total += rule.peso
            n_checks += 1
            if chk.ok:
                earned += rule.peso
                n_ok += 1
                continue
            if rule.fonte in ("@campo", "@tabela"):
                source = chk.fonte or rule.fonte
            else:
                source = f"{rule.fonte} · {chk.fonte}" if chk.fonte else rule.fonte
            pending.append(
                BillingItem(
                    regra=rule.regra,
                    tipo=rule.tipo,
                    severidade=rule.severidade,  # type: ignore[arg-type]
                    texto=rule.mensagem.format_map(_Vars(chk.vars)),
                    fonte=source,
                    campo=chk.campo,
                    refs=chk.refs,
                    peso=rule.peso,
                )
            )
    order = {"critico": 0, "atencao": 1, "info": 2}
    pending.sort(key=lambda p: (order[p.severidade], -p.peso))
    critical = sum(p.severidade == "critico" for p in pending)
    score = round(100 * earned / total) if total else 0
    if critical:
        score = min(score, params["teto_com_critica"])
    proc = ctx.proc
    return Readiness(
        score=score,
        pronto=critical == 0 and score >= params["limiar_pronto"],
        convenio=conv.id,
        convenio_rotulo=conv.rotulo,
        regras_avaliadas=n_checks,
        regras_ok=n_ok,
        criticas=critical,
        atencoes=sum(p.severidade == "atencao" for p in pending),
        pendencias=pending,
        procedimento=proc.descricao if proc else None,
        codigo_demo=(proc.sigtap if conv.codigos == "sigtap_demo" else proc.tuss) if proc else None,
        numero_guia=numero_guia,
        versao=params["versao"],
    )


def snapshot(r: Readiness) -> dict:
    """Resumo para o audit: o que estava pendente (ids das regras, nunca texto clínico) e o score."""
    return {
        "score": r.score,
        "pronto": r.pronto,
        "criticas": r.criticas,
        "atencoes": r.atencoes,
        "pendencias": [{"regra": p.regra, "severidade": p.severidade, "campo": p.campo} for p in r.pendencias],
    }


def resolved(before: dict | None, after: dict) -> list[str]:
    """Regras que estavam pendentes antes e já não estão: as glosas evitadas."""
    now = {(p["regra"], p["campo"]) for p in after["pendencias"]}
    return sorted({p["regra"] for p in (before or {}).get("pendencias", []) if (p["regra"], p["campo"]) not in now})
