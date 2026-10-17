"""Sumarizador com fonte por frase (camada determinística).

Cada frase do resumo carrega `refs` (falas e/ou recursos FHIR) e a marca `nova` (não estava no resumo anterior).
As frases saem de: comorbidades e TFG; queixas com atributos; como o paciente toma os remédios (fala com
medicamento, horário ou jejum); emoção principal; mudança recente de medicamento; plano dito pelo médico.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.consult.chart import fmt_date
from app.consult.schemas import Emotion, Insights, SummarySentence, fala_ref
from app.consult.text import clauses, contains, mentioned_drugs, norm
from app.timeline.record import MedInfo

if TYPE_CHECKING:
    from app.consult.engine import Engine

CHANGE_WINDOW_DAYS = 90  # prescrição iniciada há menos disso é "mudança recente"
CHANGE_FALA_DAYS = 180  # o paciente diz que começou/trocou: vale prescrição de até 6 meses
MAX_PLAN = 3
FASTING_CUES = (
    "em jejum", "jejum", "vim direto", "sem comer", "sem tomar cafe", "nao tomei cafe", "nao comi",
    "so como mais tarde", "so como depois", "nao tenho fome", "pulei o cafe", "sem cafe", "nao tomo cafe",
    "nao como de manha", "antes de comer", "antes do cafe", "antes de tomar cafe",
)  # fmt: skip
TIME_CUES = (
    "de manha", "cedo", "a noite", "de noite", "antes do almoco", "depois do almoco", "antes de dormir", "duas vezes",
    "tres vezes", "uma vez", "de 8 em 8", "de 12 em 12", "todo dia", "toda manha", "quando lembro", "as vezes",
    "depois de comer", "antes de comer", "com o almoco", "com o cafe",
)  # fmt: skip
# comeu logo depois: "vim direto do almoço" não é jejum
FED = re.compile(
    r"(?<!\w)(depois d[oae]s? (almoco|jantar|cafe|comer|refeicao)|apos (comer|o almoco|o cafe)|com o (cafe|almoco))"
)
GENERIC_DIABETES = ("remedio do acucar", "remedio da diabetes", "remedio de diabetes", "remedio da glicose", "insulina")
HYPO_CLASSES = ("sulfonilureia", "insulina_basal", "insulina_rapida")
ANTIDIABETIC = (*HYPO_CLASSES, "biguanida")
CHANGE_CUES = (
    "comecei", "iniciei", "comecou", "comecaram", "trocaram", "trocou", "troquei", "mudaram", "mudou", "passei a tomar",
    "passou a tomar", "aumentaram", "diminuiram", "aumentou a dose", "remedio novo", "medicacao nova", "novo remedio",
    "nova medicacao", "remedio que comecei",
)  # fmt: skip
CLASS_GROUPS = {
    "diuretico": ("diuretico_tiazidico", "diuretico_alca", "diuretico_poupador_potassio"),
    "insulina": ("insulina_basal", "insulina_rapida"),
    "anticoagulante": ("anticoagulante_oral", "heparina"),
    "antibiotico": ("penicilina", "cefalosporina", "macrolideo", "fluoroquinolona", "nitroimidazol"),
    "remedio da pressao": ("ieca", "bra", "betabloqueador", "bloqueador_canal_calcio", "diuretico_tiazidico"),
    "remedio do acucar": ANTIDIABETIC,
    "estatina": ("estatina",),
}
PLAN_CUE = re.compile(
    r"(?<!\w)(vou|vamos|vai|iremos|precisamos|precisa|preciso|temos que|tem que"
    r"|a gente (?:vai|precisa|tem que)|volte|voltar)(?!\w)"
)
PLAN_ACTION = re.compile(
    r"(?<!\w)(ajustar|pedir|solicitar|passar|prescrever|trocar|suspender|reduzir|aumentar|encaminhar|marcar|agendar|controlar"
    r"|deixar|olhar|repetir|iniciar|retomar|orientar|avaliar|investigar|acompanhar|monitorar|medir|operar|checar|revisar"
    r"|parar|manter|continuar|retornar|voltar|medicar|explicar|conversar)(?!\w)"
)
POSITIVE_EMOTIONS = ("alivio", "confianca")


def _clip(text: str, size: int = 120) -> str:
    text = text.strip()
    return f"“{text[:size]}{'…' if len(text) > size else ''}”"


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " e " + names[-1]


def _refs(*groups: list[str]) -> list[str]:
    out: list[str] = []
    for g in groups:
        for r in g:
            if r not in out:
                out.append(r)
    return out


def _by_ref_order(refs: list[str]) -> list[str]:
    return sorted(refs, key=lambda r: (not r.startswith("Fala/"), int(r.split("/")[1]) if r.startswith("Fala/") else 0))


def comorbidities(eng: Engine) -> SummarySentence:
    dx = [(t, c) for _, t, c in eng.chart.conditions][:5]
    text = f"[PACIENTE], {eng.r.age} anos" + (f", com {', '.join(t.lower() for t, _ in dx)}" if dx else "")
    refs = [f"Condition/{c['id']}" for _, c in dx]
    egfr = eng.chart.egfr
    if egfr and egfr[0] < 60:
        text += f" (TFG estimada ~{round(egfr[0])} mL/min)"
        refs.append(egfr[1].source_ref)
    return SummarySentence(chave="comorbidades", texto=text, refs=refs or [f"Patient/{eng.r.id}"])


def complaints(eng: Engine, tags) -> list[SummarySentence]:
    out = []
    present = [t for t in tags if t.status == "presente"]
    # "Tontura ao levantar" é a mesma queixa de "Tontura": uma frase só, com as falas das duas
    general = [t for t in present if not any(o is not t and t.rotulo.startswith(f"{o.rotulo} ") for o in present)]
    for t in general[:4]:
        same = [o.refs for o in present if o is not t and o.rotulo.startswith(f"{t.rotulo} ")]
        detail = f" ({t.detalhe})" if t.detalhe else ""
        out.append(
            SummarySentence(
                chave=f"queixa:{t.rotulo}",
                texto=f"Queixa de {t.rotulo.lower()}{detail}",
                refs=_by_ref_order(_refs(t.refs, *same)),
            )
        )
    if len(general) > 4:
        rest = general[4:]
        out.append(
            SummarySentence(
                chave="queixas_outras",
                texto="Também relata " + _join([t.rotulo.lower() for t in rest]),
                refs=_refs(*[t.refs for t in rest]),
            )
        )
    denied = [t for t in tags if t.status == "negado"]
    if denied:
        out.append(
            SummarySentence(
                chave="nega",
                texto="Nega " + _join([t.rotulo.lower() for t in denied]),
                refs=_refs(*[t.refs for t in denied]),
            )
        )
    return out


def _hypoglycemics(eng: Engine) -> list[MedInfo]:
    meds = eng.chart.active_meds
    risky = [m for m in meds if m.drug_class in HYPO_CLASSES]
    return risky or [m for m in meds if m.drug_class in ANTIDIABETIC]


def medication_use(eng: Engine) -> list[SummarySentence]:
    """Como o paciente toma os remédios: em jejum, sem comer, em certo horário, por conta própria."""
    catalog = {m.ingredient: m for m in eng.chart.active_meds}
    fasting: dict[str, tuple[str, list[str], list[str]]] = {}  # ingrediente → (nome, falas, o que disse)
    uses: dict[str, tuple[str, list[str], str, bool]] = {}
    for s in eng.patient_segments:
        t = eng.norm[s.n]
        cue = contains(t, FASTING_CUES) if not FED.search(t) else None
        mentioned = [
            (ing, term) for ing, term, st in mentioned_drugs(s.texto) if st == "em_uso" and not ing.startswith("@")
        ]
        if cue:
            if not mentioned and contains(t, GENERIC_DIABETES):
                mentioned = [(m.ingredient, m.name) for m in _hypoglycemics(eng)]
            for ing, term in mentioned:
                name = catalog[ing].name if ing in catalog else term
                _, refs, quotes = fasting.get(ing, (name, [], []))
                fasting[ing] = (name, refs + [fala_ref(s.n)], quotes + [_clip(s.texto, 90)])
            continue
        if mentioned and contains(t, TIME_CUES):
            for ing, term in mentioned:
                own = ing not in catalog
                name = catalog[ing].name if ing in catalog else term.capitalize()
                uses[ing] = (name, [fala_ref(s.n)], _clip(s.texto, 110), own)
    out = []
    glicemia = eng.chart.latest_vital("glicemia", 24)
    for ing, (name, refs, quotes) in fasting.items():
        tail = ""
        extra: list[str] = []
        if glicemia is not None and catalog.get(ing) and catalog[ing].drug_class in HYPO_CLASSES:
            tail = f"; glicemia capilar de hoje {glicemia.value['valor']:.0f} mg/dL"
            extra = [glicemia.source_ref]
        out.append(
            SummarySentence(
                chave=f"jejum:{ing}",
                texto=f"Toma {name.lower()} em jejum ou sem comer em seguida: {'; '.join(quotes)}{tail}",
                refs=_refs(refs, extra, [catalog[ing].ref] if ing in catalog else []),
            )
        )
    for ing, (name, refs, quote, own) in uses.items():
        if ing in fasting:
            continue
        head = (
            f"Toma {name.lower()} por conta própria (sem prescrição no prontuário)"
            if own
            else f"Como toma {name.lower()}"
        )
        out.append(
            SummarySentence(
                chave=f"uso:{ing}",
                texto=f"{head}: {quote}",
                refs=_refs(refs, [catalog[ing].ref] if ing in catalog else []),
            )
        )
    for ing, term, ref in eng.reported_meds():
        if ing.startswith("@") or ing in catalog or ing in uses or ing in fasting:
            continue
        out.append(
            SummarySentence(
                chave=f"uso:{ing}",
                texto=f"Toma {term.lower()} por conta própria (sem prescrição no prontuário)",
                refs=[ref],
            )
        )
    return out


def main_emotion(emotions: list[Emotion]) -> SummarySentence | None:
    if not emotions:
        return None
    rank = {"alta": 0, "media": 1, "baixa": 2}
    negative = [e for e in emotions if e.emocao not in POSITIVE_EMOTIONS] or emotions
    top = min(negative, key=lambda e: (rank[e.intensidade], e.quem != "paciente", -len(e.refs)))
    who = {"paciente": "paciente", "acompanhante": "acompanhante"}.get(top.quem, top.quem)
    return SummarySentence(
        chave=f"emocao:{top.emocao}:{top.quem}",
        texto=f"Emoção principal ({who}): {top.rotulo.lower()}, {top.trecho}",
        refs=top.refs,
    )


def _class_meds(eng: Engine, text: str) -> list[MedInfo]:
    """Prescrições ativas citadas pelo paciente por classe ("o diurético", "o remédio da pressão") ou por nome."""
    meds = eng.chart.active_meds
    found: list[MedInfo] = []
    for label, classes in CLASS_GROUPS.items():
        if contains(text, (label,)):
            found += [m for m in meds if m.drug_class in classes and m not in found]
    if "diuretico" in text:  # termo mais específico vence o "remédio da pressão"
        found = [m for m in found if m.drug_class.startswith("diuretico")]
    return found


def recent_changes(eng: Engine) -> list[SummarySentence]:
    now = eng.chart.now
    by_ref = {m.ref: m for m in eng.r.medications}
    changes: dict[str, tuple[MedInfo, list[str], list[str]]] = {}
    for m in eng.chart.active_meds:
        if m.start is not None and 0 <= (now - m.start).days <= CHANGE_WINDOW_DAYS:
            changes[m.ingredient] = (m, [], [])
    recent = [m for m in eng.chart.active_meds if m.start is not None and 0 <= (now - m.start).days <= CHANGE_FALA_DAYS]
    for s in eng.patient_segments:
        t = eng.norm[s.n]
        if not contains(t, CHANGE_CUES):
            continue
        candidates = _class_meds(eng, t)
        by_ing = {m.ingredient: m for m in eng.chart.active_meds}
        candidates += [by_ing[i] for i, _, st in mentioned_drugs(s.texto) if st == "em_uso" and i in by_ing]
        if (
            not candidates
            and contains(t, ("remedio novo", "medicacao nova", "novo remedio", "nova medicacao"))
            and recent
        ):
            candidates = [max(recent, key=lambda m: m.start)]
        for m in candidates:
            if m.start is None or (now - m.start).days > CHANGE_FALA_DAYS:
                continue
            _, refs, quotes = changes.get(m.ingredient, (m, [], []))
            changes[m.ingredient] = (m, refs + [fala_ref(s.n)], quotes + [_clip(s.texto, 90)])
    out = []
    for ing, (m, refs, quotes) in changes.items():
        prior = by_ref.get(m.prior_ref or "")
        days = (now - m.start).days if m.start else 0
        text = f"Mudança recente de medicamento: {m.name} iniciado em {fmt_date(m.start)} (há {days} dias)"
        if prior:
            text += f", no lugar de {prior.name}"
        if quotes:
            text += f"; relato: {quotes[-1]}"
        out.append(SummarySentence(chave=f"mudanca:{ing}", texto=text, refs=_refs([m.ref], refs)))
    return out[:3]


def doctor_plan(eng: Engine) -> SummarySentence | None:
    """O que o médico disse que vai fazer: as últimas orações com intenção e ação (sem pergunta)."""
    found: list[tuple[int, str]] = []
    for s in eng.segments:
        if s.falante not in ("medico",):
            continue
        for sentence in re.split(r"(?<=[.!])\s+", s.texto.strip()):
            n = norm(sentence)
            if "?" in sentence or not PLAN_CUE.search(n):
                continue
            if any(PLAN_ACTION.search(c) and PLAN_CUE.search(c) for c in clauses(sentence)) or (
                PLAN_CUE.search(n) and PLAN_ACTION.search(n)
            ):
                found.append((s.n, sentence.strip()))
    if not found:
        return None
    last = found[-MAX_PLAN:]
    return SummarySentence(
        chave="plano",
        texto="Plano dito pelo médico: " + "; ".join(_clip(t, 110) for _, t in last),
        refs=[fala_ref(n) for n, _ in last],
    )


def build(eng: Engine, tags, hyps, exams, emotions) -> list[SummarySentence]:
    out = [comorbidities(eng), *complaints(eng, tags), *medication_use(eng), *recent_changes(eng)]
    if emotion := main_emotion([e for e in emotions if e.quem in ("paciente", "acompanhante")]):
        out.append(emotion)
    abnormal = [e for e in exams if e.status != "normal" and e.origem_dado == "prontuário"][:4]
    if abnormal:
        out.append(
            SummarySentence(
                chave="exames",
                texto="Exames alterados: " + "; ".join(f"{e.exame.lower()} {e.valor}" for e in abnormal),
                refs=_refs(*[e.refs[:1] for e in abnormal]),
            )
        )
    if hyps:
        out.append(
            SummarySentence(
                chave="hipoteses",
                texto="Hipóteses principais: " + "; ".join(h.hipotese.lower() for h in hyps[:3]),
                refs=_refs(*[h.refs[:2] for h in hyps[:3]]),
            )
        )
    if plan := doctor_plan(eng):
        out.append(plan)
    return [f for f in out if f.refs]


def text_of(sentences: list[SummarySentence]) -> str:
    return " ".join(f"{f.texto.rstrip('.')}." for f in sentences)


def mark_new(ins: Insights, previous: dict | None) -> None:
    """`nova`: a frase não estava no resumo anterior (ou mudou). Reanálise com as mesmas falas mantém a marca."""
    if not ins.resumo or not previous:
        return
    old = {f.get("chave") or f.get("texto"): f for f in (previous.get("resumo") or {}).get("frases", [])}
    if not old:
        return
    same = previous.get("falas_analisadas") == ins.falas_analisadas
    for f in ins.resumo.frases:
        before = old.get(f.chave or f.texto)
        if before is None:
            f.nova = True
        elif before.get("texto") != f.texto:
            f.nova = True
        else:
            f.nova = bool(before.get("nova")) if same else False
