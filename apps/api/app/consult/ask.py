"""Copiloto da consulta ("Discutir o caso" / "Perguntar ao prontuário"): pergunta livre com fonte em cada frase.

Primeiro as intenções resolvidas por regra sobre o prontuário (ChartView) e o Engine, com as refs FHIR e a fonte da
regra; só o que sobra vai ao LLM, que recebe os resultados das regras como ferramentas. O LLM nunca calcula: número
que não está nos dados recebidos derruba a frase. Frase sem ref válida (ou, nas de regra, sem fonte) é descartada.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from app.consult import ai
from app.consult.chart import ChartView, fmt_date, fmt_num
from app.consult.engine import PLANNED_SUFFIX, Engine
from app.consult.schemas import Insights, MedSuggestion
from app.consult.text import drug_hits, norm
from app.fhir_client import codes
from app.llm.client import generate_structured
from app.reference import class_labels, drugs, interactions, matches

MAX_QUESTION = 500
HYPOTHETICAL = "@pergunta"  # ref provisória do medicamento perguntado (ainda não existe no prontuário)
NOTE_TARGETS = ("hda", "antecedentes", "medicacoes", "alergias", "exames_complementares", "avaliacao", "plano")


# ---------- modelos ----------
class Frase(BaseModel):
    texto: str
    refs: list[str] = []
    fonte: str = ""  # regra de onde vem (dose_alerts.csv, interactions.csv, KDIGO...)
    origem: Literal["regra", "ia"] = "regra"


class Resposta(BaseModel):
    intencao: str  # nome da intenção resolvida por regra, ou "ia" / "sem_resposta"
    modo: Literal["regras", "ia"] = "regras"
    frases: list[Frase] = []
    aviso: str = ""  # o que o copiloto não fez (não é afirmação clínica, por isso não leva fonte)
    descartados: list[dict] = []
    acao: dict | None = None  # comando executado ("adicionar ao plano", "criar pendência")


class AskFraseIA(BaseModel):
    texto: str = Field(description="uma afirmação curta, sem cálculos próprios")
    refs: list[str] = Field(description="Fala/N e/ou recursos FHIR exatamente como recebidos")


class AskOutput(BaseModel):
    frases: list[AskFraseIA]


@dataclass
class Command:
    destino: Literal["plano", "pendencia"]
    texto: str


@dataclass
class Ctx:
    engine: Engine
    ins: Insights
    q: str  # pergunta original
    n: str  # normalizada
    chart: ChartView = field(init=False)

    def __post_init__(self):
        self.chart = self.engine.chart


# ---------- comandos ----------
_PLAN = re.compile(
    r"^\s*(?:por favor[,:]?\s*)?(?:adicionar|adicione|incluir|inclua|acrescentar|acrescente|colocar|coloque)\s+"
    r"(?:isso\s+|isto\s+)?(?:ao|no|em)\s+plano\s*[:,\-–]?\s*(.*)$",
    re.I | re.S,
)
_PEND = re.compile(
    r"^\s*(?:por favor[,:]?\s*)?(?:criar|crie|abrir|abra|registrar|registre|adicionar|adicione)\s+"
    r"(?:uma\s+|a\s+)?pend[eê]ncia\s*(?:de|para|sobre)?\s*[:,\-–]?\s*(.*)$",
    re.I | re.S,
)


def parse_command(question: str) -> Command | None:
    for pattern, destino in ((_PLAN, "plano"), (_PEND, "pendencia")):
        m = pattern.match(question)
        if m:
            return Command(destino, m.group(1).strip(" .\n"))  # type: ignore[arg-type]
    return None


# ---------- vocabulário ----------
def _lab_terms() -> list[tuple[str, str]]:
    """(termo normalizado, chave do exame no prontuário), do mais longo ao mais curto."""
    terms: dict[str, str] = {}
    for key, m in codes.LABS.items():
        terms[norm(key)] = key
        terms[norm(m.label)] = key
    terms |= {
        "k": "potassio",
        "na": "sodio",
        "glicemia": "glicose",
        "glicemia de jejum": "glicose",
        "hemoglobina glicada": "hba1c",
        "glicada": "hba1c",
        "a1c": "hba1c",
        "hb": "hemoglobina",
        "plaqueta": "plaquetas",
        "leucocito": "leucocitos",
        "ldl": "ldl",
        "colesterol ldl": "ldl",
        "colesterol": "ldl",
        "proteina c reativa": "pcr",
        "tfg": "tfg",
        "taxa de filtracao glomerular": "tfg",
        "filtracao glomerular": "tfg",
        "funcao renal": "tfg",
        "clearance": "tfg",
    }
    return sorted(terms.items(), key=lambda kv: -len(kv[0]))


_LAB_TERMS = _lab_terms()
_VITAL_TERMS = {
    "peso": ("peso",),
    "pressao": ("pas", "pad"),
    "pa": ("pas", "pad"),
    "frequencia cardiaca": ("fc",),
    "saturacao": ("spo2",),
    "temperatura": ("temp",),
}


def _has_word(text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def _lab_keys(n: str) -> list[str]:
    found: list[str] = []
    rest = n
    for term, key in _LAB_TERMS:
        if _has_word(rest, term):
            if key not in found:
                found.append(key)
            rest = re.sub(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", " ", rest)
    return found


def _drugs_in(n: str) -> list[tuple[str, str]]:
    """(princípio ativo ou @classe, termo) citados na pergunta, incluindo siglas de classe (AINE, IECA, BRA...)."""
    hits = [(h[2], h[3]) for h in drug_hits(f" {n} ")]
    seen = {h[0] for h in hits}
    labels = {norm(rot): cls for cls, rot in class_labels().items()}
    labels |= {norm(cls): cls for cls in class_labels()}
    labels |= {"aines": "aine", "anti inflamatorios": "aine", "anticoagulantes": "anticoagulante_oral"}
    for term, cls in sorted(labels.items(), key=lambda kv: -len(kv[0])):
        if len(term) < 3 or f"@{cls}" in seen or not _has_word(n, term):
            continue
        if any(cls == drugs()[h[0]].drug_class for h in hits if h[0] in drugs()):
            continue
        hits.append((f"@{cls}", class_labels().get(cls, term)))
        seen.add(f"@{cls}")
    return hits


def _name(ing: str, term: str) -> str:
    if ing.startswith("@"):
        return class_labels().get(ing[1:], term)
    return drugs()[ing].name if ing in drugs() else term


def _drug_class(ing: str) -> str:
    return ing[1:] if ing.startswith("@") else (drugs()[ing].drug_class if ing in drugs() else "")


def _in_chart_ref(c: ChartView, ing: str) -> str | None:
    for m in c.active_meds:
        if (ing.startswith("@") and m.drug_class == ing[1:]) or m.ingredient == ing:
            return m.ref
    return None


# ---------- intenções ----------
def _egfr_frase(c: ChartView) -> Frase | None:
    egfr = c.egfr
    if egfr is None:
        return None
    value, cr = egfr
    return Frase(
        texto=f"TFG estimada ~{round(value)} mL/min/1,73 m² (CKD-EPI 2021, creatinina {fmt_num(float(cr.value['valor']), 1)}"  # noqa: E501
        f" mg/dL em {fmt_date(cr.timestamp)}).",
        refs=[cr.source_ref],
        fonte="CKD-EPI 2021 (calculado pelo HousIA, não pela IA)",
    )


def _lab_frase(c: ChartView, key: str) -> Frase | None:
    if key == "tfg":
        return _egfr_frase(c)
    last = c.latest_lab(key, max_days=3650)
    if last is None:
        return None
    m = codes.LABS[key]
    value = float(last.value["valor"])
    unit = f" {m.unit}" if m.unit else ""
    flag = codes.classify(m, value)
    text = f"{m.label}: {fmt_num(value, m.decimals)}{unit} em {fmt_date(last.timestamp)}"
    text += " (fora da faixa de referência)" if flag != "normal" else " (dentro da faixa de referência)"
    refs = [last.source_ref]
    prev = [e for e in c.labs.get(key, []) if e.timestamp < last.timestamp]
    if prev:
        p = prev[-1]
        text += f"; anterior {fmt_num(float(p.value['valor']), m.decimals)}{unit} em {fmt_date(p.timestamp)}"
        refs.append(p.source_ref)
    return Frase(texto=text + ".", refs=refs, fonte="Prontuário (Observation) e faixas de referência do HousIA")


def intent_valor(ctx: Ctx) -> Resposta | None:
    keys = _lab_keys(ctx.n)
    vit = [(t, ks) for t, ks in _VITAL_TERMS.items() if _has_word(ctx.n, t)]
    exam_words = _has_word(ctx.n, "exames") or _has_word(ctx.n, "exame") or _has_word(ctx.n, "laboratorio")
    if not keys and not vit and not exam_words:
        return None
    frases: list[Frase] = []
    aviso = ""
    c = ctx.chart
    for key in keys:
        f = _lab_frase(c, key)
        if f:
            frases.append(f)
        else:
            label = "TFG" if key == "tfg" else codes.LABS[key].label
            aviso += f"Sem {label} registrado no prontuário. "
    for term, ks in vit:
        if term == "peso":
            w = c.weight()
            if w:
                frases.append(
                    Frase(texto=f"Último peso registrado: {fmt_num(w[0], 1)} kg.", refs=[w[1]], fonte="Prontuário")
                )
            else:
                aviso += "Sem peso registrado no último ano. "
            continue
        vals = [c.latest_vital(k, 72) for k in ks]
        if all(v is not None for v in vals):
            parts = " / ".join(fmt_num(float(v.value["valor"]), 0) for v in vals if v)
            m = codes.VITALS[ks[0]]
            frases.append(
                Frase(
                    texto=f"{m.label.replace(' sistólica', '')}: {parts} {m.unit} em {fmt_date(vals[0].timestamp)}.",  # type: ignore[union-attr]
                    refs=[v.source_ref for v in vals if v],
                    fonte="Prontuário (sinais vitais das últimas 72 h)",
                )
            )
        else:
            aviso += f"Sem {term} recente no prontuário. "
    if not keys and not vit and exam_words:
        for e in c.exams()[:6]:
            frases.append(
                Frase(
                    texto=f"{e.exame}: {e.valor} em {e.data} ({e.status})"
                    + (f"; {e.tendencia}" if e.tendencia else "")
                    + ".",
                    refs=e.refs,
                    fonte="Prontuário (Observation) e faixas de referência do HousIA",
                )
            )
        if not frases:
            aviso = "Nenhum exame recente no prontuário."
    return Resposta(intencao="valor_exame", frases=frases, aviso=aviso.strip())


def intent_alergias(ctx: Ctx) -> Resposta | None:
    if not _has_word(ctx.n, "alergia") and not _has_word(ctx.n, "alergias") and "alergic" not in ctx.n:
        return None
    frases = [
        Frase(
            texto=f"{a.alergeno}"
            + (f" — {a.reacao}" if a.reacao else "")
            + ("" if "criticidade" in (a.reacao or "") else f" (criticidade {a.criticidade})")
            + ("; relatada na consulta, ainda não registrada" if a.fonte == "relatado" else "")
            + ".",
            refs=a.refs,
            fonte=a.fonte_regra or "Prontuário (AllergyIntolerance)",
        )
        for a in ctx.ins.alergias
    ]
    aviso = ""
    if not frases:
        aviso = (
            "O prontuário registra que o paciente nega alergias conhecidas."
            if ctx.chart.ctx.nega_alergias
            else "Sem alergias registradas no prontuário."
        )
    return Resposta(intencao="alergias", frases=frases, aviso=aviso)


def _since(ctx: Ctx):
    """Data de término da última consulta ambulatorial finalizada (antes da atual)."""
    from app.timeline.record import parse_dt

    best = None
    for e in ctx.chart.r.encounters.values():
        if e.get("class", {}).get("code") != "AMB" or e.get("status") != "finished":
            continue
        end = parse_dt(e.get("period", {}).get("end") or e.get("period", {}).get("start"))
        if end and (best is None or end > best[0]):
            best = (end, e)
    return best


def intent_mudou(ctx: Ctx) -> Resposta | None:
    if not re.search(r"\b(mudou|mudanca|mudancas|novidade|novidades|evoluiu|evolucao desde|desde a ultima)\b", ctx.n):
        return None
    c = ctx.chart
    last = _since(ctx)
    if last is None:
        return Resposta(intencao="mudou", aviso="Não há consulta anterior finalizada no prontuário para comparar.")
    when, enc = last
    since = fmt_date(when)
    frases: list[Frase] = []
    for key, items in c.labs.items():
        new = [e for e in items if e.timestamp > when]
        if not new:
            continue
        cur = new[-1]
        before = [e for e in items if e.timestamp <= when]
        m = codes.LABS[key]
        unit = f" {m.unit}" if m.unit else ""
        if not before:
            frases.append(
                Frase(
                    texto=f"{m.label}: novo exame, {fmt_num(float(cur.value['valor']), m.decimals)}{unit} em {fmt_date(cur.timestamp)}.",  # noqa: E501
                    refs=[cur.source_ref],
                    fonte="Prontuário (Observation)",
                )
            )
            continue
        a, b = float(before[-1].value["valor"]), float(cur.value["valor"])
        if abs(b - a) < max(0.05 * abs(a), 10**-m.decimals):
            continue
        frases.append(
            Frase(
                texto=f"{m.label} {'subiu' if b > a else 'caiu'} de {fmt_num(a, m.decimals)} para {fmt_num(b, m.decimals)}{unit}"  # noqa: E501
                f" ({fmt_date(before[-1].timestamp)} → {fmt_date(cur.timestamp)}).",
                refs=[before[-1].source_ref, cur.source_ref],
                fonte="Prontuário (Observation); comparação feita pelo HousIA",
            )
        )
    for m in c.r.medications:
        if m.start and m.start > when and m.status == "active":
            frases.append(
                Frase(
                    texto=f"{m.name} iniciado em {fmt_date(m.start)}.",
                    refs=[m.ref],
                    fonte="Prontuário (MedicationRequest)",
                )
            )
        elif m.status == "stopped" and m.end and m.end > when:
            frases.append(
                Frase(
                    texto=f"{m.name} suspenso em {fmt_date(m.end)}.",
                    refs=[m.ref],
                    fonte="Prontuário (MedicationRequest)",
                )
            )
    last_meds = [m for m in c.active_meds if m.encounter_id == enc["id"] and m.start]
    for m in last_meds:
        frases.append(
            Frase(
                texto=f"Prescrito na última consulta ({since}): {m.name}.",
                refs=[m.ref],
                fonte="Prontuário (MedicationRequest)",
            )
        )
    for ing, term, ref in ctx.engine.reported_meds():
        if ing not in {m.ingredient for m in c.active_meds}:
            frases.append(
                Frase(
                    texto=f"Relatado hoje, sem prescrição no prontuário: {_name(ing, term)}.",
                    refs=[ref],
                    fonte="Fala da consulta",
                )
            )
    for cond in c.r.active_conditions:
        from app.timeline.record import parse_dt

        onset = parse_dt(cond.get("onsetDateTime"))
        if onset and onset > when:
            text = cond.get("code", {}).get("text", "Diagnóstico")
            frases.append(
                Frase(
                    texto=f"Novo diagnóstico: {text} ({fmt_date(onset)}).",
                    refs=[f"Condition/{cond['id']}"],
                    fonte="Prontuário (Condition)",
                )
            )
    aviso = "" if frases else f"Nada novo no prontuário desde a última consulta ({since})."
    return Resposta(intencao="mudou", frases=frases, aviso=aviso)


def intent_alertas(ctx: Ctx) -> Resposta | None:
    if not re.search(
        r"\b(alerta|alertas|risco|riscos|perigo|critico|criticos|discutir|preocupa|preocupacao|seguranca)\b", ctx.n
    ):
        return None
    items = [m for m in ctx.ins.sugestoes_medicamentos if m.severidade in ("critico", "atencao")]
    frases = [Frase(texto=m.texto, refs=m.refs, fonte=m.fonte) for m in items[:8]]
    aviso = "" if frases else "Nenhum alerta de segurança de medicamentos no momento."
    return Resposta(intencao="alertas", frases=frases, aviso=aviso)


# ---------- medicamentos: "posso usar X?", dose, interação ----------
def _evaluate(ctx: Ctx, ing: str, term: str) -> list[MedSuggestion]:
    """Alertas que o medicamento perguntado geraria agora: o que as regras já sabem do paciente + o hipotético."""
    eng = ctx.engine
    allergies = eng.allergies([(ing, term, HYPOTHETICAL)])
    base = {m.texto for m in eng.med_safety(eng.allergies([]), None, [])}
    with_ = eng.med_safety(allergies, None, [(ing, term, HYPOTHETICAL)])
    return [m for m in with_ if m.texto not in base or m.texto.startswith("Tríplice")]


def _clean(m: MedSuggestion) -> Frase:
    refs = [r for r in m.refs if r != HYPOTHETICAL]
    texto = m.texto.replace(PLANNED_SUFFIX, "").replace(
        "que o profissional disse que vai prescrever", "perguntado agora"
    )
    return Frase(texto=texto, refs=refs, fonte=m.fonte)


def intent_posso(ctx: Ctx) -> Resposta | None:
    pattern = r"\b(posso|pode|podemos|podia|poderia|da pra|seguro|liberado|contraindicado|contraindicacao|prescrever|usar|iniciar|associar)\b"  # noqa: E501
    hits = _drugs_in(ctx.n)
    if not hits or not re.search(pattern, ctx.n) or "interac" in ctx.n:
        return None
    c = ctx.chart
    frases: list[Frase] = []
    for ing, term in hits[:3]:
        name = _name(ing, term)
        alerts = [m for m in _evaluate(ctx, ing, term) if m.tipo != "registrar"]
        relevant = alerts
        crit = [m for m in relevant if m.severidade == "critico"]
        attn = [m for m in relevant if m.severidade == "atencao"]
        refs = sorted({r for m in (crit or attn) for r in m.refs if r != HYPOTHETICAL}, key=lambda r: r.split("/")[0])
        if crit:
            head = Frase(
                texto=f"Não. {name} tem alerta crítico para este paciente; veja os motivos abaixo.",
                refs=refs,
                fonte="; ".join(dict.fromkeys(m.fonte for m in crit if m.fonte)),
            )
        elif attn:
            head = Frase(
                texto=f"Com cautela. {name} tem alerta de atenção para este paciente; veja os motivos abaixo.",
                refs=refs,
                fonte="; ".join(dict.fromkeys(m.fonte for m in attn if m.fonte)),
            )
        else:
            in_use = [m.ref for m in c.active_meds]
            ctx_refs = (
                in_use + [r for a in ctx.ins.alergias for r in a.refs] + ([c.egfr[1].source_ref] if c.egfr else [])
            )
            head = Frase(
                texto=f"Sem alerta nas regras do HousIA para {name} (dose por TFG e idade, interações com os medicamentos em uso, "  # noqa: E501
                "alergias e alto alerta). Isso não substitui o julgamento clínico.",
                refs=ctx_refs,
                fonte="Regras do HousIA: dose_alerts.csv, interactions.csv, allergy_cross.csv, high_alert_meds.csv",
            )
        frases.append(head)
        if crit or attn:
            egfr = _egfr_frase(c)
            if egfr and c.egfr and c.egfr[0] < 60:
                frases.append(egfr)
        frases += [_clean(m) for m in relevant]
    return Resposta(intencao="posso_usar", frases=frases)


def intent_interacao(ctx: Ctx) -> Resposta | None:
    if "interac" not in ctx.n:
        return None
    hits = _drugs_in(ctx.n)
    if not hits:
        return None
    c = ctx.chart
    frases: list[Frase] = []
    if len(hits) >= 2:
        (ia, ta), (ib, tb) = hits[0], hits[1]
        ca, cb = _drug_class(ia), _drug_class(ib)
        for row in interactions():
            if (
                matches(row["a"], ia if not ia.startswith("@") else "", ca)
                and matches(row["b"], ib if not ib.startswith("@") else "", cb)
            ) or (
                matches(row["a"], ib if not ib.startswith("@") else "", cb)
                and matches(row["b"], ia if not ia.startswith("@") else "", ca)
            ):
                refs = [r for r in (_in_chart_ref(c, ia), _in_chart_ref(c, ib)) if r]
                frases.append(
                    Frase(
                        texto=f"Interação {_name(ia, ta)} + {_name(ib, tb)} ({row['severidade']}): {row['descricao']}. {row['recomendacao']}.",  # noqa: E501
                        refs=refs,
                        fonte=row.get("fonte", ""),
                    )
                )
                break
        if not frases:
            frases.append(
                Frase(
                    texto=f"Nenhuma interação cadastrada entre {_name(ia, ta)} e {_name(ib, tb)} na base de regras do HousIA.",  # noqa: E501
                    refs=[r for r in (_in_chart_ref(c, ia), _in_chart_ref(c, ib)) if r],
                    fonte="interactions.csv (base de demonstração do HousIA)",
                )
            )
    else:
        ing, term = hits[0]
        found = [m for m in _evaluate(ctx, ing, term) if m.tipo == "interacao"]
        frases = [_clean(m) for m in found]
        if not frases:
            frases.append(
                Frase(
                    texto=f"Nenhuma interação de {_name(ing, term)} com os medicamentos em uso ou relatados, nas regras do HousIA.",  # noqa: E501
                    refs=[m.ref for m in c.active_meds],
                    fonte="interactions.csv (base de demonstração do HousIA)",
                )
            )
    return Resposta(intencao="interacao", frases=frases)


def intent_dose(ctx: Ctx) -> Resposta | None:
    if not re.search(r"\b(dose|doses|posologia|ajuste|ajustar|ajustes)\b", ctx.n):
        return None
    hits = _drugs_in(ctx.n)
    if not hits:
        return None
    from app.consult.knowledge import dose_alerts

    c = ctx.chart
    frases: list[Frase] = []
    aviso = ""
    egfr = c.egfr
    age = c.r.age or 0
    for ing, term in hits[:3]:
        name = _name(ing, term)
        cls = _drug_class(ing)
        ref = _in_chart_ref(c, ing)
        rules = [r for r in dose_alerts() if matches(r.item, ing if not ing.startswith("@") else "", cls)]
        for r in sorted(rules, key=lambda x: x.limit):
            if r.criterion == "tfg":
                if egfr:
                    applies = egfr[0] < r.limit
                    state = (
                        f"a TFG atual (~{round(egfr[0])}) está abaixo de {round(r.limit)}: a regra se aplica"
                        if applies
                        else f"a TFG atual (~{round(egfr[0])}) está acima de {round(r.limit)}: a regra não se aplica agora"  # noqa: E501
                    )
                    refs = [egfr[1].source_ref] + ([ref] if ref else [])
                else:
                    state, refs = "sem creatinina para estimar a TFG", ([ref] if ref else [])
                frases.append(
                    Frase(
                        texto=f"{name}: {r.recommendation} Para este paciente, {state}.",
                        refs=refs,
                        fonte=r.source,
                    )
                )
            elif r.criterion == "idade":
                state = (
                    f"o paciente tem {age} anos: a regra se aplica"
                    if age >= r.limit
                    else f"o paciente tem {age} anos: a regra não se aplica"
                )
                frases.append(
                    Frase(
                        texto=f"{name}: {r.recommendation} Aqui, {state}.",
                        refs=[ref] if ref else [],
                        fonte=r.source,
                    )
                )
        if re.search(r"\bpeso\b", ctx.n):
            w = c.weight()
            if w:
                frases.append(
                    Frase(
                        texto=f"Peso registrado para o cálculo por kg: {fmt_num(w[0], 1)} kg.",
                        refs=[w[1]],
                        fonte="Prontuário",
                    )
                )
            else:
                aviso += "Sem peso registrado no último ano. "
            aviso += f"O HousIA não tem tabela curada de dose por peso para {name}; confira na bula. "
        if not rules and not re.search(r"\bpeso\b", ctx.n):
            aviso += f"Não há regra de ajuste por TFG ou idade para {name} na base do HousIA; confira na bula. "
    return Resposta(intencao="dose", frases=frases, aviso=aviso.strip())


# ---------- orquestração ----------
RULES = (intent_interacao, intent_dose, intent_posso, intent_mudou, intent_alergias, intent_alertas, intent_valor)


def answer_by_rules(ctx: Ctx) -> Resposta | None:
    for intent in RULES:
        r = intent(ctx)
        if r is not None:
            return r
    return None


def mock_ask(_variables: dict) -> dict:
    """Em mock não há IA: só as regras respondem (sem chave de API)."""
    return {"frases": []}


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    return {m.group(0).replace(",", ".") for m in _NUM.finditer(text)}


def validate(frases: list[Frase], valid: set[str], where: str = "copiloto") -> tuple[list[Frase], list[dict]]:
    """Frase sem fonte sai: a de regra precisa de refs válidas ou da fonte da regra; a da IA, só de refs válidas."""
    kept: list[Frase] = []
    dropped: list[dict] = []
    for f in frases:
        bad = [r for r in f.refs if r not in valid]
        ok = not bad and (f.refs or (f.origem == "regra" and f.fonte.strip()))
        if f.origem == "ia":
            ok = bool(f.refs) and not bad
        if ok and f.texto.strip():
            kept.append(f)
        else:
            dropped.append({"onde": where, "texto": f.texto, "refs_invalidas": bad or ["(sem ref)"]})
    return kept, dropped


def run_ai(ctx: Ctx, pseudo, valid: set[str], tools: dict) -> tuple[Resposta, object]:
    """Pergunta que as regras não resolvem: LLM com o contexto pseudonimizado e os resultados das regras."""
    variables = {
        "tipo": ctx.engine.kind,
        "especialidade": ctx.engine.specialty,
        "pergunta": pseudo.text(ctx.q),
        "prontuario": pseudo.apply(ai.chart_payload(ctx.engine)),
        "regras": pseudo.apply(tools),
        "transcricao": ai.transcript_text(ctx.engine.segments, pseudo),
    }
    result = generate_structured("consult_ask", variables, AskOutput, mock_ask, "low")
    allowed = _numbers(
        repr(variables["prontuario"])
        + repr(variables["regras"])
        + str(variables["transcricao"])
        + variables["pergunta"]
    )
    frases: list[Frase] = []
    dropped: list[dict] = []
    for f in result.output.frases:
        new = _numbers(f.texto) - allowed
        if new:  # o LLM não calcula: número fora dos dados recebidos derruba a frase
            dropped.append(
                {"onde": "copiloto", "texto": f.texto, "refs_invalidas": [], "motivo": "número fora dos dados"}
            )
            continue
        frases.append(Frase(texto=f.texto, refs=f.refs, origem="ia"))
    kept, bad = validate(frases, valid)
    resp = Resposta(
        intencao="ia", modo="ia" if result.mode == "real" else "regras", frases=kept, descartados=dropped + bad
    )
    if not kept:
        resp.intencao = "sem_resposta"
        resp.aviso = (
            "Não consegui responder com fonte no prontuário ou na conversa."
            if result.mode == "real"
            else "Sem a IA ligada, só respondo por regra: exames e valores, alergias, o que mudou, 'posso usar X?', "
            "dose por TFG, interação entre medicamentos e alertas."
        )
    return resp, result


def rules_tools(ctx: Ctx) -> dict:
    """Resultados das regras entregues ao LLM como ferramentas (ele cita, não calcula)."""
    c = ctx.chart
    return {
        "tfg": {"valor": round(c.egfr[0], 1), "ref": c.egfr[1].source_ref} if c.egfr else None,
        "alertas_de_medicamentos": [
            {"texto": m.texto, "severidade": m.severidade, "refs": m.refs, "fonte": m.fonte}
            for m in ctx.ins.sugestoes_medicamentos[:12]
        ],
        "hipoteses": [{"texto": f"{h.hipotese} ({h.cid})", "refs": h.refs} for h in ctx.ins.hipoteses[:6]],
        "protocolos_pendentes": [
            {"texto": f"{p.protocolo}: {p.item}", "refs": p.refs} for p in ctx.ins.protocolos if p.status != "em_dia"
        ][:8],
    }
