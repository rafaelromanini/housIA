"""Pedidos e prescrições em rascunho tirados da fala (ambient orders), checados ANTES de propor.

1. Detecção: na fala do profissional, um verbo de intenção ("vou pedir", "vamos começar", "aumentar a losartana para
   100") mais um medicamento (drugs.csv e nomes comerciais) ou um exame (lab_catalog.csv).
2. Posologia por regex ("500 mg de 8 em 8 horas por 7 dias").
3. Checagens de segurança sobre cada rascunho: alergia e reatividade cruzada, dose por TFG e idade, interação com o
   que está em uso e com o que o paciente contou, duplicidade, alto alerta e exame repetido dentro do intervalo.
4. Sugestões vindas dos protocolos pendentes e dos próximos passos das hipóteses.

Tudo determinístico: roda com ou sem LLM. A IA só completa posologia e justificativa (ver `ai.merge_live`) e nunca
cria rascunho nem remove checagem. Cada rascunho cita `Fala/N` (e/ou o registro do prontuário que o motivou).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache

from app.consult import knowledge
from app.consult.chart import ChartView, fmt_date, fmt_num
from app.consult.schemas import (
    AllergyInfo,
    Hypothesis,
    OrderCheck,
    OrderDecision,
    OrderDraft,
    ProtocolItem,
    Segment,
    Severity,
    fala_ref,
)
from app.consult.text import NEG_WORDS, drug_hits, is_question, norm
from app.fhir_client import codes
from app.medrec.rules import CRITICAL_OMISSION, CROSS_REACTIVITY, DUPLICITY_EXEMPT
from app.reference import drugs, high_alert_reason, interactions, matches
from app.timeline.record import normalize

SEVERITY_RANK: dict[str, int] = {"critico": 0, "atencao": 1, "info": 2}
MAX_SUGGESTIONS = 6
ORDER_SYSTEM = "http://housia.dev/fhir/NamingSystem/pedido-consulta"
DRAFT_NOTE = (
    "Rascunho gerado pelo HousIA a partir da conversa e assinado pelo profissional. Não vale como receita nem como "
    "pedido oficial até ser emitido no sistema do serviço."
)

# ---------- vocabulário da intenção ----------
# classe do verbo e a que itens ele se aplica: "exam" só pedido de exame, "rx" só prescrição, "both" os dois.
# ("vamos avaliar a metformina" não é prescrever; "vou colher a creatinina" não é medicamento)
VERBS: dict[str, tuple[str, str]] = {}
for _words, _cls, _scope in (
    ("realizar colher coletar repetir dosar checar avaliar verificar conferir fazer faco mandar", "start", "exam"),
    (
        "comecar comecamos comeco iniciar iniciamos inicio prescrever prescrevo receitar receito introduzir associar "
        "associo adicionar adiciono acrescentar tomar usar tome use liberar",
        "start",
        "rx",
    ),
    ("pedir peco pedimos solicitar solicito requisitar passar passo colocar", "start", "both"),
    (
        "aumentar aumento aumentamos subir elevar reduzir reduzo diminuir diminuo baixar ajustar ajusto corrigir",
        "change",
        "rx",
    ),
    (
        "suspender suspendo suspendemos suspenda parar pare interromper interrompa retirar retire tirar tire cortar "
        "corte descontinuar",
        "stop",
        "rx",
    ),
    ("trocar troco trocamos troque substituir substituo mudar", "swap", "rx"),
):
    for _w in _words.split():
        VERBS[_w] = (_cls, _scope)
# "tomar/usar" soltos são comentário ("tomar ibuprofeno faz mal"): só valem com "vai/precisa/tem que" antes
NEEDS_LEAD = {"tomar", "usar", "passar", "fazer", "mandar", "liberar"}
IMPERATIVE_OK = {"tome", "use", "suspenda", "pare", "interrompa", "retire", "tire", "corte", "troque"}
FUTURE = set(
    (
        "vou vamos vai iremos irei preciso precisamos precisa precisar quero queremos devo devemos deve podemos tem "
        "temos"
    ).split()
)
CONDITIONAL = {"se", "caso", "talvez", "quando", "sabe"}
FILLERS = {"entao", "ai", "e", "bom", "ok", "agora", "ja", "tambem", "a", "gente", "doutora", "doutor", "pois", "certo"}
LIST_FILLER = re.compile(
    r"^(?:(?:e|ou|tambem|alem|disso|mais|o|a|os|as|um|uma|de|do|da|dos|das|para|pra|com|sem|duas|dois|alguns)\s*)*$"
)

# ---------- posologia ----------
NUMBER_WORDS = {
    "um": 1,
    "uma": 1,
    "dois": 2,
    "duas": 2,
    "tres": 3,
    "quatro": 4,
    "cinco": 5,
    "seis": 6,
    "sete": 7,
    "oito": 8,
    "nove": 9,
    "dez": 10,
    "doze": 12,
    "quatorze": 14,
    "quinze": 15,
    "vinte": 20,
    "trinta": 30,
}
NUM = r"(\d+|" + "|".join(NUMBER_WORDS) + r")"
UNIT = r"(mg|miligramas?|mcg|microgramas?|g|gramas?|ml|ui|unidades|gotas|comprimidos?|capsulas?|ampolas?)"
DOSE = re.compile(rf"(?<![\w,.])(\d+(?:[.,]\d+)?)\s*{UNIT}(?![a-z])")
DOSE_TO = re.compile(
    rf"(?:\bpara|\bpra)\s+(?:o\s+)?(\d+(?:[.,]\d+)?)(?:\s*{UNIT}(?![a-z]))?(?!\s*(?:dias?|semanas?|mes|meses|horas?|vezes|vez|x)\b)"
)
EVERY_N = re.compile(r"\bde (\d+) em \1 horas?\b")
EACH_N = re.compile(r"\b(?:a )?cada (\d+) horas?\b")
TIMES = re.compile(rf"\b{NUM}\s*(?:x|vezes?|vez)\s*(?:ao|por|no|a|em)?\s*(dia|semana|mes)\b")
EVERY_DAY = re.compile(r"\b(?:todo dia|diariamente|uma vez ao dia)\b")
AS_NEEDED = re.compile(
    r"\b(?:se (?:houver |tiver |precisar de |sentir )?(?:dor|febre|necessario|precisar|enjoo|nausea)"
    r"|sos|em caso de (?:dor|febre))\b"
)
MOMENT = re.compile(
    r"\b(?:pela manha|de manha|a noite|antes de dormir|em jejum|antes do cafe|apos o almoco|com as refeicoes)\b"
)
SINGLE = re.compile(r"\bdose unica\b")
DURATION = re.compile(rf"\b(?:por|durante|pelos proximos|nos proximos)\s+{NUM}\s*(dias?|semanas?|mes(?:es)?)\b")
DURATION_BARE = re.compile(rf"\b{NUM}\s*(dias|semanas|meses)\b")
CONTINUOUS = re.compile(r"\b(?:uso continuo|continuo|uso prolongado|para sempre)\b")
ROUTES = (
    (re.compile(r"\b(?:via oral|vo|oral|por via oral)\b"), "via oral", "26643006", "Oral route"),
    (re.compile(r"\b(?:endovenos[oa]|intravenos[oa]|iv|na veia)\b"), "endovenosa", "47625008", "Intravenous route"),
    (re.compile(r"\b(?:intramuscular|im)\b"), "intramuscular", "78421000", "Intramuscular route"),
    (re.compile(r"\b(?:subcutane[oa]|sc)\b"), "subcutânea", "34206005", "Subcutaneous route"),
    (re.compile(r"\b(?:topic[oa]|na pele|pomada)\b"), "tópica", "", ""),
    (re.compile(r"\b(?:inalatoria|inalacao|bombinha)\b"), "inalatória", "", ""),
)
PURPOSE = re.compile(
    r"\b(?:para|pra|por causa d[aeo]s?|devido a[o]?s?)\s+(?!\d)(?!o senhor|a senhora|voce|eu |nos )(.{4,90})$"
)
UNIT_LABEL = {
    "miligrama": "mg",
    "miligramas": "mg",
    "microgramas": "mcg",
    "micrograma": "mcg",
    "gramas": "g",
    "grama": "g",
}
PERIOD_UNIT = {"dia": "d", "semana": "wk", "mes": "mo"}


@dataclass
class Posology:
    dose: str = ""
    via: str = ""
    frequencia: str = ""
    duracao: str = ""
    periodicidade: dict | None = None
    duracao_dias: int | None = None
    new_dose: bool = False  # "para 100": dose-alvo de um ajuste

    def empty(self) -> bool:
        return not (self.dose or self.via or self.frequencia or self.duracao)


def _num(token: str) -> int:
    return int(token) if token.isdigit() else NUMBER_WORDS[token]


def _fmt_dose(value: str, unit: str | None) -> str:
    unit = UNIT_LABEL.get(unit or "", unit or "")
    return f"{value.replace('.', ',')} {unit}".strip()


def parse_posology(text: str) -> Posology:
    """Dose, via, frequência e duração de um trecho já normalizado ("500 mg de 8 em 8 horas por 7 dias")."""
    out = Posology()
    t = text
    m = DOSE_TO.search(t)
    if m:  # "aumentar a losartana para 100 mg": a dose-alvo é a que vem depois do "para"
        out.dose, out.new_dose = _fmt_dose(m.group(1), m.group(2)), True
    else:
        d = DOSE.search(t)
        if d:
            out.dose = _fmt_dose(d.group(1), d.group(2))
    if m := EVERY_N.search(t):
        n = int(m.group(1))
        out.frequencia = f"de {n} em {n} horas"
        out.periodicidade = {"frequencia": 1, "periodo": n, "unidade": "h"}
        t = t.replace(m.group(0), " ")
    elif m := EACH_N.search(t):
        n = int(m.group(1))
        out.frequencia = f"a cada {n} horas"
        out.periodicidade = {"frequencia": 1, "periodo": n, "unidade": "h"}
        t = t.replace(m.group(0), " ")
    elif m := TIMES.search(t):
        n, unit = _num(m.group(1)), m.group(2)
        per = {"dia": "ao dia", "semana": "por semana", "mes": "por mês"}[unit]
        out.frequencia = f"{'1 vez' if n == 1 else f'{n} vezes'} {per}"
        out.periodicidade = {"frequencia": n, "periodo": 1, "unidade": PERIOD_UNIT[unit]}
        t = t.replace(m.group(0), " ")
    elif m := EVERY_DAY.search(t):
        out.frequencia = "1 vez ao dia"
        out.periodicidade = {"frequencia": 1, "periodo": 1, "unidade": "d"}
    elif SINGLE.search(t):
        out.frequencia = "dose única"
    if AS_NEEDED.search(t):
        out.frequencia = (out.frequencia + " · " if out.frequencia else "") + "se necessário (SOS)"
    elif not out.frequencia and (m := MOMENT.search(t)):
        out.frequencia = m.group(0)
    if m := DURATION.search(t) or DURATION_BARE.search(t):
        n, unit = _num(m.group(1)), m.group(2)
        base = "dia" if unit.startswith("dia") else "semana" if unit.startswith("semana") else "mês"
        out.duracao = f"{n} {base if n == 1 else {'dia': 'dias', 'semana': 'semanas', 'mês': 'meses'}[base]}"
        out.duracao_dias = n * {"dia": 1, "semana": 7, "mês": 30}[base]
    elif CONTINUOUS.search(t):
        out.duracao = "uso contínuo"
    for rx, label, _code, _display in ROUTES:
        if rx.search(t):
            out.via = label
            break
    return out


def route_coding(via: str) -> dict | None:
    for _rx, label, code, display in ROUTES:
        if label == via and code:
            return {"system": codes.SNOMED, "code": code, "display": display}
    return None


# ---------- itens na fala ----------
@dataclass
class Item:
    kind: str  # "medicamento" | "exame"
    start: int
    end: int
    key: str  # princípio ativo, @classe ou chave do catálogo
    term: str
    lab: knowledge.LabDef | None = None


def _word_match(clause: str, words: tuple[str, ...], taken: list[tuple[int, int]]) -> tuple[int, int] | None:
    for w in words:
        for m in re.finditer(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", clause):
            span = (m.start(), m.end())
            if not any(span[0] < b and a < span[1] for a, b in taken):
                return span
    return None


# associações e nomes comerciais que o catálogo (nome único com "+") não casa na fala
ALIASES = {
    "sulfametoxazol com trimetoprima": "sulfametoxazol_trimetoprima",
    "sulfametoxazol e trimetoprima": "sulfametoxazol_trimetoprima",
    "sulfametoxazol": "sulfametoxazol_trimetoprima",
    "bactrim": "sulfametoxazol_trimetoprima",
    "amoxicilina com clavulanato": "amoxicilina_clavulanato",
    "amoxicilina e clavulanato": "amoxicilina_clavulanato",
    "clavulin": "amoxicilina_clavulanato",
    "augmentin": "amoxicilina_clavulanato",
    "piperacilina com tazobactam": "piperacilina_tazobactam",
    "piperacilina e tazobactam": "piperacilina_tazobactam",
    "tazocin": "piperacilina_tazobactam",
}


@lru_cache(maxsize=4096)
def _items(clause: str) -> tuple[Item, ...]:
    """Exames do catálogo e medicamentos citados na oração (o sinônimo mais longo vence), em ordem de aparição."""
    items: list[Item] = []
    taken: list[tuple[int, int]] = []
    words = sorted(((w, lab) for lab in knowledge.lab_catalog() for w in lab.words), key=lambda p: -len(p[0]))
    for w, lab in words:
        for m in re.finditer(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", clause):
            span = (m.start(), m.end())
            if any(span[0] < b and a < span[1] for a, b in taken) or any(i.lab is lab for i in items):
                continue
            taken.append(span)
            items.append(Item("exame", span[0], span[1], lab.key, w, lab))
    for alias, ing in sorted(ALIASES.items(), key=lambda kv: -len(kv[0])):
        m = re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", clause)
        if m and not any(m.start() < b and a < m.end() for a, b in taken) and not any(i.key == ing for i in items):
            taken.append((m.start(), m.end()))
            items.append(Item("medicamento", m.start(), m.end(), ing, alias))
    for start, end, ing, term in drug_hits(clause):
        span = (start, end - 1)
        if any(span[0] < b and a < span[1] for a, b in taken):
            continue
        taken.append(span)
        items.append(Item("medicamento", span[0], span[1], ing, term))
    return tuple(sorted(items, key=lambda i: i.start))


def find_items(clause: str) -> list[Item]:
    return list(_items(clause))


@dataclass
class Intent:
    cls: str  # start | change | stop | swap
    scope: str  # exam | rx | both
    start: int
    end: int

    def fits(self, kind: str) -> bool:
        return self.scope == "both" or (self.scope == "exam") == (kind == "exame")


TOKEN = re.compile(r"[a-z0-9]+")


@lru_cache(maxsize=4096)
def _intents(clause: str) -> tuple[Intent, ...]:
    """Verbos de pedido/prescrição que valem como intenção do profissional (não negados, não condicionais)."""
    tokens = [(m.group(), m.start(), m.end()) for m in TOKEN.finditer(clause)]
    out: list[Intent] = []
    for i, (word, start, end) in enumerate(tokens):
        verb = VERBS.get(word)
        if verb is None:
            continue
        before = [w for w, _, _ in tokens[max(0, i - 7) : i]]
        near = before[-4:]
        if any(w in NEG_WORDS for w in near) or any(w in CONDITIONAL for w in before):
            continue
        # "vou suspender o ibuprofeno e trocar a hidroclorotiazida…": o "vou" alcança os verbos seguintes da lista
        has_lead = any(w in FUTURE for w in (near if word in NEEDS_LEAD else before[-7:]))
        at_start = all(w in FILLERS for w in before)
        if word in NEEDS_LEAD:
            ok = has_lead or (word in IMPERATIVE_OK and at_start)
        else:
            ok = has_lead or at_start
        if ok:
            out.append(Intent(verb[0], verb[1], start, end))
    return tuple(out)


def find_intents(clause: str) -> list[Intent]:
    return list(_intents(clause))


def _governing(item: Item, intents: list[Intent]) -> Intent | None:
    intents = [i for i in intents if i.fits(item.kind)]
    left = [i for i in intents if i.end <= item.start]
    if left:
        return left[-1]
    right = [i for i in intents if i.start >= item.end]
    return right[0] if right else None


# ---------- checagens ----------
@dataclass
class OrderContext:
    chart: ChartView
    age: int | None
    # (princípio, classe, nome, ref): em uso no prontuário + relatado na consulta
    in_use: list[tuple[str, str, str, str]]
    allergies: list[tuple[str, str, list[str]]]  # (alérgeno "@classe"/princípio, rótulo, refs)
    pending: list = field(default_factory=list)  # ServiceRequest em aberto (record.pending_exams)


def allergen_keys(chart: ChartView, allergies: list[AllergyInfo]) -> list[tuple[str, str, list[str]]]:
    """Alérgenos (chave de `matches`) do prontuário e os relatados na consulta, cada um com a fonte."""
    out: list[tuple[str, str, list[str]]] = []
    for a in chart.ctx.alergias:
        detail = a.detail or {}
        code = detail.get("codigo") or ""
        if not code:
            continue
        key = f"@{code}" if detail.get("sistema") == codes.DRUG_CLASS else code
        out.append((key, a.summary.partition(" — ")[0], [a.source_ref]))
    defs = {d.label: d for d in knowledge.allergy_defs()}
    for a in allergies:
        if a.fonte == "relatado" and a.alergeno in defs:
            out.append((defs[a.alergeno].allergen, a.alergeno, a.refs))
    return out


def _check(tipo, sev: Severity, texto: str, refs: list[str]) -> OrderCheck:
    return OrderCheck(tipo=tipo, severidade=sev, texto=texto, refs=list(dict.fromkeys(refs)))


def _drug_checks(d: OrderDraft, ing: str, cls: str, name: str, ctx: OrderContext, drafts: list[OrderDraft]):
    checks: list[OrderCheck] = []
    chart = ctx.chart
    active = [m for m in chart.active_meds if (ing and m.ingredient == ing) or (not ing and m.drug_class == cls)]
    if d.acao == "suspender":
        risk = CRITICAL_OMISSION.get(cls)
        if risk:
            checks.append(_check("retirada_abrupta", "atencao", f"Suspender {name}: {risk}", [m.ref for m in active]))
        return checks
    for key, label, refs in ctx.allergies:
        if matches(key, ing, cls):
            checks.append(
                _check(
                    "alergia",
                    "critico",
                    f"Alergia registrada a {label}: {name} pertence ao grupo. Escolher alternativa ou justificar.",
                    refs,
                )
            )
        elif key.startswith("@") and cls in CROSS_REACTIVITY.get(key[1:], set()):
            checks.append(
                _check(
                    "reatividade_cruzada",
                    "atencao",
                    f"Alergia a {label}: possível reação cruzada com {name} (classe {cls.replace('_', ' ')}); "
                    "avaliar o risco antes de prescrever.",
                    refs,
                )
            )
    egfr, age = chart.egfr, ctx.age or 0
    fired: set[str] = set()
    for rule in sorted(knowledge.dose_alerts(), key=lambda r: r.limit):
        if not matches(rule.item, ing, cls) or rule.criterion in fired:
            continue
        sev: Severity = rule.severity  # type: ignore[assignment]
        if rule.criterion == "tfg" and egfr and egfr[0] < rule.limit:
            fired.add("tfg")
            checks.append(
                _check(
                    "dose_tfg",
                    sev,
                    f"{name}: {rule.recommendation} (TFG estimada ~{round(egfr[0])} mL/min/1,73 m²)",
                    [egfr[1].source_ref],
                )
            )
        elif rule.criterion == "idade" and age >= rule.limit:
            fired.add("idade")
            checks.append(_check("dose_idade", sev, f"{name}: {rule.recommendation} ({age} anos)", []))
    others = [c for c in ctx.in_use if (c[0] != ing if ing else c[1] != cls)]
    for other in drafts:  # outros rascunhos da mesma consulta também entram na checagem
        if other is d or other.tipo != "medicamento" or other.acao in ("suspender",):
            continue
        o_ing = other.chave if not other.chave.startswith("@") else ""
        o_cls = other.chave[1:] if other.chave.startswith("@") else drugs()[other.chave].drug_class
        if (o_ing and o_ing == ing) or (not o_ing and o_cls == cls):
            continue
        others.append((o_ing, o_cls, other.item, other.refs[0]))
    seen: set[str] = set()
    for o_ing, o_cls, o_name, o_ref in others:
        if o_name in seen:
            continue
        for row in interactions():
            hit = (matches(row["a"], ing, cls) and matches(row["b"], o_ing, o_cls)) or (
                matches(row["a"], o_ing, o_cls) and matches(row["b"], ing, cls)
            )
            if hit:
                seen.add(o_name)
                checks.append(
                    _check(
                        "interacao",
                        row["severidade"],
                        f"Interação {name} + {o_name}: {row['descricao']}. {row['recomendacao']}.",
                        [o_ref],
                    )
                )
                break
    if d.acao == "iniciar":
        for m in active:
            checks.append(
                _check(
                    "duplicidade",
                    "atencao",
                    f"{m.name} já está em uso ({m.text}): conferir se é mesmo um novo início ou só ajuste de dose.",
                    [m.ref],
                )
            )
        if cls not in DUPLICITY_EXEMPT:
            same = [m for m in chart.active_meds if m.drug_class == cls and m.ingredient != ing]
            for m in same:
                checks.append(
                    _check(
                        "duplicidade",
                        "atencao",
                        f"Duplicidade terapêutica: {m.name} (mesma classe, {cls.replace('_', ' ')}) já está em uso.",
                        [m.ref],
                    )
                )
    elif d.acao == "alterar_dose" and not active:
        checks.append(
            _check(
                "sem_prescricao",
                "atencao",
                f"{name} não consta na prescrição ativa do prontuário: confirmar a dose atual antes de ajustar.",
                [],
            )
        )
    reason = high_alert_reason(ing, cls)
    if reason:
        checks.append(_check("alto_alerta", "atencao", f"{name}: {reason}.", []))
    return checks


def _exam_checks(d: OrderDraft, lab: knowledge.LabDef, ctx: OrderContext) -> list[OrderCheck]:
    checks: list[OrderCheck] = []
    chart = ctx.chart
    last = None
    for key in lab.result_keys:
        e = chart.latest_lab(key, max_days=3650)
        if e is not None and (last is None or e.timestamp > last[0].timestamp):
            last = (e, key)
    if last and lab.interval_days:
        event, key = last
        days = (chart.now - event.timestamp).days
        if days < lab.interval_days:
            measure = codes.LABS[key]
            value = f"{fmt_num(float(event.value['valor']), measure.decimals)} {measure.unit}".strip()
            when = "hoje" if days == 0 else f"há {days} dia{'s' if days != 1 else ''}"
            checks.append(
                _check(
                    "exame_repetido",
                    "atencao",
                    f"{lab.name}: já há resultado de {measure.label} {when} ({value}, {fmt_date(event.timestamp)}); o "
                    f"intervalo habitual entre repetições é de {lab.interval_days} dias. Repetir só se houver "
                    "mudança clínica.",
                    [event.source_ref],
                )
            )
    words = {norm(lab.name), *lab.words}
    for p in ctx.pending:
        if any(w in norm(p.texto) for w in words):
            checks.append(
                _check(
                    "exame_pendente",
                    "atencao",
                    f"Já existe pedido em aberto de {p.texto} ({p.ref}) sem resultado.",
                    [p.ref],
                )
            )
    return checks


def _finish(d: OrderDraft) -> OrderDraft:
    d.checagens.sort(key=lambda c: SEVERITY_RANK[c.severidade])
    d.gravidade = d.checagens[0].severidade if d.checagens else "info"
    return d


# ---------- detecção ----------
def _draft_id(prefix: str, n: int, key: str, action: str) -> str:
    return f"{prefix}-{n}-{key.lstrip('@')}-{action}"


def _new_med(item: Item, action: str, seg: Segment, pos: Posology, active_dose: str, active_ref: str) -> OrderDraft:
    catalog = drugs()
    if item.key.startswith("@"):
        name, atc = f"{item.term} (classe)", ""
    else:
        name, atc = catalog[item.key].name, catalog[item.key].atc
    dose = pos.dose
    if action == "alterar_dose" and dose and dose.replace(",", "").isdigit() and active_dose:
        unit = active_dose.split(" ", 1)[1] if " " in active_dose else ""
        dose = f"{dose} {unit}".strip()
    return OrderDraft(
        id=_draft_id("med", seg.n, item.key, action),
        tipo="medicamento",
        acao=action,  # type: ignore[arg-type]
        item=name,
        chave=item.key,
        dose=dose,
        via=pos.via,
        frequencia=pos.frequencia,
        duracao=pos.duracao,
        dose_anterior=active_dose if action in ("alterar_dose", "suspender") else "",
        periodicidade=pos.periodicidade,
        duracao_dias=pos.duracao_dias,
        refs=[fala_ref(seg.n)],
        atc=atc,
        prescricao_ref=active_ref if action in ("alterar_dose", "suspender") else "",
    )


def _new_exam(item: Item, seg: Segment, pos: Posology) -> OrderDraft:
    lab = item.lab
    assert lab is not None
    return OrderDraft(
        id=_draft_id("exm", seg.n, lab.key, "solicitar"),
        tipo="exame",
        acao="solicitar",
        item=lab.name,
        chave=lab.key,
        refs=[fala_ref(seg.n)],
        loinc=lab.loinc,
        loinc_nome=lab.loinc_name,
        categoria=lab.category,
        preparo=lab.prep,
    )


def _merge(old: OrderDraft, new: OrderDraft) -> None:
    """O profissional repetiu o item mais adiante (ou corrigiu a dose): vale o mais recente, com todas as falas."""
    for f in ("dose", "via", "duracao", "justificativa"):
        if getattr(new, f):
            setattr(old, f, getattr(new, f))
    if new.frequencia:
        sos = "se necessário (SOS)"
        if old.frequencia and (sos in old.frequencia) != (sos in new.frequencia):  # "1 g se dor, de 6 em 6 horas"
            regular = new.frequencia if sos in old.frequencia else old.frequencia
            old.frequencia = f"{regular} · {sos}"
        else:
            old.frequencia = new.frequencia
    if new.periodicidade:
        old.periodicidade = new.periodicidade
    if new.duracao_dias is not None:
        old.duracao_dias = new.duracao_dias
    old.refs = list(dict.fromkeys(old.refs + new.refs))


def detect(segments: list[Segment], chart: ChartView) -> list[OrderDraft]:
    """Rascunhos das falas do profissional (sem checagens ainda)."""
    drafts: dict[tuple[str, str], OrderDraft] = {}
    active = {m.ingredient: m for m in chart.active_meds}
    by_class = {m.drug_class: m for m in chart.active_meds}

    def put(d: OrderDraft) -> OrderDraft:
        key = (d.chave, d.acao)
        if key in drafts:
            _merge(drafts[key], d)
            return drafts[key]
        drafts[key] = d
        return d

    for seg in segments:
        if seg.falante != "medico":
            continue
        for sentence in re.split(r"(?<=[.!?;])\s+|\n", seg.texto):
            if not sentence.strip() or is_question(sentence):
                continue
            carry: Intent | None = None
            last: list[OrderDraft] = []
            sentence_drafts: list[OrderDraft] = []
            conditional = False
            for clause in re.split(r"(?<!\d),(?!\d)|;|\b(?:mas|porem)\b", norm(sentence)):
                clause = re.sub(r"[.!?]+", " ", clause).strip()
                if not clause:
                    continue
                if conditional:  # "Caso tenha febre, vou pedir hemograma": a consequência de uma condição não é pedido
                    conditional = False
                    carry, last = None, []
                    continue
                if clause.split()[0] in CONDITIONAL:
                    conditional = True
                    continue
                items = find_items(clause)
                intents = find_intents(clause)
                if not items:
                    pos = parse_posology(clause)
                    if last and not pos.empty() and not intents:  # "…amoxicilina, 500 mg de 8 em 8 horas, por 7 dias"
                        for d in last:
                            _merge(d, _fill(d, pos))
                    else:
                        carry = None
                    continue
                spans = [(i.start, i.end) for i in items]
                leftover = clause
                for a, b in sorted(spans, reverse=True):
                    leftover = leftover[:a] + " " + leftover[b:]
                leftover = PURPOSE.sub(" ", leftover)
                continues = bool(LIST_FILLER.match(re.sub(r"\s+", " ", leftover).strip()))
                governed: list[tuple[Item, Intent]] = []
                for item in items:
                    intent = _governing(item, intents)
                    if intent is None and carry is not None and carry.fits(item.kind) and continues:
                        intent = carry
                    if intent is not None:
                        governed.append((item, intent))
                # "trocar A por B": o primeiro medicamento sai, o resto entra; um só medicamento é ajuste
                swap_group: dict[int, list[Item]] = {}
                for item, intent in governed:
                    if intent.cls == "swap" and item.kind == "medicamento":
                        swap_group.setdefault(id(intent), []).append(item)
                made: list[OrderDraft] = []
                for item, intent in governed:
                    n_item = items.index(item)
                    nxt = items[n_item + 1].start if n_item + 1 < len(items) else len(clause)
                    pos = parse_posology(clause[item.end : nxt])
                    if item.kind == "exame":
                        if intent.cls == "start":
                            made.append(put(_new_exam(item, seg, pos)))
                        continue
                    act = {"start": "iniciar", "change": "alterar_dose", "stop": "suspender", "swap": "iniciar"}[
                        intent.cls
                    ]
                    if intent.cls == "swap":
                        group = swap_group[id(intent)]
                        if len(group) == 1:
                            act = "alterar_dose"
                        elif item is group[0]:
                            act = "suspender"
                    cur = active.get(item.key) or (by_class.get(item.key[1:]) if item.key.startswith("@") else None)
                    made.append(put(_new_med(item, act, seg, pos, cur.dose if cur else "", cur.ref if cur else "")))
                if made:
                    sentence_drafts.extend(made)
                if made:
                    last = made
                    carry = next((i for i in (_governing(it, intents) for it in items) if i), carry)
                else:
                    carry = None
            if sentence_drafts and (why := _purpose(sentence)):
                for d in sentence_drafts:  # "vou pedir ecg, raio x e tsh para investigar a fraqueza"
                    d.justificativa = d.justificativa or why
    return list(drafts.values())


def _purpose(sentence: str) -> str:
    """Finalidade dita na frase ("para investigar a fraqueza"), com a grafia original (acentos)."""
    text = re.sub(r"\s+", " ", sentence.strip())
    chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        for c in normalize(ch):
            chars.append(c)
            origin.append(i)
    m = PURPOSE.search("".join(chars))
    if not m or not m.group(1).strip(" ,.!?"):
        return ""
    why = text[origin[m.start(1)] : origin[m.end(1) - 1] + 1].strip(" ,.!?")
    return f"Para {why}"[:160]


def _fill(d: OrderDraft, pos: Posology) -> OrderDraft:
    """Posologia dita numa oração à parte, logo depois do item."""
    return OrderDraft(
        id=d.id,
        tipo=d.tipo,
        acao=d.acao,
        item=d.item,
        chave=d.chave,
        dose=pos.dose if not d.dose or pos.new_dose else "",
        via=pos.via,
        frequencia=pos.frequencia,
        duracao=pos.duracao,
        periodicidade=pos.periodicidade,
        duracao_dias=pos.duracao_dias,
        refs=d.refs,
    )


# ---------- sugestões das regras ----------
def suggest(
    chart: ChartView,
    protocols: list[ProtocolItem],
    hypotheses: list[Hypothesis],
    existing: list[OrderDraft],
    ctx: OrderContext,
) -> list[OrderDraft]:
    """Protocolos pendentes e próximos passos das hipóteses viram sugestões de exame (com as mesmas checagens)."""
    taken = {d.chave for d in existing if d.tipo == "exame"}
    labs = {lab.key: lab for lab in knowledge.lab_catalog()}
    out: list[OrderDraft] = []

    def recent(lab: knowledge.LabDef) -> bool:
        if not lab.interval_days:
            return False
        for key in lab.result_keys:
            e = chart.latest_lab(key, max_days=lab.interval_days)
            if e is not None:
                return True
        return False

    def add(lab: knowledge.LabDef, why: str, refs: list[str], source: str) -> None:
        if lab.key in taken or recent(lab) or not refs or len(out) >= MAX_SUGGESTIONS:
            return
        taken.add(lab.key)
        d = OrderDraft(
            id=f"sug-{lab.key}",
            tipo="exame",
            acao="solicitar",
            item=lab.name,
            chave=lab.key,
            justificativa=why,
            refs=refs,
            fonte_pedido=source,  # type: ignore[arg-type]
            loinc=lab.loinc,
            loinc_nome=lab.loinc_name,
            categoria=lab.category,
            preparo=lab.prep,
        )
        d.checagens = _exam_checks(d, lab, ctx)
        out.append(_finish(d))

    pending = {(p.protocolo, p.item): p for p in protocols if p.status in ("pendente", "sem_registro")}
    for rule in knowledge.protocols():
        kind, _, spec = rule.source.partition(":")
        p = pending.get((rule.protocol, rule.item))
        if p is None:
            continue
        if kind == "lab":
            lab = next((x for x in labs.values() if spec in x.result_keys), None)
        else:  # item de texto ("Eletrocardiograma", "Albuminúria…"): vira pedido se o catálogo tem o exame
            item = norm(rule.item)
            lab = next(
                (x for x in labs.values() if any(re.search(rf"(?<![a-z0-9]){re.escape(w)}", item) for w in x.words)),
                None,
            )
        if lab:
            state = "sem registro" if p.status == "sem_registro" else "vencido"
            add(lab, f"Protocolo {rule.protocol} — {rule.item} ({state}). {rule.guidance}", p.refs, "protocolo")
    for h in hypotheses:
        text = norm(h.proximos_passos)
        for lab in labs.values():
            if any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", text) for w in lab.words):
                add(lab, f"Próximo passo de «{h.hipotese}» ({h.cid}).", h.refs, "hipotese")
    return out


# ---------- montagem ----------
def build(
    segments: list[Segment],
    chart: ChartView,
    ctx: OrderContext,
    protocols: list[ProtocolItem],
    hypotheses: list[Hypothesis],
    decisions: dict | None = None,
) -> list[OrderDraft]:
    """Rascunhos da fala + sugestões, cada um com as checagens de segurança e a decisão do profissional."""
    drafts = detect(segments, chart)
    labs = {lab.key: lab for lab in knowledge.lab_catalog()}
    for d in drafts:
        if d.tipo == "exame":
            d.checagens = _exam_checks(d, labs[d.chave], ctx)
        else:
            if d.chave.startswith("@"):
                ing, cls, name = "", d.chave[1:], d.item
            else:
                dr = drugs()[d.chave]
                ing, cls, name = dr.ingredient, dr.drug_class, dr.name
            d.checagens = _drug_checks(d, ing, cls, name, ctx, drafts)
        _finish(d)
    drafts += suggest(chart, protocols, hypotheses, drafts, ctx)
    apply_decisions(drafts, decisions or {})
    rank = {"critico": 0, "atencao": 1, "info": 2}
    return sorted(drafts, key=lambda d: (d.fonte_pedido != "fala", rank[d.gravidade]))


def apply_decisions(drafts: list[OrderDraft], decisions: dict) -> None:
    for d in drafts:
        raw = decisions.get(d.id)
        if not raw:
            continue
        edits = raw.get("edicao") or {}
        for f in ("dose", "via", "frequencia", "duracao"):
            if edits.get(f) is not None:
                setattr(d, f, edits[f])
        if "frequencia" in edits or "duracao" in edits:
            pos = parse_posology(norm(f"{d.frequencia} {d.duracao}"))
            d.periodicidade, d.duracao_dias = pos.periodicidade, pos.duracao_dias
        d.decisao = OrderDecision(
            estado=raw.get("estado", "pendente"),
            justificativa=raw.get("justificativa", ""),
            por=raw.get("por"),
            em=raw.get("em"),
            editado=bool(edits),
        )


# ---------- FHIR ----------
def _sentence(d: OrderDraft) -> str:
    parts = [d.item, d.dose, d.via, d.frequencia, d.duracao]
    return " · ".join(p for p in parts if p)


def _notes(d: OrderDraft) -> list[dict]:
    notes = [{"text": DRAFT_NOTE}, {"text": "Origem: " + ", ".join(d.refs)}]
    if d.decisao.justificativa:
        notes.append({"text": f"Justificativa do profissional: {d.decisao.justificativa}"})
    crit = [c.texto for c in d.checagens if c.severidade == "critico"]
    if crit:
        notes.append({"text": "Alerta crítico ao assinar: " + " | ".join(crit)})
    return notes


def medication_request(
    d: OrderDraft, *, patient_id: str, encounter_id: str | None, author: str, when: datetime, consultation_id: int
) -> dict:
    """MedicationRequest em RASCUNHO (status=draft, intent=proposal): não entra na prescrição ativa nem na reconciliação
    como medicamento em uso até alguém emiti-lo."""
    cc: dict = {"text": _sentence(d)}
    if not d.chave.startswith("@"):
        dr = drugs()[d.chave]
        cc["coding"] = [
            {"system": codes.INGREDIENT, "code": dr.ingredient, "display": dr.name},
            {"system": codes.ATC, "code": dr.atc},
        ]
    dosage: dict = {"text": _sentence(d) if d.acao != "suspender" else f"Suspender {d.item}"}
    if d.dose and d.acao != "suspender":
        m = re.match(r"([\d,.]+)\s*(.*)", d.dose)
        if m:
            quantity = {"value": float(m.group(1).replace(",", "."))} | ({"unit": m.group(2)} if m.group(2) else {})
            dosage["doseAndRate"] = [{"doseQuantity": quantity}]
    if d.periodicidade:
        repeat = {
            "frequency": d.periodicidade["frequencia"],
            "period": d.periodicidade["periodo"],
            "periodUnit": d.periodicidade["unidade"],
        }
        if d.duracao_dias:
            repeat["boundsDuration"] = {
                "value": d.duracao_dias,
                "unit": "dia",
                "system": "http://unitsofmeasure.org",
                "code": "d",
            }
        dosage["timing"] = {"repeat": repeat}
    elif d.duracao_dias:
        dosage["timing"] = {
            "repeat": {
                "boundsDuration": {
                    "value": d.duracao_dias,
                    "unit": "dia",
                    "system": "http://unitsofmeasure.org",
                    "code": "d",
                }
            }
        }
    if "SOS" in d.frequencia:
        dosage["asNeededBoolean"] = True
    route = d.via and route_coding(d.via)
    if d.via:
        dosage["route"] = {"text": d.via} | ({"coding": [route]} if route else {})
    resource: dict = {
        "resourceType": "MedicationRequest",
        "status": "draft",
        "intent": "proposal",
        "identifier": [{"system": ORDER_SYSTEM, "value": f"{consultation_id}/{d.id}"}],
        "medicationCodeableConcept": cc,
        "subject": {"reference": f"Patient/{patient_id}"},
        "authoredOn": when.isoformat(),
        "requester": {"display": author},
        "dosageInstruction": [dosage],
        "note": _notes(d),
    }
    if d.justificativa:
        resource["reasonCode"] = [{"text": d.justificativa}]
    if encounter_id:
        resource["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    if d.prescricao_ref:
        resource["priorPrescription"] = {"reference": d.prescricao_ref}
    if d.acao in ("alterar_dose", "suspender"):
        resource["note"].append(
            {
                "text": ("Proposta de mudança de dose" if d.acao == "alterar_dose" else "Proposta de suspensão")
                + (f" (dose atual: {d.dose_anterior})" if d.dose_anterior else "")
            }
        )
    return resource


def service_request(
    d: OrderDraft, *, patient_id: str, encounter_id: str | None, author: str, when: datetime, consultation_id: int
) -> dict:
    """ServiceRequest em RASCUNHO (status=draft, intent=proposal) com o LOINC e o preparo do exame."""
    cat = codes.SR_IMAGING if d.categoria == "imagem" else codes.SR_LAB
    resource: dict = {
        "resourceType": "ServiceRequest",
        "status": "draft",
        "intent": "proposal",
        "identifier": [{"system": ORDER_SYSTEM, "value": f"{consultation_id}/{d.id}"}],
        "category": [{"coding": [{"system": codes.SNOMED, "code": cat[0], "display": cat[1]}]}],
        "code": {"coding": [{"system": codes.LOINC, "code": d.loinc, "display": d.loinc_nome}], "text": d.item},
        "subject": {"reference": f"Patient/{patient_id}"},
        "authoredOn": when.isoformat(),
        "requester": {"display": author},
        "note": _notes(d),
    }
    if d.preparo:
        resource["patientInstruction"] = d.preparo
    if d.justificativa:
        resource["reasonCode"] = [{"text": d.justificativa}]
    if encounter_id:
        resource["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    return resource


def fhir_resource(
    d: OrderDraft, *, patient_id: str, encounter_id: str | None, author: str, when: datetime, consultation_id: int
) -> dict:
    build_ = medication_request if d.tipo == "medicamento" else service_request
    return build_(
        d, patient_id=patient_id, encounter_id=encounter_id, author=author, when=when, consultation_id=consultation_id
    )


def signed(orders: list[dict]) -> list[OrderDraft]:
    """Rascunhos assinados (a partir do JSON guardado em `insights_json`)."""
    out = [OrderDraft.model_validate(o) for o in orders]
    return [d for d in out if d.decisao.estado == "assinado"]
