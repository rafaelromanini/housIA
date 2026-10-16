"""Smart Apps em tempo real (camada determinística).

Roda a cada fala nova, em milissegundos: queixas em tags, emoções, perguntas sugeridas (as já abordadas saem da
lista), hipóteses com evidências, alertas de segurança (dose × função renal, interações com o que o paciente
relatou, alergias), resumo e anamnese estruturada. A camada de IA (app/consult/ai.py) refina e complementa.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.consult import knowledge
from app.consult.chart import ChartView, fmt_date
from app.consult.schemas import (
    AllergyInfo,
    Anamnese,
    CommMetrics,
    Emotion,
    EmotionPoint,
    Evidence,
    Highlight,
    HistoryItem,
    Hypothesis,
    Insights,
    MedSuggestion,
    Question,
    Segment,
    Summary,
    SurgeryItem,
    Tag,
    fala_ref,
)
from app.consult.surgical import surgical_report
from app.consult.text import contains, keyword_status, mentioned_drugs, norm
from app.reference import drugs, interactions, matches
from app.timeline.context import PatientContext
from app.timeline.events import ClinicalEvent
from app.timeline.record import PatientRecord

PATIENT_SIDE = ("paciente", "acompanhante")
OPEN_MARKERS = ("como ", "o que", "me conta", "me fala", "qual ", "quais ", "por que", "de que forma", "o senhor sente")
DOCTOR_MARKERS = ("o senhor", "a senhora", "vamos ", "me conta", "voce tem", "voce sente", "doutor", "sou a medica")
FAMILY_RE = re.compile(r"(minha|meu) (mae|pai|irma|irmao|avo|avô|tia|tio|filha|filho)")
FAMILY_DISEASE = (
    "diabet",
    "insulina",
    "derrame",
    "avc",
    "infarto",
    "cancer",
    "pressao alta",
    "hipertens",
    "rim",
    "dialise",
)
# cirurgia a fazer (não a que já aconteceu na internação)
SURGERY_PLAN_WORDS = (
    "operar",
    "vou ser operad",
    "cirurgia marcada",
    "cirurgia programada",
    "marcar a cirurgia",
    "fazer a cirurgia",
    "fazer cirurgia",
)
SURGERY_PAST_WORDS = ("operei", "fui operad", "ja fiz cirurgia", "fiz uma cirurgia")
CONTEXT_WORDS = ("moro ", "sozinh", "fumo", "cigarro", "bebo", "bebida", "alcool", "caminhada", "trabalho", "aposentad")
PRIORITY = {"alta": 0, "media": 1, "baixa": 2}
SEVERITY = {"critico": 0, "atencao": 1, "info": 2}


def infer_speaker(text: str) -> str:
    """Quem falou, quando a captura não informou (heurística; a IA corrige depois)."""
    t = norm(text)
    if text.strip().endswith("?") and any(m in t for m in DOCTOR_MARKERS):
        return "medico"
    if any(m in t for m in ("doutora", "doutor")):
        return "paciente"
    return "medico" if text.strip().endswith("?") else "paciente"


class Engine:
    def __init__(
        self,
        record: PatientRecord,
        events: list[ClinicalEvent],
        ctx: PatientContext,
        segments: list[Segment],
        specialty: str,
        kind: str,
        now: datetime,
        extra: dict | None = None,
    ):
        self.chart = ChartView(record, events, ctx, now)
        self.r = record
        self.segments = segments
        self.specialty = specialty
        self.kind = kind
        self.extra = extra or {}
        self.norm = {s.n: norm(s.texto) for s in segments}
        self.patient_segments = [s for s in segments if s.falante in PATIENT_SIDE]

    # ---------- utilidades ----------
    def said(self, words: tuple[str, ...], speakers: tuple[str, ...] | None = None) -> list[Segment]:
        return [
            s for s in self.segments if (speakers is None or s.falante in speakers) and contains(self.norm[s.n], words)
        ]

    @staticmethod
    def quote(s: Segment, size: int = 110) -> str:
        text = s.texto.strip()
        return f"“{text[:size]}{'…' if len(text) > size else ''}”"

    # ---------- Queixa principal (tags) ----------
    def complaints(self) -> tuple[list[Tag], dict[str, list[str]]]:
        present: dict[str, list[str]] = {}
        denied: dict[str, list[str]] = {}
        order: list[str] = []
        labels = {d.key: d.label for d in knowledge.symptoms()}
        for s in self.patient_segments:
            for d in knowledge.symptoms():
                status = keyword_status(s.texto, d.words)
                if status is None:
                    continue
                bucket = present if status == "presente" else denied
                bucket.setdefault(d.key, []).append(fala_ref(s.n))
                if d.key not in order:
                    order.append(d.key)
        tags = []
        for key in order:
            if key in present:
                tags.append(Tag(rotulo=labels[key], status="presente", refs=present[key]))
            else:
                tags.append(Tag(rotulo=labels[key], status="negado", refs=denied[key]))
        return tags, present

    # ---------- Análise multidimensional ----------
    def emotions(self) -> tuple[list[Emotion], list[EmotionPoint], CommMetrics]:
        hits: dict[tuple[str, str], list[Segment]] = {}
        timeline: list[EmotionPoint] = []
        defs = knowledge.emotions()
        for s in self.patient_segments:
            found = [d for d in defs if contains(self.norm[s.n], d.words)]
            for d in found:
                hits.setdefault((d.key, s.falante), []).append(s)
            timeline.append(EmotionPoint(n=s.n, falante=s.falante, emocoes=[d.key for d in found]))
        out = []
        by_key = {d.key: d for d in defs}
        for (key, who), segs in hits.items():
            d = by_key[key]
            intensity = "alta" if len(segs) >= 3 else ("media" if len(segs) == 2 else "baixa")
            out.append(
                Emotion(
                    emocao=key,
                    rotulo=d.label,
                    quem=who,
                    intensidade=intensity,
                    trecho=self.quote(segs[-1]),
                    abordagem=d.approach,
                    refs=[fala_ref(s.n) for s in segs],
                )
            )
        out.sort(key=lambda e: int(e.refs[-1].split("/")[1]), reverse=True)  # mais recente primeiro

        words = {"pro": 0, "pac": 0}
        open_q = closed_q = 0
        for s in self.segments:
            n_words = len(s.texto.split())
            if s.falante == "medico":
                words["pro"] += n_words
                for q in re.findall(r"[^.?!]*\?", s.texto):
                    if any(m in norm(q) for m in OPEN_MARKERS):
                        open_q += 1
                    else:
                        closed_q += 1
            elif s.falante in PATIENT_SIDE:
                words["pac"] += n_words
        total = max(1, words["pro"] + words["pac"])
        metrics = CommMetrics(
            falas=len(self.segments),
            palavras_paciente_pct=round(100 * words["pac"] / total),
            palavras_profissional_pct=round(100 * words["pro"] / total),
            perguntas_abertas=open_q,
            perguntas_fechadas=closed_q,
        )
        return out, timeline, metrics

    # ---------- medicamentos citados ----------
    def reported_meds(self) -> list[tuple[str, str, str]]:
        out = []
        for s in self.patient_segments:
            for ing, term in mentioned_drugs(s.texto):
                out.append((ing, term, fala_ref(s.n)))
        return out

    def _med_classes(self, reported: list[tuple[str, str, str]]) -> list[tuple[str, str, str, str]]:
        """(princípio, classe, nome, ref) do que está em uso + relatado."""
        catalog = drugs()
        out = [(m.ingredient, m.drug_class, m.name, m.ref) for m in self.chart.active_meds]
        for ing, term, ref in reported:
            if ing.startswith("@"):
                out.append(("", ing[1:], term, ref))
            elif ing in catalog and all(ing != o[0] for o in out):
                out.append((ing, catalog[ing].drug_class, catalog[ing].name, ref))
        return out

    # ---------- Perguntas sugeridas ----------
    def questions(self, present: dict[str, list[str]], emotions: list[Emotion], surgery_refs: list[str]):
        triggers: dict[str, list[dict]] = {}

        def add(trigger: str, refs: list[str], **fmt) -> None:
            triggers.setdefault(trigger, []).append({"refs": refs, "fmt": fmt})

        for key, refs in present.items():
            add(f"sintoma:{key}", refs)
        ambulatory = self.kind == "consulta"  # perguntas de acompanhamento domiciliar só fazem sentido na consulta
        for icd, _, c in self.chart.conditions if ambulatory else []:
            for q in knowledge.questions():
                if q.trigger.startswith("condicao:") and icd.startswith(q.trigger.split(":", 1)[1]):
                    add(q.trigger, [f"Condition/{c['id']}"])
        reported = self.reported_meds()
        for _ing, cls, name, ref in self._med_classes(reported):
            add(f"classe:{cls}", [ref], med=name)
        for a in self.chart.ctx.alergias:
            name, _, reaction = a.summary.partition(" — ")
            add("alergia:*", [a.source_ref], alergeno=name, reacao=reaction.split(" (")[0].lower() or "não descrita")
        if surgery_refs:
            add("contexto:cirurgia", surgery_refs)
        if ambulatory and len(self.chart.active_meds) >= 4:
            add("contexto:adesao", [m.ref for m in self.chart.active_meds][:4])
        if "queda" in present or (self.r.age or 0) >= 65:
            add("contexto:queda", present.get("queda", []))
        anxious = [e for e in emotions if e.emocao in ("ansiedade", "preocupacao", "tristeza")]
        if anxious:
            add("contexto:emocional", anxious[0].refs)
        add("especialidade:*", [])

        suggested: list[Question] = []
        covered: list[Question] = []
        seen: set[str] = set()
        for q in knowledge.questions():
            if q.specialty and q.specialty != self.specialty:
                continue
            for occ in triggers.get(q.trigger, []):
                try:
                    text = q.text.format(trat="a senhora" if self.r.sex == "F" else "o senhor", **occ["fmt"])
                except KeyError:
                    continue
                if text in seen:
                    continue
                seen.add(text)
                answered = self.said(q.asked_words) if q.asked_words else []
                item = Question(pergunta=text, motivo=q.reason, prioridade=q.priority, refs=occ["refs"])
                if answered:
                    covered.append(item.model_copy(update={"refs": [fala_ref(s.n) for s in answered[:2]]}))
                else:
                    suggested.append(item)
        conv_first = {q.pergunta: any(r.startswith("Fala/") for r in q.refs) for q in suggested}
        suggested.sort(key=lambda q: (PRIORITY[q.prioridade], not conv_first[q.pergunta]))
        return suggested[:8], covered

    # ---------- Hipóteses ----------
    def hypotheses(self, present: dict[str, list[str]], denied_tags: list[Tag]) -> list[Hypothesis]:
        reported = self.reported_meds()
        classes = self._med_classes(reported)
        labels = {d.key: d.label for d in knowledge.symptoms()}
        denied = {t.rotulo: t.refs for t in denied_tags if t.status == "negado"}
        out = []
        for d in knowledge.differentials():
            pro: list[Evidence] = []
            con: list[Evidence] = []
            for ev in d.evidence:
                kind, _, spec = ev.partition(":")
                if kind == "s":
                    if spec in present:
                        pro.append(Evidence(texto=f"Relata {labels[spec].lower()}", refs=present[spec][:3]))
                    elif labels.get(spec) in denied:
                        con.append(Evidence(texto=f"Nega {labels[spec].lower()}", refs=denied[labels[spec]]))
                elif kind in ("l", "v"):
                    key, op, val = spec.split(":")
                    e = self.chart.latest_vital(key, 48) if kind == "v" else self.chart.latest_lab(key, 400)
                    if key == "pas" and kind == "v":
                        pa = self.chart.latest_vital("pa", 48)
                        e, value = pa, (pa.value["sistolica"] if pa else None)
                    else:
                        value = float(e.value["valor"]) if e else None
                    if e is not None and value is not None:
                        hit = value < float(val) if op == "lt" else value > float(val)
                        if hit:
                            pro.append(Evidence(texto=f"{e.summary} em {fmt_date(e.timestamp)}", refs=[e.source_ref]))
                elif kind == "c":
                    for ing, cls, name, ref in classes:
                        if spec in (ing, cls):
                            origin = "relatado na consulta" if ref.startswith("Fala/") else "em uso"
                            pro.append(Evidence(texto=f"{name} ({origin})", refs=[ref]))
                            break
                elif kind == "d":
                    c = self.chart.has_condition(spec)
                    if c is not None:
                        pro.append(Evidence(texto=c.get("code", {}).get("text", spec), refs=[f"Condition/{c['id']}"]))
                elif kind == "n":
                    notes = [e for e in self.chart.notes_text(30) if spec in norm(e.text or "")]
                    segs = self.said((spec,))
                    if notes:
                        pro.append(Evidence(texto=f"Registro: “…{spec}…”", refs=[notes[-1].source_ref]))
                    elif segs:
                        pro.append(
                            Evidence(
                                texto=f"Citado na conversa: {self.quote(segs[-1], 70)}", refs=[fala_ref(segs[-1].n)]
                            )
                        )
            if len(pro) < d.minimum:
                continue
            ratio = len(pro) / len(d.evidence)
            if ratio >= 0.75 and len(pro) >= 4:
                confidence = "alta"
            elif ratio >= 0.5 and len(pro) >= 3:
                confidence = "media"
            else:
                confidence = "baixa"
            refs = list(dict.fromkeys(r for e in pro for r in e.refs))
            out.append(
                Hypothesis(
                    hipotese=d.hypothesis,
                    cid=d.icd,
                    confianca=confidence,
                    a_favor=pro,
                    contra=con,
                    proximos_passos=d.next_steps,
                    sinais_de_alarme=d.red_flags,
                    refs=refs,
                )
            )
        rank = {"alta": 0, "media": 1, "baixa": 2}
        out.sort(key=lambda h: (rank[h.confianca], -len(h.a_favor)))
        return out

    # ---------- Segurança de medicamentos ----------
    def med_safety(self, allergies: list[AllergyInfo]) -> list[MedSuggestion]:
        reported = self.reported_meds()
        classes = self._med_classes(reported)
        out: list[MedSuggestion] = []
        egfr = self.chart.egfr
        age = self.r.age or 0
        for ing, cls, name, ref in classes:
            fired: set[str] = set()
            for rule in sorted(knowledge.dose_alerts(), key=lambda r: r.limit):
                if not matches(rule.item, ing, cls) or rule.criterion in fired:
                    continue
                if rule.criterion == "tfg" and egfr and egfr[0] < rule.limit:
                    fired.add("tfg")
                    out.append(
                        MedSuggestion(
                            texto=f"{name}: {rule.recommendation} (TFG estimada ~{round(egfr[0])} mL/min/1,73 m²)",
                            tipo="suspender" if rule.severity == "critico" else "ajuste_dose",
                            severidade=rule.severity,
                            refs=[ref, egfr[1].source_ref],
                        )
                    )
                elif rule.criterion == "idade" and age >= rule.limit:
                    fired.add("idade")
                    out.append(
                        MedSuggestion(
                            texto=f"{name}: {rule.recommendation} ({age} anos)",
                            tipo="alternativa",
                            severidade=rule.severity,
                            refs=[ref],
                        )
                    )
        seen_pairs: set[frozenset] = set()
        for i, a in enumerate(classes):
            for b in classes[i + 1 :]:
                pair = frozenset((a[2], b[2]))
                if pair in seen_pairs:
                    continue
                for row in interactions():
                    hit = (matches(row["a"], a[0], a[1]) and matches(row["b"], b[0], b[1])) or (
                        matches(row["a"], b[0], b[1]) and matches(row["b"], a[0], a[1])
                    )
                    if hit:
                        seen_pairs.add(pair)
                        out.append(
                            MedSuggestion(
                                texto=f"Interação {a[2]} + {b[2]}: {row['descricao']}. {row['recomendacao']}.",
                                tipo="interacao",
                                severidade=row["severidade"],
                                refs=[a[3], b[3]],
                            )
                        )
                        break
        raas = [c for c in classes if c[1] in ("ieca", "bra")]
        diuretic = [c for c in classes if c[1].startswith("diuretico")]
        nsaid = [c for c in classes if c[1] == "aine"]
        if raas and diuretic and nsaid:
            out.append(
                MedSuggestion(
                    texto=f"Tríplice ameaça renal: {raas[0][2]} + {diuretic[0][2]} + {nsaid[0][2]} — risco alto "
                    "de lesão renal aguda. Suspender o anti-inflamatório e repetir creatinina/potássio.",
                    tipo="suspender",
                    severidade="critico",
                    refs=[raas[0][3], diuretic[0][3], nsaid[0][3]],
                )
            )
        for a in allergies:
            for conflict in a.conflitos:
                out.append(
                    MedSuggestion(
                        texto=f"Alergia a {a.alergeno}: {conflict}. Rever a prescrição.",
                        tipo="alergia",
                        severidade="critico",
                        refs=a.refs,
                    )
                )
        active = {m.ingredient for m in self.chart.active_meds}
        for ing, term, ref in reported:
            if ing not in active:
                name = drugs()[ing].name if ing in drugs() else term
                out.append(
                    MedSuggestion(
                        texto=f"{name} relatado na consulta e ausente da prescrição: registrar dose/frequência "
                        "e avaliar.",
                        tipo="registrar",
                        severidade="atencao",
                        refs=[ref],
                    )
                )
        adherence = self.said(
            ("esqueco", "esqueci", "parei de tomar", "pulo o remedio", "tomo quando lembro"), PATIENT_SIDE
        )
        if adherence:
            out.append(
                MedSuggestion(
                    texto="Sinal de dificuldade de adesão relatado: simplificar esquema e usar organizador de doses.",
                    tipo="adesao",
                    severidade="atencao",
                    refs=[fala_ref(s.n) for s in adherence],
                )
            )
        out.sort(key=lambda m: SEVERITY[m.severidade])
        return out

    # ---------- Alergias relatadas ----------
    def allergies(self) -> list[AllergyInfo]:
        out = self.chart.chart_allergies()
        known = {norm(a.alergeno) for a in out}
        reported = [(ing, term) for ing, term, _ in self.reported_meds()]
        for s in self.patient_segments:
            for clause in self.norm[s.n].split("."):
                if "alergi" not in clause:
                    continue
                for d in knowledge.allergy_defs():
                    hit = contains(clause, d.words)
                    if not hit:
                        continue
                    if norm(d.label) in known or any(hit in k or k.split(" ")[0] in hit for k in known):
                        continue
                    known.add(norm(d.label))
                    out.append(
                        AllergyInfo(
                            alergeno=d.label,
                            reacao=f"Relatado: {self.quote(s, 90)}",
                            fonte="relatado",
                            reatividade_cruzada=d.cross,
                            evitar=d.avoid,
                            alternativas=d.alternatives,
                            dicas=d.tips,
                            conflitos=self.chart.allergy_conflicts(d.allergen, reported),
                            refs=[fala_ref(s.n)],
                        )
                    )
        return out

    # ---------- Histórico relatado + cirurgia citada ----------
    def reported_history(self) -> list[HistoryItem]:
        out = []
        for s in self.patient_segments:
            t = self.norm[s.n]
            m = FAMILY_RE.search(t)
            if m and any(w in t for w in FAMILY_DISEASE):
                out.append(
                    HistoryItem(
                        texto=f"Antecedente familiar relatado: {self.quote(s, 90)}",
                        tipo="familiar",
                        refs=[fala_ref(s.n)],
                    )
                )
        return out

    def surgery(self) -> tuple[list[SurgeryItem], list[str]]:
        items = self.chart.surgical_history()
        for s in self.said(SURGERY_PLAN_WORDS):
            items.append(
                SurgeryItem(texto=f"Citado na consulta: {self.quote(s, 110)}", tipo="planejada", refs=[fala_ref(s.n)])
            )
        for s in self.said(SURGERY_PAST_WORDS, PATIENT_SIDE):
            items.append(
                SurgeryItem(
                    texto=f"Cirurgia prévia relatada: {self.quote(s, 110)}", tipo="historico", refs=[fala_ref(s.n)]
                )
            )
        planned = [i for i in items if i.tipo == "planejada"]
        if planned:
            items += self.chart.perioperative_checks()
        return items, [r for i in planned for r in i.refs]

    # ---------- Resumo e anamnese ----------
    def summary(self, tags: list[Tag], hyps: list[Hypothesis], meds: list[MedSuggestion], exams) -> Summary:
        r = self.r
        dx = [t for _, t, _ in self.chart.conditions][:5]
        egfr = self.chart.egfr
        parts = [f"[PACIENTE], {r.age} anos" + (f", com {', '.join(dx)}" if dx else "")]
        if egfr and egfr[0] < 60:
            parts[0] += f" (TFG estimada ~{round(egfr[0])})"
        present = [t.rotulo.lower() for t in tags if t.status == "presente"]
        negated = [t.rotulo.lower() for t in tags if t.status == "negado"]
        if present:
            parts.append("Queixas: " + ", ".join(present))
        if negated:
            parts.append("Nega: " + ", ".join(negated))
        abnormal = [f"{e.exame.lower()} {e.valor}" for e in exams if e.status != "normal"][:4]
        if abnormal:
            parts.append("Exames alterados: " + "; ".join(abnormal))
        if hyps:
            parts.append("Hipóteses principais: " + "; ".join(h.hipotese.lower() for h in hyps[:3]))
        text = ". ".join(parts) + "."
        highlights: list[Highlight] = [
            Highlight(texto=m.texto, severidade=m.severidade, refs=m.refs) for m in meds if m.severidade == "critico"
        ]
        for e in exams:
            if e.status != "normal" and e.chave in ("glicemia", "potassio", "sodio"):
                highlights.append(
                    Highlight(texto=f"{e.exame}: {e.valor} ({e.data})", severidade="atencao", refs=e.refs)
                )
        for t in tags:
            if t.status == "presente" and t.rotulo in ("Queda ou quase queda", "Dor no peito", "Falta de ar"):
                highlights.append(Highlight(texto=f"Relata: {t.rotulo.lower()}", severidade="atencao", refs=t.refs))
        return Summary(texto=text, destaques=highlights[:8])

    def anamnese(self, tags: list[Tag], allergies: list[AllergyInfo], reported) -> Anamnese:
        complaint_segs = [
            s for s in self.patient_segments if any(f"Fala/{s.n}" in t.refs for t in tags if t.status == "presente")
        ]
        qp = self.quote(complaint_segs[0], 140) if complaint_segs else "Sem queixa registrada na conversa até agora."
        hda = " ".join(self.quote(s, 160) for s in complaint_segs[:6]) or "—"
        meds = [m.text for m in self.chart.active_meds] + [
            f"{term} (relatado, por conta própria)"
            for ing, term, _ in reported
            if ing not in {m.ingredient for m in self.chart.active_meds}
        ]
        context = [self.quote(s, 120) for s in self.patient_segments if contains(self.norm[s.n], CONTEXT_WORDS)]
        refs = [fala_ref(s.n) for s in complaint_segs[:6]]
        return Anamnese(
            queixa_principal=qp,
            hda=hda,
            antecedentes=[h.texto for h in self.chart.history() if h.tipo in ("diagnostico", "cirurgia")],
            medicacoes_em_uso=meds,
            alergias=[f"{a.alergeno} — {a.reacao}" for a in allergies]
            or (["Nega alergias conhecidas"] if self.chart.ctx.nega_alergias else ["Sem registro"]),
            habitos_e_contexto=context,
            revisao_de_sistemas=[f"Nega {t.rotulo.lower()}" for t in tags if t.status == "negado"],
            refs=refs,
        )

    # ---------- tudo junto ----------
    def run(self) -> Insights:
        if self.kind == "cirurgia":
            report = surgical_report(self.segments, self.chart, self.extra.get("opme_autorizado", []))
            return Insights(
                tipo="cirurgia",
                especialidade=self.specialty,
                falas_analisadas=len(self.segments),
                modo="regras",
                relatorio_cirurgico=report,
                alergias=self.chart.chart_allergies(),
                alertas=[p for p in report.pendencias if p.severidade == "critico"],
            )
        tags, present = self.complaints()
        emotions, timeline, metrics = self.emotions()
        surgery_items, surgery_refs = self.surgery()
        questions, covered = self.questions(present, emotions, surgery_refs)
        hyps = self.hypotheses(present, tags)
        allergies = self.allergies()
        meds_safety = self.med_safety(allergies)
        reported = self.reported_meds()
        exams = self.chart.exams()
        history = self.chart.history() + self.reported_history()
        egfr = self.chart.egfr
        alerts = [
            Highlight(texto=m.texto, severidade=m.severidade, refs=m.refs)
            for m in meds_safety
            if m.severidade == "critico"
        ]
        return Insights(
            tipo=self.kind,
            especialidade=self.specialty,
            falas_analisadas=len(self.segments),
            modo="regras",
            queixas=tags,
            emocoes=emotions,
            linha_emocional=timeline,
            comunicacao=metrics,
            perguntas=questions,
            perguntas_abordadas=covered,
            hipoteses=hyps,
            historico=history,
            alergias=allergies,
            exames=exams,
            medicamentos=self.chart.meds(reported),
            sugestoes_medicamentos=meds_safety,
            cirurgia=surgery_items,
            protocolos=self.chart.protocols(),
            resumo=self.summary(tags, hyps, meds_safety, exams),
            anamnese=self.anamnese(tags, allergies, reported),
            alertas=alerts,
            tfg=egfr[0] if egfr else None,
        )
