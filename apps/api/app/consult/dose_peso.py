"""Dose pediátrica por peso (#27): confere a dose citada na conversa ou prescrita contra pediatric_doses.csv.

Roda nas regras, antes de qualquer IA. Compara com o ÚLTIMO PESO registrado (Observation) e, na falta dele, com o peso
dito na conversa. Dose acima do máximo (por kg, por dose ou por dia) ou intervalo curto demais é alerta crítico com a
fonte (bula ANVISA ou SBP); abaixo da faixa é atenção. Sem peso, o alerta pede o peso: não há conferência cega.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.consult import knowledge
from app.consult.chart import fmt_date, fmt_num
from app.consult.orders import NUMBER_WORDS, QTY, parse_posology
from app.consult.schemas import MedSuggestion, Segment, fala_ref
from app.consult.text import CLAUSE_SPLIT, drug_hits, norm
from app.reference import drugs

TOLERANCE = 1.02  # arredondamento de gotas e frações de comprimido
UNDER = 0.75
ADULT_KG = 50.0  # a partir daqui o adolescente toma dose de adulto: valem só os tetos absolutos (mg/dose e mg/dia)
STALE_DAYS = 90  # peso do prontuário mais velho que isso cede ao peso dito na consulta (criança cresce)
PLAUSIBLE_KG = (0.3, 400.0)
MG = re.compile(r"(\d{1,3}(?:\.\d{3})+(?![\d,])|\d+(?:[.,]\d+)?)\s*(mg|g|gramas?|miligramas?)\b")
SPOKEN_WEIGHT = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*(?:kg|quilos?|kilos?)\b")
NOT_CHILD_WEIGHT = re.compile(r"\b(?:nasc\w*|eu peso|eu pesava|eu pesei|meu peso|eu estou pesando)\b")
NEGATION = re.compile(r"\b(?:nao|nunca|sem)\b")
CONC = re.compile(rf"{QTY}\s*mg\s*(?:/|por|em|a cada)\s*{QTY}?\s*ml\b")  # "250 mg/5 ml": concentração, não dose
VOL = re.compile(rf"(?<![\w,.]){QTY}\s*ml\b")
POR_KG = re.compile(r"\bmg\s*(?:/|por)\s*(?:kg|quilo)\b")
POR_KG_DIA = re.compile(r"\b(?:kg|quilo)\s*(?:/|por)\s*dia\b")
_PALAVRAS = "|".join(NUMBER_WORDS)
DIVIDIDA = re.compile(rf"\bdividid[oa]s? em (\d+|{_PALAVRAS})\b")
DIARIA = re.compile(r"(?:/|\bpor|\bao|\bno)\s*dia\b|\bdose diaria\b|\bem 24 horas\b|\bno total\b")
POR_EXTENSO = re.compile(rf"\bde ({_PALAVRAS}) em \1 horas?\b")


@dataclass
class Achado:
    severidade: str  # critico | atencao
    texto: str
    fonte: str


def parse_mg(dose: str) -> float | None:
    """'500 mg' → 500; '1 g' → 1000; '1.000 mg' → 1000; unidades que não são massa (ml, gotas) → None."""
    m = MG.search((dose or "").lower().replace("miligrama", "mg"))
    if not m:
        return None
    raw, unit = m.group(1), m.group(2)
    value = float(raw.replace(".", "") if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw) else raw.replace(",", "."))
    return value * 1000 if unit.startswith(("g", "gramas")) and not unit.startswith("mg") else value


def interval_hours(periodicidade: dict | None) -> float | None:
    """Intervalo entre doses a partir do Timing.repeat ({"frequencia": 3, "periodo": 1, "unidade": "d"})."""
    if not periodicidade:
        return None
    per = {"h": 1, "d": 24, "wk": 168, "mo": 720}.get(periodicidade.get("unidade", ""))
    freq = periodicidade.get("frequencia") or 0
    return per * periodicidade.get("periodo", 1) / freq if per and freq else None


def _n(value: float) -> str:
    return fmt_num(value, 0 if abs(value - round(value)) < 0.05 else 1)


def avaliar(ingredient: str, dose_mg: float, interval_h: float | None, weight: float, age: int | None) -> Achado | None:
    """Confere uma dose (mg) com o peso (kg). Devolve o achado ou None se está na faixa."""
    row = knowledge.pediatric_doses().get(ingredient)
    if row is None or (age is not None and age > row.age_max) or weight <= 0:
        return None
    name = drugs()[ingredient].name if ingredient in drugs() else ingredient
    per_kg = dose_mg / weight
    adult = weight >= ADULT_KG  # adolescente grande: dose de adulto, só os tetos absolutos valem
    over: list[str] = []
    if not adult and per_kg > row.kg_max * TOLERANCE:
        over.append(
            f"{_n(per_kg)} mg/kg/dose, acima do máximo de {_n(row.kg_max)} mg/kg/dose "
            f"({_n(row.kg_max * weight)} mg para {_n(weight)} kg)"
        )
    if row.max_dose is not None and dose_mg > row.max_dose * TOLERANCE:
        over.append(f"{_n(dose_mg)} mg por dose, acima do teto de {_n(row.max_dose)} mg por dose")
    daily = None
    if interval_h:
        daily = dose_mg * (24 / interval_h)
        if not adult and row.max_kg_day is not None and daily / weight > row.max_kg_day * TOLERANCE:
            over.append(
                f"{_n(daily / weight)} mg/kg/dia ({_n(daily)} mg/dia; {_n(per_kg)} mg/kg/dose), acima do máximo de "
                f"{_n(row.max_kg_day)} mg/kg/dia ({_n(row.max_kg_day * weight)} mg/dia para {_n(weight)} kg)"
            )
        elif row.max_day is not None and daily > row.max_day * TOLERANCE:
            over.append(f"{_n(daily)} mg/dia, acima do teto de {_n(row.max_day)} mg/dia")
        if interval_h < row.interval_h * 0.99:
            over.append(f"intervalo de {_n(interval_h)} h, menor que o mínimo de {_n(row.interval_h)} h")
    if over:
        return Achado(
            "critico",
            f"{name}: dose acima do máximo para o peso de {_n(weight)} kg — " + "; ".join(over) + ".",
            row.source,
        )
    if not adult and per_kg < row.kg_min * UNDER:
        return Achado(
            "atencao",
            f"{name}: dose abaixo da faixa para {_n(weight)} kg — {_n(per_kg)} mg/kg/dose, abaixo do mínimo de "
            f"{_n(row.kg_min)} mg/kg/dose ({_n(row.kg_min * weight)} mg); risco de subdose.",
            row.source,
        )
    return None


def pediatric_ml(ingredient: str, dose: str, age: int | None) -> bool:
    """Dose em mL de fármaco da tabela pediátrica, em criança/adolescente: o mL não vira mg/kg sem a concentração."""
    row = knowledge.pediatric_doses().get(ingredient)
    return bool(row and age is not None and age <= row.age_max and parse_mg(dose) is None and VOL.search(dose.lower()))


def spoken_weight(segments: list[Segment]) -> tuple[float, str] | None:
    """Peso dito na conversa ("ela pesa 12 quilos"), quando o prontuário não tem peso recente. Ignora o peso ao
    nascer e o peso de quem fala de si ("eu peso 60 quilos"), que não é o da criança."""
    for s in reversed(segments):
        text = norm(s.texto)  # a vírgula decimal ("12,5 quilos") não pode separar orações
        if NOT_CHILD_WEIGHT.search(text):
            continue
        if re.search(r"\b(?:pesa|pesou|peso|pesando)\b", text) and (m := SPOKEN_WEIGHT.search(text)):
            kg = float(m.group(1).replace(",", "."))
            if PLAUSIBLE_KG[0] <= kg <= PLAUSIBLE_KG[1]:
                return kg, fala_ref(s.n)
    return None


def _weight(engine) -> tuple[float, str, str] | None:
    """(kg, ref, rótulo da origem): o último Observation de peso utilizável (kg, plausível, até 1 ano); se ele tem
    mais de 90 dias e a conversa traz um peso, vale o dito; sem nenhum dos dois, não há conferência cega."""
    e = engine.chart.weight_event()
    said = spoken_weight(engine.segments)
    if e is None:
        return (said[0], said[1], "peso dito na consulta") if said else None
    days = (engine.chart.now - e.timestamp).days
    if days > STALE_DAYS and said:
        return said[0], said[1], "peso dito na consulta, mais recente que o do prontuário"
    old = f"; peso com {days} dias, pesar de novo" if days > STALE_DAYS else ""
    return float(e.value["valor"]), e.source_ref, f"último peso registrado em {fmt_date(e.timestamp)}{old}"


@dataclass
class Citada:
    """Dose dita ou prescrita: `valor` em mg por tomada, ou em mg/kg por tomada (`por_kg`); None = só em mL."""

    ing: str
    valor: float | None
    intervalo: float | None
    ref: str
    rotulo: str  # como a dose foi citada, para o texto do alerta
    por_kg: bool = False
    ml: float | None = None


def _f(raw: str) -> float:
    return float(raw.replace(".", "") if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw) else raw.replace(",", "."))


def _orações(text: str) -> list[str]:
    """Orações da fala sem partir o decimal ("0,5 g", "62,5 mg", "2,5 ml") nem o milhar ("1.000 mg")."""
    t = re.sub(r"(?<=\d)([,.])(?=\d)", lambda m: "\x01" if m.group(1) == "," else "\x02", norm(text))
    parts = [c.strip() for c in CLAUSE_SPLIT.split(f" {t} ") if c.strip()]
    return [
        POR_EXTENSO.sub(lambda m: f"de {NUMBER_WORDS[m.group(1)]} em {NUMBER_WORDS[m.group(1)]} horas", c)
        .replace("\x01", ",")
        .replace("\x02", ".")
        for c in parts
    ]


def _citada(ing: str, pos, win: str, ref: str) -> Citada | None:
    """Converte a posologia lida (dose, intervalo) em dose por tomada: mg, mg/kg ou só mL; total do dia ("500 mg por
    dia", "1500 mg por dia dividido em 3 doses", "50 mg/kg/dia") é dividido pelas tomadas."""
    mg = parse_mg(pos.dose)
    interval = interval_hours(pos.periodicidade)
    if mg is None:
        m = VOL.search(pos.dose.lower())
        if m:
            return Citada(ing, None, interval, ref, f"{pos.dose}", ml=_f(m.group(1)))
        return None
    por_kg = bool(POR_KG.search(win))
    if (div := DIVIDIDA.search(win)) is not None:
        n = float(div.group(1)) if div.group(1).isdigit() else float(NUMBER_WORDS[div.group(1)])
        daily = n > 0
    elif POR_KG_DIA.search(win):
        n, daily = (24 / interval if interval else 1.0), True
    else:
        n, daily = 1.0, pos.periodicidade is None and bool(DIARIA.search(win))
    if daily:
        interval = 24 / n
    unit = "mg/kg" if por_kg else "mg"
    rotulo = f"{fmt_num(mg, 1 if mg % 1 else 0)} {unit}" + ("/dia" if daily and (n > 1 or por_kg) else "")
    return Citada(ing, mg / n if daily else mg, interval, ref, rotulo, por_kg=por_kg)


def _mentions(engine, table: dict) -> list[Citada]:
    """Doses ditas na conversa (por qualquer falante) e as prescritas no prontuário, por fármaco da tabela."""
    found: list[Citada] = []
    for s in engine.segments:
        if s.origem == "marco":
            continue
        ref = fala_ref(s.n)
        text = norm(s.texto)
        texts = [text]
        if conc := CONC.search(text):  # "amoxicilina 250 mg/5 ml, 10 ml": a dose é o volume × concentração
            mg_ml = _f(conc.group(1)) / (_f(conc.group(2)) if conc.group(2) else 1.0)
            rest = text[: conc.start()] + " " + text[conc.end() :]
            hits = [h for h in drug_hits(rest) if h[2] in table]
            vol = VOL.search(rest)
            if len({h[2] for h in hits}) == 1 and vol and not NEGATION.search(rest[: hits[0][0]]):
                pos = parse_posology(rest[hits[0][1] :])
                mg = _f(vol.group(1)) * mg_ml
                interval = interval_hours(pos.periodicidade)
                label = f"{vol.group(1)} ml × {fmt_num(mg_ml, 1 if mg_ml % 1 else 0)} mg/ml = {fmt_num(mg, 0)} mg"
                found.append(Citada(hits[0][2], mg, interval, ref, label))
                continue
            texts = [rest]
        for clause in (c for t in texts for c in _orações(t)):
            hits = [h for h in drug_hits(clause) if h[2] in table]
            t = f" {clause} "
            for i, (start, end, ing, _term) in enumerate(hits):
                prev_end = hits[i - 1][1] if i else 0
                nxt = hits[i + 1][0] if i + 1 < len(hits) else len(t)
                before, after = t[prev_end:start], t[end:nxt]
                if NEGATION.search(before):
                    continue
                pos_after = parse_posology(after)
                in_after = bool(pos_after.dose) and (parse_mg(pos_after.dose) is not None or "ml" in pos_after.dose)
                pos, win = (pos_after, after) if in_after else (parse_posology(before + after), before + after)
                if c := _citada(ing, pos, win, ref):
                    found.append(c)
    for m in engine.chart.active_meds:
        if m.ingredient not in table:
            continue
        mg = parse_mg(m.dose)
        interval = None if m.prn or not m.times else 24 / len(m.times)
        if mg is not None:
            found.append(Citada(m.ingredient, mg, interval, m.ref, f"{_n(mg)} mg"))
        elif vol := VOL.search((m.dose or "").lower()):
            found.append(Citada(m.ingredient, None, interval, m.ref, m.dose, ml=_f(vol.group(1))))
    return found


def alerts(engine) -> list[MedSuggestion]:
    """Alertas de dose pediátrica por peso (criança/adolescente), com a fonte e as refs da dose e do peso."""
    age = engine.r.age
    table = knowledge.pediatric_doses()
    if age is None or age > max(r.age_max for r in table.values()):
        return []
    mentions = _mentions(engine, table)
    if not mentions:
        return []
    weight = _weight(engine)
    out: list[MedSuggestion] = []
    seen: dict[tuple, MedSuggestion] = {}
    for c in mentions:
        key = (c.ing, c.valor, c.intervalo, c.por_kg, c.ml)
        if key in seen:
            if c.ref not in seen[key].refs:
                seen[key].refs.append(c.ref)
            continue
        name = drugs()[c.ing].name if c.ing in drugs() else c.ing
        row = table[c.ing]
        if c.valor is None:  # dose só em mL: sem a concentração não dá para chegar em mg/kg
            sug = MedSuggestion(
                texto=f"{name} {c.rotulo}: dose em mL sem a concentração (mg/mL) dita, então não foi conferida por kg. "
                "Confirmar a concentração do frasco e a dose por kg antes de prescrever.",
                tipo="ajuste_dose",
                severidade="atencao",
                refs=[c.ref],
                fonte=row.source,
            )
        elif weight is None:
            sug = MedSuggestion(
                texto=f"{name} {c.rotulo}: sem peso registrado no último ano. Pesar a criança e conferir a dose "
                "por kg antes de prescrever.",
                tipo="ajuste_dose",
                severidade="atencao",
                refs=[c.ref],
                fonte=row.source,
            )
        else:
            kg, wref, origin = weight
            mg = c.valor * kg if c.por_kg else c.valor
            found = avaliar(c.ing, mg, c.intervalo, kg, age)
            if found is None:
                continue
            freq = f", {_n(24 / c.intervalo)} vezes ao dia" if c.intervalo else ""
            sug = MedSuggestion(
                texto=f"{found.texto} Dose citada: {c.rotulo}{freq} ({origin}).",
                tipo="ajuste_dose",
                severidade=found.severidade,  # type: ignore[arg-type]
                refs=list(dict.fromkeys([c.ref, wref])),
                fonte=found.fonte,
            )
        seen[key] = sug
        out.append(sug)
    return out
