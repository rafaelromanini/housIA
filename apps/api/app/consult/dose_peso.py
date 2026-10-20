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
from app.consult.orders import parse_posology
from app.consult.schemas import MedSuggestion, Segment, fala_ref
from app.consult.text import clauses, drug_hits, norm
from app.reference import drugs

TOLERANCE = 1.02  # arredondamento de gotas e frações de comprimido
UNDER = 0.75
MG = re.compile(r"(\d{1,3}(?:\.\d{3})+(?![\d,])|\d+(?:[.,]\d+)?)\s*(mg|g|gramas?|miligramas?)\b")
SPOKEN_WEIGHT = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*(?:kg|quilos?|kilos?)\b")
NEGATION = re.compile(r"\b(?:nao|nunca|sem)\b")


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
    over: list[str] = []
    if per_kg > row.kg_max * TOLERANCE:
        over.append(
            f"{_n(per_kg)} mg/kg/dose, acima do máximo de {_n(row.kg_max)} mg/kg/dose "
            f"({_n(row.kg_max * weight)} mg para {_n(weight)} kg)"
        )
    if row.max_dose is not None and dose_mg > row.max_dose * TOLERANCE:
        over.append(f"{_n(dose_mg)} mg por dose, acima do teto de {_n(row.max_dose)} mg por dose")
    daily = None
    if interval_h:
        daily = dose_mg * (24 / interval_h)
        if row.max_kg_day is not None and daily / weight > row.max_kg_day * TOLERANCE:
            over.append(
                f"{_n(daily / weight)} mg/kg/dia ({_n(daily)} mg/dia), acima do máximo de {_n(row.max_kg_day)} "
                f"mg/kg/dia ({_n(row.max_kg_day * weight)} mg/dia para {_n(weight)} kg)"
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
    if per_kg < row.kg_min * UNDER:
        return Achado(
            "atencao",
            f"{name}: dose abaixo da faixa para {_n(weight)} kg — {_n(per_kg)} mg/kg/dose, abaixo do mínimo de "
            f"{_n(row.kg_min)} mg/kg/dose ({_n(row.kg_min * weight)} mg); risco de subdose.",
            row.source,
        )
    return None


def spoken_weight(segments: list[Segment]) -> tuple[float, str] | None:
    """Peso dito na conversa ("ela pesa 12 quilos"), só quando o prontuário não tem peso recente."""
    for s in reversed(segments):
        text = norm(s.texto)  # a vírgula decimal ("12,5 quilos") não pode separar orações
        if re.search(r"\b(?:pesa|pesou|peso|pesando)\b", text) and (m := SPOKEN_WEIGHT.search(text)):
            return float(m.group(1).replace(",", ".")), fala_ref(s.n)
    return None


def _weight(engine) -> tuple[float, str, str] | None:
    """(kg, ref, rótulo da origem): o último Observation de peso, senão o peso dito na conversa."""
    w = engine.chart.weight()
    if w:
        events = engine.chart.vitals.get("peso", [])
        when = fmt_date(events[-1].timestamp) if events else None
        return w[0], w[1], f"último peso registrado{f' em {when}' if when else ''}"
    said = spoken_weight(engine.segments)
    return (said[0], said[1], "peso dito na consulta") if said else None


def _mentions(engine, table: dict) -> list[tuple[str, float, float | None, bool, str]]:
    """(princípio, mg, intervalo em h, dose citada na fala?, ref) das doses ditas e das prescritas no prontuário."""
    found: list[tuple[str, float, float | None, bool, str]] = []
    for s in engine.segments:
        if s.origem == "marco":
            continue
        for clause in clauses(s.texto):
            hits = [h for h in drug_hits(clause) if h[2] in table]
            t = f" {clause} "
            for i, (start, end, ing, _term) in enumerate(hits):
                prev_end = hits[i - 1][1] if i else 0
                nxt = hits[i + 1][0] if i + 1 < len(hits) else len(t)
                before, after = t[prev_end:start], t[end:nxt]
                if NEGATION.search(before):
                    continue
                pos = parse_posology(after) if parse_mg(parse_posology(after).dose) else parse_posology(before + after)
                mg = parse_mg(pos.dose)
                if mg is None:
                    continue
                found.append((ing, mg, interval_hours(pos.periodicidade), True, fala_ref(s.n)))
    for m in engine.chart.active_meds:
        mg = parse_mg(m.dose)
        if m.ingredient in table and mg is not None:
            interval = None if m.prn or not m.times else 24 / len(m.times)
            found.append((m.ingredient, mg, interval, False, m.ref))
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
    for ing, mg, interval, _spoken, ref in mentions:
        key = (ing, mg, interval)
        if key in seen:
            if ref not in seen[key].refs:
                seen[key].refs.append(ref)
            continue
        if weight is None:
            name = drugs()[ing].name if ing in drugs() else ing
            sug = MedSuggestion(
                texto=f"{name} {_n(mg)} mg: sem peso registrado no último ano. Pesar a criança e conferir a dose "
                "por kg antes de prescrever.",
                tipo="ajuste_dose",
                severidade="atencao",
                refs=[ref],
                fonte=table[ing].source,
            )
        else:
            kg, wref, origin = weight
            found = avaliar(ing, mg, interval, kg, age)
            if found is None:
                continue
            freq = f", {_n(24 / interval)} vezes ao dia" if interval else ""
            sug = MedSuggestion(
                texto=f"{found.texto} Dose citada: {_n(mg)} mg{freq} ({origin}).",
                tipo="ajuste_dose",
                severidade=found.severidade,  # type: ignore[arg-type]
                refs=list(dict.fromkeys([ref, wref])),
                fonte=found.fonte,
            )
        seen[key] = sug
        out.append(sug)
    return out
