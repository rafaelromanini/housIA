"""Smart Apps em tempo real (camada determinística).

Roda a cada fala nova, em milissegundos: queixas em tags, emoções, perguntas sugeridas (as já abordadas saem da
lista), hipóteses com evidências, alertas de segurança (dose × função renal, interações com o que o paciente
relatou, alergias), resumo e anamnese estruturada. A camada de IA (app/consult/ai.py) refina e complementa.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.consult import knowledge, semiology
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
    QueixaDetalhada,
    Question,
    Segment,
    Summary,
    SurgeryItem,
    Tag,
    fala_ref,
)
from app.consult.surgical import surgical_report
from app.consult.text import (
    FOLLOW_UP,
    QUESTION_WHEN,
    contains,
    find,
    has_vocative,
    keyword_status,
    listener_questions,
    mentioned_drugs,
    norm,
    question_text,
)
from app.reference import drugs, interactions, matches
from app.timeline.context import PatientContext
from app.timeline.events import ClinicalEvent
from app.timeline.record import PatientRecord

PATIENT_SIDE = ("paciente", "acompanhante")
TEAM_SIDE = ("medico", "anestesista", "circulante", "instrumentador")
OPEN_MARKERS = ("como ", "o que", "me conta", "me fala", "qual ", "quais ", "por que", "de que forma", "o senhor sente")
# quem falou (modo automático): pistas lexicais, porque a fala do microfone chega sem "?" e sem vírgula
DOCTOR_MARKERS = tuple(
    (
        "me conta|me fala|me diga|me diz|me mostra|vamos |vou examinar|vou pedir|vou passar|vou trocar|"
        "vou prescrever|vou ajustar|vou suspender|vou receitar|vou solicitar|sou a medica|sou o medico|"
        "sou a enfermeira|sou o enfermeiro|sou a doutora|sou o doutor|respira fundo|pode deitar|pode sentar|"
        "abre a boca|seus exames|sua pressao|sua glicemia|seu exame|o exame mostrou|o resultado|recomendo|"
        "a gente vai|a gente precisa|precisa tomar|vai precisar|dona |que bom ver|entendo o medo|"
        "fique tranquila|fique tranquilo|isso explica|quero que a senhora|quero que o senhor|como ta|como esta|"
        "como vai|como anda|como tem passado|pra eu examinar|deixa eu examinar|deixa eu ver|vou medir|vou te examinar|"
        # orientação/conduta no imperativo: "evite ibuprofeno", "se tiver febre procure o pronto-socorro"
        "evite|evitar o|evitar a|nao tome|nao use|pode tomar|pode usar|tome |continue|mantenha|suspenda|procure|"
        "volte|volta em|retorne|retorno em|o retorno|fica tranquila|fica tranquilo|pode ficar tranquila|"
        "pode ficar tranquilo|nao precisa ficar|nao precisa se preocupar|nao precisa ter medo"
    ).split("|")
)
PATIENT_MARKERS = tuple(
    (
        "eu sinto|sinto |senti |to com|tou com|estou com|estou sentindo|to sentindo|tenho sentido|eu tenho|"
        "tenho |eu tive|tive |me da |me deu|me doi|eu fico|fico |fiquei|eu ando|ando |minha cabeca|meu peito|"
        "minha barriga|minha perna|meu joelho|minha pressao|eu tomo|tomo |tomei|eu uso|comprei|comecei|parei|"
        "esqueco|esqueci|posso |eu queria|eu acho|sei la|eu nao sei|eu moro|moro |eu trabalho|eu levanto|"
        "levanto|eu nunca|comigo|eu estou|estou |to |o que significa|e grave|sera que|vou precisar|tem cura|nao sei|"
        # paciente que se dirige ao profissional sem vocativo: "não entendi o que o senhor falou"
        "nao entendi|a senhora me |o senhor me |a senhora vai me|o senhor vai me|a senhora explicou|o senhor explicou|"
        "a senhora receitou|o senhor receitou|a senhora pediu|o senhor pediu|a senhora mandou|o senhor mandou"
    ).split("|")
)
# 1ª pessoa com sintoma: quem fala é o próprio paciente (decide entre paciente e acompanhante)
FIRST_PERSON_SYMPTOM = tuple(
    (
        "eu sinto|sinto |senti |to com|tou com|estou com|estou sentindo|to sentindo|tenho sentido|me da |"
        "me deu|me doi|minha cabeca|meu peito|minha barriga|eu ando"
    ).split("|")
)
THIRD_PERSON = re.compile(
    r"(?<!\w)(ela|ele)( nao| ja| quase| tambem| sempre| nunca)? (tem|teve|esta|ta|anda|andou|caiu|cai|sente|sentiu"
    r"|reclama|reclamou|fica|ficou|toma|tomou|e|era|estava|vem|veio|nao)(?!\w)|(?<!\w)(dele|dela)(?!\w)"
)
MEASUREMENT = re.compile(r"\d+ (por|x) \d+|(deu|esta|ta|foi) \d+")
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
    "rins",
    "dialise",
)
# cirurgia a fazer (não a que já aconteceu na internação); "não vai precisar operar" é negativa
SURGERY_PLAN_WORDS = (
    "operar ",  # infinitivo: "operaram minha mãe" não é cirurgia planejada da paciente
    "vou ser operad",
    "cirurgia marcada",
    "cirurgia ta marcada",
    "cirurgia esta marcada",
    "cirurgia programada",
    "marcar a cirurgia",
    "fazer a cirurgia",
    "fazer cirurgia",
)
SURGERY_PAST_WORDS = ("operei", "fui operad", "ja fiz cirurgia", "fiz uma cirurgia", "fiz cirurgia")
FORGET_WORDS = (
    "esqueco",
    "esqueci",
    "esquecendo de tomar",
    "esquecendo os remedios",
    "esquecendo o remedio",
    "pulo o remedio",
    "tomo quando lembro",
)  # sinais de dificuldade de adesão
STOP_WORDS = ("parei de tomar", "deixei de tomar")
CONTEXT_WORDS = ("moro ", "sozinh", "fumo", "cigarro", "bebo", "bebida", "alcool", "caminhada", "trabalho", "aposentad")
PRIORITY = {"alta": 0, "media": 1, "baixa": 2}
# ordem em que os campos vazios da queixa viram pergunta
MAX_QUESTIONS = 10  # perguntas sugeridas na lista (as de anamnese vêm depois, no máximo 8)
SEMIO_ORDER = ("inicio_duracao", "intensidade", "piora", "melhora", "frequencia", "localizacao", "associados")
SEVERITY = {"critico": 0, "atencao": 1, "info": 2}


def _cues(text: str, markers: tuple[str, ...]) -> int:
    padded = f" {text} "
    return sum(1 for m in markers if contains(padded, (m,)))


def infer_speaker(text: str) -> str:
    """Quem falou, quando a captura não informou (heurística; a IA corrige depois).

    Pontua pistas de profissional (pergunta em 2ª pessoa mesmo sem "?", "vamos", medidas ditadas) e de paciente
    (vocativo "doutora", 1ª pessoa com sintoma). Empate: pergunta é do profissional, afirmação é do paciente.
    """
    t = norm(text)
    question = text.strip().endswith("?")
    vocative = has_vocative(text)
    doctor = 3 * sum(listener_questions(text)) + 2 * _cues(t, DOCTOR_MARKERS)
    doctor += 2 * bool(MEASUREMENT.search(t)) + bool(re.search(r"(?<!\w)(a senhora|o senhor|voce)(?!\w)", t))
    doctor += 2 * bool(QUESTION_WHEN.search(t) and not re.search(r"(?<!\w)(eu|ela|ele)(?!\w)", t))
    patient = 3 * vocative + 2 * _cues(t, PATIENT_MARKERS)
    doctor += 2 * bool(FOLLOW_UP.match(t) and len(t.split()) <= 8 and not patient)
    if not question and any(keyword_status(text, d.words, skip_questions=True) for d in knowledge.symptoms()):
        patient += 1
    if patient > doctor and vocative and THIRD_PERSON.search(t) and not _cues(t, FIRST_PERSON_SYMPTOM):
        return "acompanhante"  # "doutora, ela quase caiu no banheiro"
    if doctor != patient:
        return "medico" if doctor > patient else "paciente"
    return "medico" if question else "paciente"


def billing_params(extra: dict) -> dict:
    """Convênio e número da guia da cirurgia (extra_json): entram nas regras anti-glosa."""
    faturamento = extra.get("faturamento") or {}
    return {
        "convenio": faturamento.get("convenio") or "generico",
        "numero_guia": faturamento.get("numero_guia") or (extra.get("opme_guia") or {}).get("numero"),
    }


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
        self.denied: dict[str, list[str]] = {}
        self.tags_denied: list[Tag] = []

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
        defs = [d for d in knowledge.symptoms() if self._fits(d.age_min, d.age_max, d.sex)]
        labels = {d.key: d.label for d in defs}
        for s in self.patient_segments:
            # na ordem em que o paciente falou ("febre e tosse e falta de ar"), não na da tabela
            spoken = sorted((hit[1], i) for i, d in enumerate(defs) for hit in [find(self.norm[s.n], d.words)] if hit)
            for _, i in spoken:
                d = defs[i]
                # pergunta em 2ª pessoa ("a senhora tem sentido dor no peito") não é queixa de quem fala
                status = keyword_status(s.texto, d.words, skip_questions=True, excludes=d.excludes)
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
        self.denied = {k: v for k, v in denied.items() if k not in present}
        return tags, present

    def _fits(self, age_min: int | None, age_max: int | None, sex: str) -> bool:
        """A idade e o sexo do paciente estão dentro do que o sintoma ou a pergunta pede?"""
        return knowledge.fits(age_min, age_max, sex, self.r.age, self.r.sex)

    # ---------- Análise multidimensional ----------
    def emotions(self) -> tuple[list[Emotion], list[EmotionPoint], CommMetrics]:
        hits: dict[tuple[str, str], list[Segment]] = {}
        timeline: list[EmotionPoint] = []
        defs = knowledge.emotions()
        for s in self.patient_segments:
            # negação vale para emoção ("não estou preocupada") e uma pista anula outra ("não entendi" ≠ confiança)
            found = [
                d
                for d in defs
                if keyword_status(s.texto, d.words, trailing_negation=False) == "presente"
                and not contains(self.norm[s.n], d.excludes)
            ]
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
            if s.falante in TEAM_SIDE:
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
    def _mentions(self, status: str) -> list[tuple[str, str, str]]:
        out = []
        for s in self.patient_segments:
            for ing, term, st in mentioned_drugs(s.texto):
                if st == status:
                    out.append((ing, term, fala_ref(s.n)))
        return out

    def reported_meds(self) -> list[tuple[str, str, str]]:
        """(princípio ou @classe, termo, ref) do que o paciente diz que USA. Negado ("nunca tomei") fica de fora."""
        return self._mentions("em_uso")

    def suspended_meds(self) -> list[tuple[str, str, str]]:
        """O que o paciente diz que parou ("não tomo mais", "parei"): histórico, não entra nos alertas de uso."""
        in_use = {ing for ing, _, _ in self.reported_meds()}
        return [m for m in self._mentions("suspenso_relatado") if m[0] not in in_use]

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
    def team_asked(self, words: tuple[str, ...]) -> tuple[Segment, Segment | None] | None:
        """A primeira PERGUNTA do PROFISSIONAL com alguma das palavras (inclusive parafraseada) e a resposta do paciente
        na fala seguinte. Fala do paciente sozinha não conta ("sono" dito pelo paciente não responde a pergunta que
        ninguém fez), nem a do profissional que só comenta ("vou pedir exames para a falta de ar, tá?")."""
        found = [s for s in self.segments if s.falante in TEAM_SIDE and contains(question_text(s.texto), words)]
        if not found:
            return None
        by_n = {s.n: s for s in self.segments}
        for s in found:
            reply = by_n.get(s.n + 1)
            if reply is not None and reply.falante in PATIENT_SIDE:
                return s, reply
        return found[0], None

    @staticmethod
    def _answer(reply: Segment | None, size: int = 160) -> tuple[str, list[str]]:
        if reply is None:
            return "", []
        text = reply.texto.strip()
        return f"{text[:size]}{'…' if len(text) > size else ''}", [fala_ref(reply.n)]

    def questions(
        self,
        present: dict[str, list[str]],
        emotions: list[Emotion],
        surgery_refs: list[str],
        hyps: list[Hypothesis] | None = None,
        sem: semiology.Semiology | None = None,
        specialty_fields: list | None = None,
    ):
        triggers: dict[str, list[dict]] = {}

        def add(trigger: str, refs: list[str], key: str = "", filled: bool = False, semio: bool = False, **fmt) -> None:
            occ = {"refs": refs, "fmt": fmt, "key": key, "filled": filled, "semio": semio}
            triggers.setdefault(trigger, []).append(occ)

        for key, refs in present.items():
            add(f"sintoma:{key}", refs)
        ambulatory = self.kind == "consulta"  # perguntas de acompanhamento domiciliar só fazem sentido na consulta
        for icd, _, c in self.chart.conditions if ambulatory else []:
            for q in knowledge.questions():
                if q.trigger.startswith("condicao:") and icd.startswith(q.trigger.split(":", 1)[1]):
                    add(q.trigger, [f"Condition/{c['id']}"], key=c["id"])
        reported = self.reported_meds()
        for _ing, cls, name, ref in self._med_classes(reported):
            add(f"classe:{cls}", [ref], key=name, med=name)
        for a in self.chart.ctx.alergias:
            name, _, reaction = a.summary.partition(" — ")
            add(
                "alergia:*",
                [a.source_ref],
                key=name,
                alergeno=name,
                reacao=reaction.split(" (")[0].lower() or "não descrita",
            )
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
        for f in specialty_fields or []:  # campos extras da especialidade (pediatria: gestação e parto, vacinas, DNPM)
            add(f"semiologia:{f.campo}", [], key=f.campo, filled=bool(f.valor), semio=True)

        # semiologia: campos vazios (ou já perguntados) da queixa principal e das duas seguintes
        semio_rows = {
            q.trigger.split(":", 1)[1]: q for q in knowledge.questions() if q.trigger.startswith("semiologia:")
        }
        asked_semio = {(a.campo, a.chave): a for a in (sem.perguntas if sem else [])}
        semio_occ: list[tuple[knowledge.QuestionDef, dict, bool]] = []
        for rank, d in enumerate((sem.queixas if sem else [])[:3]):
            group = knowledge.semiology().group_of(d.chave)
            wanted = [f for f in SEMIO_ORDER if f in group.fields]
            missing = 0
            for field_ in wanted:
                row = semio_rows.get(field_)
                if row is None or row.specialty:
                    continue
                known = bool(getattr(d, field_).valor)
                if known and (field_, d.chave) not in asked_semio:
                    continue
                if not known and (field_, d.chave) not in asked_semio:
                    missing += 1
                    if missing > (3 if rank == 0 else 1):
                        continue
                occ = {
                    "refs": d.refs[:3],
                    "fmt": {"queixa": d.rotulo.lower()},
                    "key": d.chave,
                    "filled": False,
                    "semio": True,
                    "asked": asked_semio.get((field_, d.chave)),
                    "priority": row.priority if rank == 0 else "baixa",
                }
                semio_occ.append((row, occ, True))

        top = [h for h in (hyps or [])[:2]]
        suggested: list[tuple[Question, int, bool, bool, bool, int]] = []
        order: dict[tuple[str, str], int] = {}
        covered: list[tuple[int, Question]] = []
        seen: set[str] = set()
        trat = "a senhora" if self.r.sex == "F" else "o senhor"

        def build(q: knowledge.QuestionDef, occ: dict, semio: bool) -> None:
            if q.specialty and q.specialty != self.specialty:
                return
            if not self._fits(q.age_min, q.age_max, q.sex):
                return
            try:
                text = q.text.format(trat=trat, **occ["fmt"])
            except KeyError:
                return
            if text in seen:
                return
            seen.add(text)
            qid = f"{q.id}:{occ['key']}" if occ["key"] else q.id
            priority = occ.get("priority", q.priority)
            names = [h.hipotese for h in top if h.cid in q.discriminates]
            item = Question(
                id=qid,
                pergunta=text,
                motivo=q.reason,
                prioridade=priority,
                refs=occ["refs"],
                fonte=q.source,
                discrimina=names,
            )
            asked = occ.get("asked")
            if asked is not None:  # semiologia: a pergunta do profissional já foi lida pelo extrator
                origin = next(s for s in self.segments if s.n == asked.pergunta_n)
                reply = next((s for s in self.segments if s.n == asked.resposta_n), None)
                found = (origin, reply)
            elif occ.get("semio") and "queixa" in occ["fmt"]:
                found = None  # pergunta de semiologia ainda não feita: o extrator já olhou as falas
            else:
                found = self.team_asked(q.asked_words) if q.asked_words else None
            if found is not None:
                answer, answer_refs = self._answer(found[1])
                covered.append(
                    (
                        found[0].n,
                        item.model_copy(
                            update={"refs": [fala_ref(found[0].n)], "resposta": answer, "resposta_refs": answer_refs}
                        ),
                    )
                )
            elif occ.get("filled"):
                return  # campo da especialidade que o paciente já informou sem ser perguntado
            else:
                conv_first = any(r.startswith("Fala/") for r in occ["refs"])
                boosted = bool(names)
                nth = order.get((q.trigger, occ["key"]), 0)  # 1ª, 2ª… pergunta da mesma queixa ou do mesmo gatilho
                order[(q.trigger, occ["key"])] = nth + 1
                suggested.append((item, PRIORITY[priority], conv_first, boosted, semio, nth))

        for q in knowledge.questions():
            if q.trigger.startswith("semiologia:") and q.trigger.split(":", 1)[1] in knowledge.semiology().fields:
                continue  # as de queixa vêm de `semio_occ`
            for occ in triggers.get(q.trigger, []):
                build(q, occ, occ["semio"])
        for row, occ, semio in semio_occ:
            build(row, occ, semio)

        # prioridade; dentro dela, as que nascem da conversa antes das do prontuário, uma pergunta de cada queixa antes
        # da segunda e, entre elas, as que ajudam a decidir entre as 2 hipóteses de maior confiança primeiro
        suggested.sort(key=lambda t: (t[1], not t[2], t[5], not t[3]))
        main = [t[0] for t in suggested if not t[4]][:MAX_QUESTIONS]
        tail = [t[0] for t in suggested if t[4]][:8]
        covered.sort(key=lambda t: t[0])
        return main + tail, [c for _, c in covered]

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
        prescribed = {m.ingredient: m for m in self.chart.active_meds}
        for ing, _term, ref in self.suspended_meds():
            if ing in prescribed:
                m = prescribed[ing]
                out.append(
                    MedSuggestion(
                        texto=f"Relata ter parado {m.name}, que segue ativo na prescrição: confirmar adesão, motivo "
                        "e registrar.",
                        tipo="adesao",
                        severidade="atencao",
                        refs=[ref, m.ref],
                    )
                )
        adherence = []
        for s in self.patient_segments:
            if keyword_status(s.texto, FORGET_WORDS) == "presente":  # "não esqueço nunca" não conta
                adherence.append(s)
            elif keyword_status(s.texto, STOP_WORDS) == "presente":
                # com remédio citado, o alerta específico acima (prescrito) basta; "parei de tomar o ibuprofeno"
                # (por conta própria) não é falha de adesão ao tratamento
                if not any(m.status != "negado" for m in mentioned_drugs(s.texto)):
                    adherence.append(s)
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
                # "não tenho alergia a dipirona", "alergia a dipirona não", "tem alergia a algum remédio" (pergunta)
                if keyword_status(clause, ("alergi",), skip_questions=True) != "presente":
                    continue
                for d in knowledge.allergy_defs():
                    hit = contains(clause, d.words)
                    if not hit or keyword_status(clause, d.words) != "presente":
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
            # palavra inteira ("rim" não casa com "primeiro") e com negação ("minha mãe não tem diabetes")
            if m and keyword_status(s.texto, FAMILY_DISEASE) == "presente":
                out.append(
                    HistoryItem(
                        texto=f"Antecedente familiar relatado: {self.quote(s, 90)}",
                        tipo="familiar",
                        refs=[fala_ref(s.n)],
                    )
                )
        catalog = drugs()
        for ing, term, ref in self.suspended_meds():
            name = catalog[ing].name if ing in catalog else term
            out.append(HistoryItem(texto=f"Uso prévio relatado (suspenso): {name}", tipo="relatado", refs=[ref]))
        return out

    def surgery(self) -> tuple[list[SurgeryItem], list[str]]:
        items = self.chart.surgical_history()
        for s in self.segments:
            if keyword_status(s.texto, SURGERY_PLAN_WORDS) != "presente":  # "não vai precisar operar"
                continue
            items.append(
                SurgeryItem(texto=f"Citado na consulta: {self.quote(s, 110)}", tipo="planejada", refs=[fala_ref(s.n)])
            )
        for s in self.patient_segments:
            if keyword_status(s.texto, SURGERY_PAST_WORDS) != "presente":  # "nunca fui operada"
                continue
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

    def semiology(self, present: dict[str, list[str]]) -> semiology.Semiology:
        """Semiologia das queixas presentes; a queixa principal que o profissional fixou vai para o começo."""
        sem = semiology.extract(
            self.segments,
            present,
            self.denied,
            age=self.r.age,
            sex=self.r.sex,
            patient_side=PATIENT_SIDE,
            team_side=TEAM_SIDE,
        )
        pinned = self.extra.get("qp_principal")
        sem.queixas.sort(key=lambda d: d.chave != pinned)  # estável: o resto segue a ordem em que foi dito
        return sem

    def anamnese(
        self,
        allergies: list[AllergyInfo],
        reported,
        details: list[QueixaDetalhada],
        specialty_fields: list,
    ) -> Anamnese:
        meds = [m.text for m in self.chart.active_meds] + [
            f"{term} (relatado, por conta própria)"
            for ing, term, _ in reported
            if ing not in {m.ingredient for m in self.chart.active_meds}
        ]
        own = [(term, ref) for ing, term, ref in reported if ing not in {m.ingredient for m in self.chart.active_meds}]
        extras = []
        if own:
            names = ", ".join(dict.fromkeys(term.lower() for term, _ in own))
            extras.append(f"Refere uso por conta própria de {names}{semiology.falas_text([r for _, r in own])}.")
        context = [self.quote(s, 120) for s in self.patient_segments if contains(self.norm[s.n], CONTEXT_WORDS)]
        refs = sorted({r for d in details for r in d.refs} | {r for _, r in own}, key=lambda r: int(r.split("/")[1]))
        catalog = drugs()
        previous = [
            f"Uso prévio de {catalog[ing].name if ing in catalog else term} (relatado, suspenso)"
            for ing, term, _ in self.suspended_meds()
        ]
        return Anamnese(
            queixa_principal=semiology.complaint_text(details),
            hda=semiology.hda_text(details, extras),
            antecedentes=[h.texto for h in self.chart.history() if h.tipo in ("diagnostico", "cirurgia")] + previous,
            medicacoes_em_uso=meds,
            alergias=[f"{a.alergeno} — {a.reacao}" for a in allergies]
            or (["Nega alergias conhecidas"] if self.chart.ctx.nega_alergias else ["Sem registro"]),
            habitos_e_contexto=context,
            revisao_de_sistemas=[f"Nega {t.rotulo.lower()}" for t in self.tags_denied],
            refs=refs,
            qp_principal=details[0].chave if details else "",
            qp_fixada=bool(details and self.extra.get("qp_principal") == details[0].chave),
            queixas_detalhadas=details,
            campos_especialidade=specialty_fields,
        )

    # ---------- tudo junto ----------
    def run(self) -> Insights:
        if self.kind == "cirurgia":
            report = surgical_report(
                self.segments,
                self.chart,
                self.extra.get("opme_autorizado", []),
                specialty=self.specialty,
                marks=self.extra.get("checklist"),
                consumed=self.extra.get("opme_consumo", []),
                billing=billing_params(self.extra),
            )
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
        self.tags_denied = [t for t in tags if t.status == "negado"]
        sem = self.semiology(present)
        label_key = {d.label: d.key for d in knowledge.symptoms()}
        detail = {d.chave: semiology.tag_detail(d) for d in sem.queixas}
        for t in tags:
            if t.status == "presente":
                t.detalhe = detail.get(label_key.get(t.rotulo, ""), "")
        specialty_fields = semiology.specialty_fields(self.segments, self.specialty, self.r.age, PATIENT_SIDE)
        emotions, timeline, metrics = self.emotions()
        surgery_items, surgery_refs = self.surgery()
        hyps = self.hypotheses(present, tags)
        questions, covered = self.questions(present, emotions, surgery_refs, hyps, sem, specialty_fields)
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
            medicamentos=self.chart.meds(reported, self.suspended_meds()),
            sugestoes_medicamentos=meds_safety,
            cirurgia=surgery_items,
            protocolos=self.chart.protocols(),
            resumo=self.summary(tags, hyps, meds_safety, exams),
            anamnese=self.anamnese(allergies, reported, sem.queixas, specialty_fields),
            alertas=alerts,
            tfg=egfr[0] if egfr else None,
        )
