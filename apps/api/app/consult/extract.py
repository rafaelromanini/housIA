"""Antecedentes, cirurgias e exames ditos na conversa viram dados estruturados (camada determinística).

- Antecedente pessoal: `condition_lexicon.csv` (termo popular ou técnico → CID-10) + expressão de tempo ("há cinco
  anos", "desde 2018", "desde criança"), conciliado com `Condition.onset` do prontuário.
- Cirurgia prévia: `procedure_lexicon.csv` + ano e intercorrência, conciliada com `Procedure`.
- Exame relatado: "meu colesterol deu 250", comparado com a série de `Observation` do prontuário.
- RCRI (índice de Lee revisado) para a cirurgia planejada, com cada ponto ligado ao recurso FHIR que o sustenta.

Só vale fala do paciente (ou do acompanhante falando dele): pergunta do médico, hipótese ("tenho medo de ter
diabetes"), antecedente da família e negativa não geram item.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from app.consult import knowledge
from app.consult.chart import ChartView, fmt_date, fmt_num
from app.consult.schemas import ExamInfo, HistoryItem, Rcri, RcriItem, Segment, SurgeryItem, fala_ref
from app.consult.text import CLAUSE_SPLIT, HYPOTHETICAL, keyword_status, listener_question, negated_span, norm
from app.timeline.record import parse_dt

PATIENT_SIDE = ("paciente", "acompanhante")
YEAR_TOLERANCE = 3  # diferença (anos) entre o início relatado e o do prontuário que ainda conta como o mesmo
EXAM_TOLERANCE = 0.15  # valor relatado até 15% do último do prontuário = consistente
NUMBER_WORDS = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8,
    "nove": 9, "dez": 10, "onze": 11, "doze": 12, "treze": 13, "quatorze": 14, "quinze": 15, "vinte": 20,
    "trinta": 30, "quarenta": 40,
}  # fmt: skip
THIRD_PERSON = re.compile(r"(?<!\w)(ela|ele|dela|dele|a paciente|o paciente)(?!\w)")
FAMILY = re.compile(
    r"(?<!\w)(minha|meu|minhas|meus|a|o) ?(mae|pai|irma|irmao|avo|avos|tia|tio|filha|filho|marido|esposa)(?!\w)"
)
# o que o paciente diz sem afirmar que tem: medo, dúvida, suspeita
DOUBT = re.compile(
    r"(?<!\w)(medo|receio|susto|sera que|desconfio|suspeito|suspeita|achando|acha que|acho que|pode ser|vai que"
    r"|tenho que|preciso|quero saber|me diga|explica|o que e)(?!\w)"
)
# cirurgia ainda por fazer: "vou operar a vesícula", "a cirurgia de varizes está marcada"
FUTURE = re.compile(
    r"(?<!\w)(vou|vai|quer|querem|preciso|precisa|precisar|marcar|marcad[ao]|agendar|agendad[ao]|programar|programad[ao]"
    r"|indicou|indicad[ao]|pretendo|penso em|operar|posso|devo|talvez|futura|proxim[ao]|semana que vem"
    r"|mes que vem)(?!\w)"
)
YEAR = re.compile(r"(?<!\d)(19[3-9]\d|20[0-4]\d)(?!\d)")
AGO = re.compile(
    r"(?<!\w)(?:ha|faz|fazem|tem|ja faz)\s+"
    r"(?:(?:cerca de|mais ou menos|umas|uns|uma|um|quase|pouco mais de|mais de)\s+)*"
    r"(\d+|" + "|".join(NUMBER_WORDS) + r")\s+(anos?|meses|mes)(?!\w)"
)
CHILDHOOD = re.compile(
    r"(?<!\w)(?:desde|de|quando era|na|logo na)\s+(?:a\s+)?"
    r"(crianca|pequen[ao]|infancia|nascenca|nascimento|bebe|menin[ao]|jovem"
    r"|adolescencia|adolescente)(?!\w)"
)
AGE_AT = re.compile(r"(?<!\w)(?:desde os|aos|quando tinha|com)\s+(\d{1,2})\s+anos(?!\w)")
LAST_YEAR = re.compile(r"(?<!\w)ano passado(?!\w)|(?<!\w)ano retrasado(?!\w)|(?<!\w)(?:esse|este) ano(?!\w)")
COMPLICATIONS = (
    (("sangramento", "hemorragia", "sangrei muito", "perdi muito sangue", "sangrou muito"), "sangramento"),
    (
        (
            "reacao a anestesia", "reacao na anestesia", "problema com a anestesia", "problema na anestesia",
            "alergia a anestesia", "passei mal na anestesia", "acordei durante", "anestesia deu problema",
        ),
        "reação ou intercorrência com a anestesia",
    ),
    (("infeccao", "infeccionou"), "infecção"),
    (("trombose", "embolia"), "trombose ou embolia"),
    (("transfusao", "precisei de sangue", "tomei sangue"), "transfusão"),
    (("fui pra uti", "fui para a uti", "fiquei na uti", "ficou na uti"), "internação em UTI"),
    (("abriu o ponto", "abriram os pontos", "deiscencia"), "deiscência da ferida"),
    (("complicacao", "complicacoes", "intercorrencia", "deu problema"), "complicação não detalhada"),
)  # fmt: skip
# cirurgia de alto risco no índice de Lee: intraperitoneal, intratorácica ou vascular suprainguinal
RCRI_HIGH_RISK = (
    "laparotomia", "laparoscopia", "videolaparoscop", "colecistectomia", "vesicula", "colectomia", "gastrectomia",
    "esofagectomia", "hepatectomia", "pancreat", "esplenectomia", "histerectomia", "apendicectomia", "apendice",
    "toracotomia", "lobectomia", "pneumonectomia", "aneurisma de aorta", "aorta", "endarterectomia", "carotida",
    "revascularizacao periferica", "bypass femoro", "intestino", "gastroplastia", "bariatrica",
)  # fmt: skip
RCRI_SOURCE = (
    "Lee et al., Circulation 1999 (índice de risco cardíaco revisado, RCRI); taxas de eventos cardíacos maiores de "
    "Duceppe et al., Can J Cardiol 2017 (diretriz canadense de avaliação cardiovascular perioperatória)"
)
RCRI_CLASSES = ((0, "I", 3.9), (1, "II", 6.0), (2, "III", 10.1), (3, "IV", 15.0))


# ---------- léxicos ----------
def _compile(terms: dict[str, object]) -> tuple[re.Pattern, dict[str, object]]:
    """Alternância de termos (mais longos primeiro): início de palavra; termos curtos (≤ 4 letras) palavra inteira."""
    ordered = sorted(terms, key=len, reverse=True)
    parts = [re.escape(t) + (r"(?![a-z0-9])" if len(t) <= 4 else r"[a-z0-9]*") for t in ordered]
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + ")"), terms


@lru_cache
def _condition_lexicon() -> tuple[re.Pattern, dict[str, object]]:
    return _compile({t: d for d in knowledge.conditions() for t in d.terms})


@lru_cache
def _procedure_lexicon() -> tuple[re.Pattern, dict[str, object]]:
    return _compile({t: d for d in knowledge.procedures() for t in d.terms})


def _lookup(table: dict[str, object], token: str):
    """Termo do léxico que casa com a palavra encontrada (a palavra pode ter sufixo: "asmatica")."""
    if token in table:
        return table[token]
    for term in sorted(table, key=len, reverse=True):
        if token.startswith(term):
            return table[term]
    return None


@dataclass
class Span:
    start: int
    end: int
    definition: object
    clause: tuple[int, int]  # limites da oração no texto da fala


def _outer(spans: list[Span]) -> list[Span]:
    """Só o termo mais longo quando um contém o outro ("diabetes tipo 1" × "diabetes")."""
    return [sp for sp in spans if not any(o is not sp and o.start <= sp.start and sp.end <= o.end for o in spans)]


def _clause_bounds(text: str) -> list[tuple[int, int]]:
    bounds, last = [], 0
    for m in CLAUSE_SPLIT.finditer(f" {text} "):
        bounds.append((last, max(m.start() - 1, last)))
        last = max(m.end() - 1, 0)
    bounds.append((last, len(text)))
    return [(a, b) for a, b in bounds if b > a]


def _spans(text: str, lexicon: tuple[re.Pattern, dict[str, object]]) -> list[Span]:
    pattern, table = lexicon
    bounds = _clause_bounds(text)
    out = []
    for m in pattern.finditer(text):
        d = _lookup(table, m.group(0))
        if d is None:
            continue
        clause = next(((a, b) for a, b in bounds if a <= m.start() < b), (0, len(text)))
        out.append(Span(m.start(), m.end(), d, clause))
    return out


# ---------- tempo ----------
@dataclass
class When:
    """Quando começou: texto para mostrar e o intervalo de anos em que pode ter começado."""

    texto: str
    lo: int
    hi: int

    @property
    def year(self) -> int:
        return (self.lo + self.hi) // 2


def _number(token: str) -> int:
    return int(token) if token.isdigit() else NUMBER_WORDS[token]


def parse_when(piece: str, now_year: int, birth_year: int) -> When | None:
    """Expressão de tempo dentro do trecho da fala: "há cinco anos", "desde 2018", "desde criança", "aos 40 anos"."""
    if m := CHILDHOOD.search(piece):
        word = m.group(1)
        if word in ("jovem", "adolescencia", "adolescente"):
            return When("desde a adolescência", birth_year + 12, birth_year + 20)
        return When("desde a infância", birth_year, birth_year + 12)
    if m := AGE_AT.search(piece):
        year = birth_year + int(m.group(1))
        return When(f"aos {m.group(1)} anos (≈ {year})", year, year)
    if m := AGO.search(piece):
        n = _number(m.group(1))
        years = n if m.group(2).startswith("ano") else max(round(n / 12), 0)
        if now_year - years < birth_year:  # "há 200 anos": antes de a pessoa nascer não vale
            return None
        unit = "ano" if m.group(2).startswith("ano") else "mes" if n == 1 else "mes"
        label = f"há {n} {'ano' if n == 1 else 'anos'}" if unit == "ano" else f"há {n} {'mês' if n == 1 else 'meses'}"
        return When(f"{label} (≈ {now_year - years})", now_year - years, now_year - years)
    if m := YEAR.search(piece):
        return When(f"em {m.group(1)}", int(m.group(1)), int(m.group(1)))
    if m := LAST_YEAR.search(piece):
        year = now_year - (2 if "retrasado" in m.group(0) else 0 if "esse" in m.group(0) or "este" in m.group(0) else 1)
        return When(f"em {year}", year, year)
    return None


# ---------- extração ----------
@dataclass
class Extraction:
    antecedentes: list[HistoryItem] = field(default_factory=list)
    cirurgias: list[HistoryItem] = field(default_factory=list)
    exames: list[ExamInfo] = field(default_factory=list)

    @property
    def matched_chart_refs(self) -> set[str]:
        """Recursos do prontuário que um relato já cobre (o item do prontuário sai da lista, sem duplicar)."""
        return {
            r
            for i in (*self.antecedentes, *self.cirurgias)
            if i.conciliacao
            for r in i.refs
            if "/" in r and not r.startswith("Fala/")
        }


def _speaker_ok(s: Segment, text: str) -> bool:
    return s.falante == "paciente" or (s.falante == "acompanhante" and bool(THIRD_PERSON.search(text)))


def _asserted(text: str, span: Span, piece_before: str) -> bool:
    """O paciente afirma (sem negação, pergunta, dúvida, hipótese ou parente) o que o termo diz?"""
    a, b = span.clause
    clause = text[a:b]
    rel_s, rel_e = span.start - a, span.end - a
    if negated_span(clause, rel_s, rel_e):
        return False
    before = clause[:rel_s]
    if HYPOTHETICAL.search(before) or DOUBT.search(before) or listener_question(clause):
        return False
    # "meu pai tem diabetes e eu tenho asma": quem fala retoma a si mesmo, o parente fica para trás
    piece_before = re.split(r"\b(?:e|mas|porem) eu\b", piece_before)[-1]
    return not FAMILY.search(piece_before)


def _history_window(text: str, spans: list[Span], i: int) -> tuple[int, int]:
    """Trecho da fala que descreve o i-ésimo termo: até o início do próximo termo (o tempo vem depois do termo)."""
    end = spans[i + 1].start if i + 1 < len(spans) else len(text)
    return spans[i].start, end


def _chart_year(resource: dict) -> int | None:
    dt = parse_dt(resource.get("onsetDateTime") or resource.get("performedDateTime"))
    return dt.year if dt else None


def _birth_year(chart: ChartView) -> int:
    born = parse_dt((chart.r.patient or {}).get("birthDate"))
    return born.year if born else chart.now.year - (chart.r.age or 40)


def _complication(window: str) -> str:
    for words, label in COMPLICATIONS:
        if keyword_status(window, words) == "presente":
            return label
    return ""


def extract_history(segments: list[Segment], chart: ChartView) -> tuple[list[HistoryItem], list[HistoryItem]]:
    now_year, birth_year = chart.now.year, _birth_year(chart)
    conditions: dict[str, HistoryItem] = {}
    surgeries: dict[tuple[str, int | None], HistoryItem] = {}
    chart_conditions = chart.conditions
    chart_procedures = [
        p for p in chart.r.resources.get("Procedure", []) if "cirurg" in norm((p.get("category") or {}).get("text", ""))
    ]
    for s in segments:
        if s.falante not in PATIENT_SIDE:
            continue
        text = norm(s.texto)
        if not _speaker_ok(s, text):
            continue
        ref = fala_ref(s.n)
        procedure_spans = _outer(_spans(text, _procedure_lexicon()))
        # ---- antecedentes pessoais ----
        spans = _outer(_spans(text, _condition_lexicon()))
        # "operei a catarata" é cirurgia, não antecedente: o termo da condição dentro do da cirurgia não vale
        spans = [sp for sp in spans if not any(p.start < sp.end and sp.start < p.end for p in procedure_spans)]
        for i, sp in enumerate(spans):
            d: knowledge.ConditionDef = sp.definition  # type: ignore[assignment]
            a, b = _history_window(text, spans, i)
            before = text[max(a - 40, sp.clause[0]) : sp.start]
            if not _asserted(text, sp, before) or d.key in conditions:
                continue
            when = parse_when(text[a:b], now_year, birth_year)
            item = HistoryItem(
                texto="",
                tipo="relatado",
                refs=[ref],
                condicao=d.label,
                cid=d.cid,
                inicio_aprox=when.texto if when else None,
                ano=when.year if when else None,
                quando=str(when.year) if when else None,
            )
            _conciliate_condition(item, when, chart, chart_conditions)
            conditions[d.key] = item
        # ---- cirurgias prévias ----
        spans = procedure_spans
        for i, sp in enumerate(spans):
            d2: knowledge.ProcedureDef = sp.definition  # type: ignore[assignment]
            a, b = _history_window(text, spans, i)
            window = text[a:b]
            before = text[max(a - 40, sp.clause[0]) : sp.start]
            if not _asserted(text, sp, before) or FUTURE.search(text[sp.clause[0] : sp.clause[1]]):
                continue
            when = parse_when(window, now_year, birth_year)
            key = (d2.key, when.year if when else None)
            if key in surgeries or any(
                k[0] == d2.key and (k[1] == key[1] or None in (k[1], key[1])) for k in surgeries
            ):
                continue
            item = HistoryItem(
                texto="",
                tipo="cirurgia",
                refs=[ref],
                procedimento=d2.label,
                inicio_aprox=when.texto if when else None,
                ano=when.year if when else None,
                quando=str(when.year) if when else None,
                intercorrencia=_complication(window),
            )
            _conciliate_surgery(item, d2, when, chart, chart_procedures)
            surgeries[key] = item
    return list(conditions.values()), list(surgeries.values())


def _conciliate_condition(item: HistoryItem, when: When | None, chart: ChartView, chart_conditions) -> None:
    said = f"relatado{' ' + when.texto if when else ''}"
    match = next((c for c in chart_conditions if c[0][:3] == item.cid[:3]), None)
    if match is None:
        item.conciliacao = "relatado_nao_registrado"
        item.observacao = "Dito pelo paciente; não consta no prontuário."
        item.texto = f"{item.condicao} ({item.cid}) — {said}; não consta no prontuário"
        return
    icd, text, resource = match
    onset = _chart_year(resource)
    item.refs.append(f"Condition/{resource['id']}")
    since = f"desde {onset}" if onset else "sem data de início"
    if when and onset and not (when.lo - YEAR_TOLERANCE <= onset <= when.hi + YEAR_TOLERANCE):
        item.conciliacao = "diverge"
        item.observacao = f"Início relatado (≈ {when.year}) diverge do prontuário ({onset}); confirmar com o paciente."
        item.texto = f"{text} ({icd}) — {said}; o prontuário registra {since}: diverge"
    else:
        item.conciliacao = "prontuario"
        item.observacao = f"Consta no prontuário ({since})."
        item.texto = f"{text} ({icd}) — {said}; consta no prontuário ({since})"
    item.tipo = "diagnostico"


def _conciliate_surgery(
    item: HistoryItem, d: knowledge.ProcedureDef, when: When | None, chart: ChartView, procedures
) -> None:
    extra = f", com {item.intercorrencia}" if item.intercorrencia else ""
    said = f"{d.label}{' ' + when.texto if when else ''}{extra}"
    match = next(
        (
            p
            for p in procedures
            if any(re.search(pat, norm(p.get("code", {}).get("text", ""))) for pat in d.chart_patterns)
        ),
        None,
    )
    if match is None:
        item.conciliacao = "relatado_nao_registrado"
        item.observacao = "Dita pelo paciente; não consta como procedimento no prontuário."
        item.texto = f"{said} — relatada, não consta no prontuário"
        return
    done = parse_dt(match.get("performedDateTime"))
    item.refs.append(f"Procedure/{match['id']}")
    if when and done and not (when.lo - YEAR_TOLERANCE <= done.year <= when.hi + YEAR_TOLERANCE):
        item.conciliacao = "diverge"
        item.observacao = f"Ano relatado ({when.year}) diverge do prontuário ({done.year})."
        item.texto = f"{said} — o prontuário registra {fmt_date(done)}: diverge"
    else:
        item.conciliacao = "prontuario"
        item.observacao = f"Consta no prontuário ({fmt_date(done) or 'sem data'})."
        item.texto = f"{said} — no prontuário ({fmt_date(done) or 'sem data'})"


# ---------- exames relatados ----------
NUM = r"(\d{1,5}(?:[.,]\d{1,2})?)"
VERBS = (
    r"(?:deu|esta|ta|tava|estava|foi|veio|ficou|era|marcou|de|em|com|deu em|ta em|esta em|tava em|estava em|ficou em)"
)
FILLER_STOP = {"e", "ou", "mas", "que", "nem", "o", "a", "nao", "nunca", "sem", "nenhum", "jamais"}


@lru_cache
def _exam_lexicon() -> tuple[re.Pattern, dict[str, knowledge.AnalyteDef]]:
    table = {syn: a for a in knowledge.analytes() if not a.qualitative for syn in a.synonyms}
    names = "|".join(re.escape(t) for t in sorted(table, key=len, reverse=True))
    pattern = re.compile(
        rf"(?<![a-z0-9])(?P<nome>{names})(?![a-z0-9])(?P<fill>(?: [a-z]+){{0,3}}?)(?: {VERBS})? (?P<num>{NUM})(?![\d])"
    )
    return pattern, table


def _reported_value(m: re.Match, table: dict) -> tuple[knowledge.AnalyteDef, float] | None:
    fill = m.group("fill").split()
    words = {w for t in table for w in t.split()}
    if any(w in FILLER_STOP or w in words for w in fill):
        return None
    # sem verbo e com palavras no meio ("tsh pedi 3 vezes") não é resultado
    between = m.group(0)[len(m.group("nome")) : m.start("num") - m.start()]
    if fill and not re.search(rf"(?<!\w){VERBS}(?!\w)", between):
        if not set(fill) <= {"total", "do", "da", "no", "na", "ontem", "hoje", "la", "livre", "sangue"}:
            return None
    value = float(m.group("num").replace(",", "."))
    analyte = table[m.group("nome")]
    # valor impossível ("glicemia 0", "colesterol 25000") é erro de fala ou de transcrição, não resultado
    if value <= 0 or (analyte.high and value > analyte.high * 50):
        return None
    return analyte, value


def extract_exams(segments: list[Segment], chart: ChartView) -> list[ExamInfo]:
    diabetic = chart.has_condition("E11") is not None or chart.has_condition("E10") is not None
    pattern, table = _exam_lexicon()
    found: dict[str, ExamInfo] = {}
    for s in segments:
        if s.falante not in PATIENT_SIDE:
            continue
        text = norm(s.texto)
        if not _speaker_ok(s, text):
            continue
        for m in pattern.finditer(text):
            got = _reported_value(m, table)
            if got is None:
                continue
            analyte, value = got
            a, b = next(((x, y) for x, y in _clause_bounds(text) if x <= m.start() < y), (0, len(text)))
            if listener_question(text[a:b]) or negated_span(text[a:b], m.start() - a, m.end("nome") - a):
                continue
            found[analyte.key] = _exam_item(analyte, value, s.n, chart, diabetic)
    return list(found.values())


def _exam_item(a: knowledge.AnalyteDef, value: float, n: int, chart: ChartView, diabetic: bool) -> ExamInfo:
    low, high = a.low, a.high
    if a.key == "hba1c" and diabetic:
        high = 7.0
    if a.key == "ldl" and diabetic:
        high = 100
    status = "baixo" if low is not None and value < low else "alto" if high is not None and value > high else "normal"
    lay = knowledge.lab_lay()[a.key]
    value_txt = f"{fmt_num(value, a.decimals)} {a.unit}".strip()
    verdict = lay.get(status) or lay["normal"]
    explanation = f"{a.label} {lay['o_que_e']}. O resultado que foi contado na consulta ({value_txt}) {verdict}."
    refs = [fala_ref(n)]
    comparison, conciliation, last_txt = (
        "sem série desse exame no prontuário (valor só relatado, a confirmar com o laudo)",
        "relatado_nao_registrado",
        None,
    )
    last = chart.latest_lab(a.key, max_days=3650) if a.key in _chart_keys() else None
    if last is not None:
        before = float(last.value["valor"])
        last_txt = f"{fmt_num(before, a.decimals)} {a.unit}".strip()
        change = (value - before) / before if before else 0.0
        if abs(change) <= EXAM_TOLERANCE:
            conciliation = "prontuario"
            comparison = f"coerente com o último do prontuário: {last_txt} em {fmt_date(last.timestamp)}"
        else:
            conciliation = "diverge"
            way = "acima" if change > 0 else "abaixo"
            comparison = (
                f"{abs(round(change * 100))}% {way} do último do prontuário: {last_txt} em {fmt_date(last.timestamp)}"
            )
        refs.append(last.source_ref)
    return ExamInfo(
        exame=a.label,
        chave=a.key,
        valor=value_txt,
        data="relatado na consulta",
        status=status,
        explicacao=explanation,
        tendencia=comparison,
        refs=refs,
        origem_dado=f"relatado (fala {n})",
        comparacao=comparison,
        conciliacao=conciliation,
        valor_num=value,
    )


@lru_cache
def _chart_keys() -> frozenset[str]:
    from app.fhir_client import codes

    return frozenset(codes.LABS)


def extract_all(segments: list[Segment], chart: ChartView) -> Extraction:
    antecedentes, cirurgias = extract_history(segments, chart)
    return Extraction(antecedentes, cirurgias, extract_exams(segments, chart))


# ---------- RCRI ----------
def _has(chart: ChartView, found: Extraction, prefixes: tuple[str, ...]) -> tuple[str, list[str]] | None:
    """(texto, refs) do diagnóstico no prontuário ou relatado na conversa cujo CID começa com um dos prefixos."""
    for icd, text, resource in chart.conditions:
        if icd.startswith(prefixes):
            return f"{text} ({icd}) no prontuário", [f"Condition/{resource['id']}"]
    for h in found.antecedentes:
        if h.cid.startswith(prefixes):
            return f"{h.condicao} ({h.cid}) relatado", h.refs
    return None


def rcri(chart: ChartView, found: Extraction, planned: list[SurgeryItem]) -> Rcri | None:
    """Índice de Lee revisado: 6 critérios de 1 ponto, cada um ligado ao recurso FHIR ou à fala que o sustenta."""
    if not planned:
        return None
    surgery = norm(" ".join(p.texto for p in planned))
    first = next((p for p in planned if p.texto.startswith("Citado")), planned[0])
    label = re.sub(r"^(Citado na consulta|Planejada no prontuário): ?", "", first.texto).strip(" “”.")
    refs = [r for p in planned for r in p.refs]
    items: list[RcriItem] = []

    def add(
        criterion: str, hit: tuple[str, list[str]] | None, absent: str, extra_refs: list[str] | None = None
    ) -> None:
        items.append(
            RcriItem(
                criterio=criterion,
                pontos=1 if hit else 0,
                presente=bool(hit),
                evidencia=hit[0] if hit else absent,
                refs=hit[1] if hit else (extra_refs or []),
            )
        )

    high = next((w for w in RCRI_HIGH_RISK if w in surgery), None)
    add(
        "Cirurgia de alto risco (intraperitoneal, intratorácica ou vascular suprainguinal)",
        (f"cirurgia planejada de alto risco ({high})", refs) if high else None,
        "Cirurgia planejada de baixo risco para o índice (nem intraperitoneal, nem intratorácica, nem vascular)",
        refs,
    )
    add(
        "Cardiopatia isquêmica (infarto, angina ou revascularização)",
        _has(chart, found, ("I20", "I21", "I22", "I23", "I24", "I25")),
        "Sem cardiopatia isquêmica no prontuário nem na conversa",
    )
    add(
        "Insuficiência cardíaca",
        _has(chart, found, ("I50", "I11.0", "I13")),
        "Sem insuficiência cardíaca no prontuário nem na conversa",
    )
    add(
        "Doença cerebrovascular (AVC ou ataque isquêmico transitório)",
        _has(chart, found, ("I60", "I61", "I62", "I63", "I64", "I65", "I66", "I67", "I69", "G45")),
        "Sem AVC ou AIT no prontuário nem na conversa",
    )
    diabetes = _has(chart, found, ("E10", "E11", "E12", "E13", "E14"))
    insulin = [m for m in chart.active_meds if m.drug_class in ("insulina_basal", "insulina_rapida")]
    if diabetes and insulin:
        add(
            "Diabetes tratado com insulina",
            (f"{diabetes[0]}; em uso de {', '.join(m.name for m in insulin)}", diabetes[1] + [m.ref for m in insulin]),
            "",
        )
    elif diabetes:
        oral = chart.active_meds
        add(
            "Diabetes tratado com insulina",
            None,
            f"{diabetes[0]}, sem insulina na prescrição ativa (não pontua)",
            diabetes[1] + [m.ref for m in oral if m.drug_class in ("sulfonilureia", "biguanida")],
        )
    else:
        add("Diabetes tratado com insulina", None, "Sem diabetes no prontuário nem na conversa")
    cr = chart.latest_lab("creatinina", max_days=400)
    if cr is None:
        items.append(
            RcriItem(
                criterio="Creatinina acima de 2,0 mg/dL",
                pontos=0,
                presente=False,
                evidencia="Sem creatinina recente no prontuário: pedir o exame antes da cirurgia",
                sem_dado=True,
            )
        )
    else:
        value = float(cr.value["valor"])
        add(
            "Creatinina acima de 2,0 mg/dL",
            (f"creatinina {fmt_num(value, 1)} mg/dL em {fmt_date(cr.timestamp)}", [cr.source_ref])
            if value > 2.0
            else None,
            f"Creatinina {fmt_num(value, 1)} mg/dL em {fmt_date(cr.timestamp)} (até 2,0: não pontua)",
            [cr.source_ref],
        )
    points = sum(i.pontos for i in items)
    cls, pct = next((c, p) for lim, c, p in reversed(RCRI_CLASSES) if points >= lim)
    return Rcri(
        cirurgia=label,
        pontos=points,
        classe=cls,
        risco_pct=pct,
        interpretacao=(
            f"RCRI {points} ponto{'s' if points != 1 else ''} (classe {cls}): cerca de {fmt_num(pct, 1)}% de risco de "
            "infarto, parada cardíaca ou morte cardíaca em 30 dias. "
            + (
                "Risco baixo: em geral não exige exame cardíaco adicional antes de cirurgia eletiva."
                if points == 0
                else "Risco elevado: avaliar a necessidade de exames e do cuidado perioperatório com a cardiologia."
            )
        ),
        itens=items,
        fonte=RCRI_SOURCE,
        refs=list(dict.fromkeys(r for i in items for r in i.refs)),
    )
