"""Smart Apps em tempo real (camada determinística).

Roda a cada fala nova, em milissegundos: queixas em tags, emoções, perguntas sugeridas (as já abordadas saem da
lista), hipóteses com evidências, alertas de segurança (dose × função renal, interações com o que o paciente
relatou, alergias), resumo e anamnese estruturada. A camada de IA (app/consult/ai.py) refina e complementa.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.consult import (
    coding,
    cronologia,
    dose_peso,
    encaminhamento,
    extract,
    idiomas,
    knowledge,
    laudos,
    orders,
    reminders,
    semiology,
    sinais,
    summary,
)
from app.consult.chart import ChartView, fmt_date
from app.consult.cid import screen
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
    ProtocolItem,
    QueixaDetalhada,
    Question,
    Rcri,
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
    brands,
    contains,
    find,
    has_vocative,
    keyword_status,
    listener_questions,
    mentioned_drugs,
    norm,
    planned_drugs,
    question_text,
)
from app.medrec.rules import high_alert_findings
from app.reference import (
    adverse_patterns,
    class_label,
    drugs,
    high_alert_reason,
    icd_entry,
    icd_problem,
    interactions,
    matches,
)
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
    # espanhol e inglês (fala ainda sem tradução)
    "se me olvida",
    "se me olvido",
    "me olvido de tomar",
    "olvide tomar",
    "i forget to take",
    "i forgot to take",
    "i keep forgetting",
    "i skip my pills",
)  # sinais de dificuldade de adesão
STOP_WORDS = ("parei de tomar", "deixei de tomar", "deje de tomar", "i stopped taking", "i quit taking")
CONTEXT_WORDS = ("moro ", "sozinh", "fumo", "cigarro", "bebo", "bebida", "alcool", "caminhada", "trabalho", "aposentad")
PRIORITY = {"alta": 0, "media": 1, "baixa": 2}
RELATIVE_ALLERGY = re.compile(
    r"(?<!\w)(minha|meu|a|o|do|da) (mae|pai|irma|irmao|filha|filho|avo|avos|marido|esposa|tia|tio|neta|neto|sobrinha"
    r"|sobrinho|prima|primo|genro|nora|sogra|sogro|vizinha|vizinho|amiga|amigo)(?!\w)"
    # parentes citados em espanhol e inglês (fala ainda sem tradução)
    r"|(?<!\w)(mi|mis|su|sus|el|la|my|her|his|our) (madre|mama|padre|papa|hermana|hermano|hija|hijo|abuela|abuelo"
    r"|esposo|esposa|marido|tia|tio|nieta|nieto|sobrina|sobrino|prima|primo|yerno|nuera|suegra|suegro|vecina|vecino"
    r"|amiga|amigo|mother|mom|father|dad|sister|brother|daughter|son|wife|husband|aunt|uncle|grandmother|grandma"
    r"|grandfather|grandpa|cousin|neighbou?r|friend)(?!\w)"
)
SELF_ALLERGY = re.compile(
    r"(?<!\w)(eu tambem|tambem tenho|eu tenho|eu sou|sou alergic|eu tb|comigo tambem"
    r"|yo tambien|tambien soy|yo soy|soy alergic|yo tengo|i am allergic|i'?m allergic|i also|me too|so am i)(?!\w)"
)
HIGH_ALERT_SOURCE = "Lista de medicamentos potencialmente perigosos (high_alert_meds.csv; conceito do ISMP)"
PLANNED_SUFFIX = " (a prescrever)"  # medicamento que o profissional disse que vai passar: ainda não está no prontuário
MAX_HYPOTHESES = 8
# efeito adverso: o sintoma tem de ter começado até 90 dias depois do início do fármaco (plausibilidade temporal)
ADVERSE_WINDOW_DAYS = 90
ADVERSE_RECENT_DAYS = 30  # sem data de início do sintoma: só vale se o fármaco é recente
NUMBER_WORDS = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8,
    "nove": 9, "dez": 10, "onze": 11, "doze": 12, "quinze": 15, "vinte": 20, "trinta": 30,
}  # fmt: skip
DURATION = re.compile(
    r"(?<!\w)(?:(?:faz|ha|ja faz|tem|desde ha|fazem)\s+)?"
    r"(?:(?:cerca de|mais ou menos|umas|uns|uma|um|quase|pouco mais de|mais de)\s+)*"
    r"(\d+|um|uma|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|quinze|vinte|trinta)\s+"
    r"(dias?|semanas?|mes|meses|anos?)(?!\w)"
)
UNIT_DAYS = {"dia": 1, "semana": 7, "mes": 30, "ano": 365}
REACTION_HIGH = (
    "inchou",
    "inchad",
    "inchei",
    "inchaco",
    "falta de ar",
    "garganta",
    "anafila",
    "choque",
    "desmai",
    "angioedema",
    "labios",
    "boca inchou",
    "passei mal",
)
REACTION_LOW = ("coceira", "manchas", "vermelh", "enjoo", "diarreia", "urticaria", "pele", "erupcao")
ONSET_QUESTION = ("desde quando", "quanto tempo", "quando comecou", "quando isso comecou", "comecou quando")
# ordem em que os campos vazios da queixa viram pergunta
MAX_PROTOCOL_QUESTIONS = 3  # perguntas de protocolo pendente na lista (depois das do quadro e antes da anamnese)
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
        self.all_segments = segments  # inclui os marcos de privacidade (#13), que numeram mas não são fala
        segments = [self._for_rules(s) for s in segments if s.origem != "marco"]
        self.segments = segments
        self.specialty = specialty
        self.kind = kind
        self.extra = extra or {}
        self.norm = {s.n: norm(s.texto) for s in segments}
        self.patient_segments = [s for s in segments if s.falante in PATIENT_SIDE]
        self.denied: dict[str, list[str]] = {}
        self.tags_denied: list[Tag] = []
        self.dropped: list[dict] = []  # itens recusados pelas regras (ex.: CID que a tabela oficial não aceita)
        self._extraction: extract.Extraction | None = None
        self.rcri: Rcri | None = None
        self.protocols: list[ProtocolItem] = []

    @staticmethod
    def _for_rules(s: Segment) -> Segment:
        """Fala estrangeira: as regras leem a tradução (mantendo a Fala/N); sem tradução, ou com tradução divergente
        (doses ou medicamentos trocados), leem o original pelos sinônimos de es/en dos CSVs."""
        if (idiomas.resolver(s.idioma) or s.idioma) == idiomas.PADRAO:
            return s
        if s.traducao and not s.traducao_divergente and not idiomas.placeholder(s.traducao):
            return s.model_copy(update={"texto": s.traducao})
        return s.model_copy(update={"texto": idiomas.texto_para_regras(s.texto, s.idioma)})

    def translation_alerts(self) -> list[Highlight]:
        """ "Tradução divergente": números, doses ou medicamentos que não batem entre a fala e a tradução."""
        return [
            Highlight(
                texto=f"Tradução divergente na fala {s.n} ({idiomas.rotulo(s.idioma)}): "
                + "; ".join(s.traducao_divergente)
                + ". Confira a fala original.",
                severidade="atencao",
                refs=[fala_ref(s.n)],
                categoria="traducao",
            )
            for s in self.all_segments
            if s.traducao_divergente
        ]

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
        defs = [] if self.extra.get("sem_emocoes") else knowledge.emotions()  # emocoes=false no consentimento (#13)
        for s in self.patient_segments if defs else []:
            if s.falante_incerto:
                continue  # falante não confirmado (#40): a emoção não é atribuída a ninguém até conferir
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
            # intensidade pelos intensificadores da fala ("muito medo", "pavor"), não pela contagem de falas (#43)
            intensity, marks = sinais.combine(sinais.intensity_of(s.texto, d.words) for s in segs)
            out.append(
                Emotion(
                    emocao=key,
                    rotulo=d.label,
                    quem=who,
                    intensidade=intensity,
                    trecho=self.quote(segs[-1]),
                    abordagem=d.approach,
                    refs=[fala_ref(s.n) for s in segs],
                    pistas=marks,
                )
            )
        out.sort(key=lambda e: int(e.refs[-1].split("/")[1]), reverse=True)  # mais recente primeiro

        words = {"pro": 0, "pac": 0}
        open_q = closed_q = 0
        incertas = 0
        for s in self.segments:
            if s.falante_incerto:
                incertas += 1
                continue  # só falas com falante de confiança entram nas métricas de comunicação (#40)
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
            falas_incertas=incertas,
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

    def planned_meds(self) -> list[tuple[str, str, str]]:
        """(princípio ou @classe, termo, ref) do que o PROFISSIONAL diz que vai prescrever ("vou passar X")."""
        out: list[tuple[str, str, str]] = []
        for s in self.segments:
            if s.falante not in TEAM_SIDE:
                continue
            for p in planned_drugs(s.texto):
                if all(p.ingredient != o[0] for o in out):
                    out.append((p.ingredient, p.term, fala_ref(s.n)))
        return out

    def _med_classes(
        self, reported: list[tuple[str, str, str]], planned: list[tuple[str, str, str]] | None = None
    ) -> list[tuple[str, str, str, str]]:
        """(princípio, classe, nome, ref) do que está em uso + relatado + a prescrever (nome com sufixo)."""
        catalog = drugs()
        out = [(m.ingredient, m.drug_class, m.name, m.ref) for m in self.chart.active_meds]
        for ing, term, ref in reported:
            if ing.startswith("@"):
                out.append(("", ing[1:], term, ref))
            elif ing in catalog and all(ing != o[0] for o in out):
                out.append((ing, catalog[ing].drug_class, catalog[ing].name, ref))
        for ing, term, ref in planned or []:
            if ing.startswith("@"):
                out.append(("", ing[1:], f"{term}{PLANNED_SUFFIX}", ref))
            elif ing in catalog and all(ing != o[0] for o in out):
                out.append((ing, catalog[ing].drug_class, f"{catalog[ing].name}{PLANNED_SUFFIX}", ref))
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

        self._protocol_questions(suggested, covered, ambulatory)

        # prioridade; dentro dela, as que nascem da conversa antes das do prontuário, uma pergunta de cada queixa antes
        # da segunda e, entre elas, as que ajudam a decidir entre as 2 hipóteses de maior confiança primeiro
        suggested.sort(key=lambda t: (t[1], not t[2], t[5], not t[3]))
        main = [t[0] for t in suggested if not t[4]][:MAX_QUESTIONS]
        tail_items = [t[0] for t in suggested if t[4]]
        tail = [q for q in tail_items if q.id.startswith("protocolo:")][:MAX_PROTOCOL_QUESTIONS]
        tail += [q for q in tail_items if not q.id.startswith("protocolo:")][:8]
        covered.sort(key=lambda t: t[0])
        return main + tail, [c for _, c in covered]

    def _protocol_questions(self, suggested: list, covered: list, ambulatory: bool) -> None:
        """Protocolos vivos (#28): o item tratado na conversa entra em "abordadas" com a fala, e o que ainda falta do
        mesmo protocolo vira a próxima pergunta sugerida."""
        if not ambulatory:
            return
        by_ref = {fala_ref(s.n): s for s in self.segments}
        warm = {p.protocolo for p in self.protocols if p.status == "abordado"}
        nth = 0
        for p in self.protocols:
            if not p.pergunta:
                continue
            if p.status == "abordado" and p.refs and p.refs[0] in by_ref:
                seg = by_ref[p.refs[0]]
                item = Question(
                    id=f"protocolo:{p.id}",
                    pergunta=p.pergunta,
                    motivo=f"Protocolo {p.protocolo}: {p.item}.",
                    prioridade="baixa",
                    refs=[p.refs[0]],
                    fonte=p.fonte,
                    resposta=self.quote(seg),
                    resposta_refs=[p.refs[0]],
                )
                covered.append((seg.n, item))
            elif p.status in ("pendente", "sem_registro"):
                priority = "alta" if p.protocolo in warm else "media"
                item = Question(
                    id=f"protocolo:{p.id}",
                    pergunta=p.pergunta,
                    motivo=f"Protocolo {p.protocolo}: {p.item} ({p.status.replace('_', ' ')}). {p.orientacao}",
                    prioridade=priority,  # type: ignore[arg-type]
                    refs=p.refs or [f"Patient/{self.r.id}"],
                    fonte=p.fonte,
                )
                if nth < MAX_PROTOCOL_QUESTIONS:  # na cauda, depois das perguntas do quadro (não tira vaga delas)
                    suggested.append((item, PRIORITY[priority], False, False, True, 1 + nth))
                    nth += 1

    # ---------- Hipóteses ----------
    def _evidence(
        self,
        token: str,
        present: dict[str, list[str]],
        denied: dict[str, list[str]],
        classes: list[tuple[str, str, str, str]],
        labels: dict[str, str],
    ) -> tuple[Evidence | None, Evidence | None]:
        """Avalia uma ficha de evidência (s, l/v, c, d, p ou n): (a favor, contra por negativa do paciente)."""
        kind, _, spec = token.partition(":")
        if kind == "s":
            if spec in present:
                return Evidence(texto=f"Relata {labels[spec].lower()}", refs=present[spec][:3]), None
            if labels.get(spec) in denied:
                return None, Evidence(texto=f"Nega {labels[spec].lower()}", refs=denied[labels[spec]])
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
                    return Evidence(texto=f"{e.summary} em {fmt_date(e.timestamp)}", refs=[e.source_ref]), None
        elif kind == "c":
            for ing, cls, name, ref in classes:
                if spec in (ing, cls):
                    origin = "relatado na consulta" if ref.startswith("Fala/") else "em uso"
                    return Evidence(texto=f"{name} ({origin})", refs=[ref]), None
        elif kind == "d":
            c = self.chart.has_condition(spec)
            if c is not None:
                return Evidence(texto=c.get("code", {}).get("text", spec), refs=[f"Condition/{c['id']}"]), None
        elif kind == "p":  # procedimento já realizado (prontuário): a cirurgia que tirou o órgão afasta a hipótese
            for proc in self.r.resources.get("Procedure", []):
                text = (proc.get("code") or {}).get("text", "")
                if spec in norm(text):
                    return Evidence(texto=f"Já realizou: {text}", refs=[f"Procedure/{proc['id']}"]), None
        elif kind == "n":
            notes = [e for e in self.chart.notes_text(30) if spec in norm(e.text or "")]
            segs = self.said((spec,))
            if notes:
                return Evidence(texto=f"Registro: “…{spec}…”", refs=[notes[-1].source_ref]), None
            if segs:
                return (
                    Evidence(texto=f"Citado na conversa: {self.quote(segs[-1], 70)}", refs=[fala_ref(segs[-1].n)]),
                    None,
                )
        return None, None

    def hypotheses(self, present: dict[str, list[str]], denied_tags: list[Tag]) -> list[Hypothesis]:
        reported = self.reported_meds()
        classes = self._med_classes(reported)
        labels = {d.key: d.label for d in knowledge.symptoms()}
        denied = {t.rotulo: t.refs for t in denied_tags if t.status == "negado"}
        out = []
        for d in knowledge.differentials():
            if not self._fits(d.age_min, d.age_max, d.sex):
                continue
            problem = icd_problem(d.icd, self.r.sex, primary=True)
            if problem:  # CID que a tabela oficial não aceita para esta hipótese: vai para "descartados" com o motivo
                self.dropped.append(
                    {"onde": "hipoteses", "texto": d.hypothesis, "refs_invalidas": [], "motivo": problem, "cid": d.icd}
                )
                continue
            pro: list[Evidence] = []
            con: list[Evidence] = []
            for ev in d.evidence:
                favor, against = self._evidence(ev, present, denied, classes, labels)
                if favor:
                    pro.append(favor)
                elif against:
                    con.append(against)
            if len(pro) < d.minimum or any(
                not self._evidence(r, present, denied, classes, labels)[0] for r in d.required
            ):
                continue
            for ev in d.against:  # exame ou sinal normal (e sintoma que afasta) conta contra, com a fonte
                found, _ = self._evidence(ev, present, denied, classes, labels)
                if found:
                    kind = ev.partition(":")[0]
                    note = (
                        ": dentro do esperado, não sustenta a hipótese"
                        if kind in ("l", "v")
                        else " (afasta a hipótese)"
                    )
                    con.append(Evidence(texto=f"{found.texto}{note}", refs=found.refs))
            ratio = len(pro) / len(d.evidence)
            if ratio >= 0.75 and len(pro) >= 4:
                confidence = "alta"
            elif ratio >= 0.5 and len(pro) >= 3:
                confidence = "media"
            else:
                confidence = "baixa"
            refs = list(dict.fromkeys(r for e in pro for r in e.refs))
            entry = icd_entry(d.icd)
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
                    cid_descricao=entry.description if entry else "",
                    pergunta_discriminante=d.question,
                    exame_discriminante=d.exam,
                    fonte=d.source,
                )
            )
        rank = {"alta": 0, "media": 1, "baixa": 2}
        # o pacote da especialidade (#27) prioriza hipóteses típicas dela quando a confiança empata
        first = (
            set(knowledge.specialty_pack(self.specialty).hipoteses_prioritarias)
            if self.specialty in knowledge.SPECIALTIES
            else set()
        )
        out.sort(key=lambda h: (rank[h.confianca], h.cid not in first, -len(h.a_favor), len(h.contra)))
        return out[:MAX_HYPOTHESES]

    # ---------- Segurança de medicamentos ----------
    def _candidates(
        self, reported: list[tuple[str, str, str]], planned: list[tuple[str, str, str]]
    ) -> list[tuple[str, str, str, str, str]]:
        """(princípio, classe, nome, ref, situação) de tudo que o paciente toma ou vai tomar, com a fonte."""
        catalog = drugs()
        out = [(m.ingredient, m.drug_class, m.name, m.ref, "em uso") for m in self.chart.active_meds]
        for situation, items in (
            ("relatado na consulta", reported),
            ("que o profissional disse que vai prescrever", planned),
        ):
            for ing, term, ref in items:
                if ing.startswith("@"):
                    out.append(("", ing[1:], term, ref, situation))
                elif ing in catalog:
                    out.append((ing, catalog[ing].drug_class, catalog[ing].name, ref, situation))
        return out

    def _allergy_matches(
        self,
        allergies: list[AllergyInfo],
        reported: list[tuple[str, str, str]],
        planned: list[tuple[str, str, str]],
    ) -> list[tuple[AllergyInfo, str, str, str, str]]:
        """(alergia, 'direto'|'cruzado', nome, ref, situação) dos medicamentos que batem com cada alergia — o que está
        em uso, o que o paciente relatou e o que o profissional disse que vai prescrever."""
        defs = {d.allergen: d for d in knowledge.allergy_defs()}
        out = []
        for a in allergies:
            d = defs.get(a.chave)
            for ing, cls, name, ref, situation in self._candidates(reported, planned):
                if a.chave and matches(a.chave, ing, cls):
                    out.append((a, "direto", name, ref, situation))
                elif d is not None and any(matches(t, ing, cls) for t in d.cross_with):
                    out.append((a, "cruzado", name, ref, situation))
        return out

    def _allergy_alerts(self, matches_: list[tuple[AllergyInfo, str, str, str, str]]) -> list[MedSuggestion]:
        out = []
        seen: set[tuple[str, str, str]] = set()
        for a, kind, name, ref, situation in matches_:
            key = (a.chave or a.alergeno, kind, name)
            if key in seen:
                continue
            seen.add(key)
            origin = "consta no prontuário" if a.fonte == "prontuario" else "relatada na consulta"
            refs = list(dict.fromkeys([ref, *a.refs]))
            if kind == "direto":
                tail = "Rever antes de prescrever." if "prescrever" in situation else "Rever a prescrição."
                text = f"Alergia a {a.alergeno} ({origin}): {name}, {situation}, é do mesmo grupo. {tail}"
                if situation == "em uso":
                    text = f"Alergia a {a.alergeno} ({origin}): {name} em uso, do mesmo grupo. {tail}"
                out.append(
                    MedSuggestion(texto=text, tipo="alergia", severidade="critico", refs=refs, fonte=a.fonte_regra)
                )
            else:
                text = (
                    f"Reatividade cruzada com a alergia a {a.alergeno} ({origin}): {name}, {situation}. "
                    f"{a.reatividade_cruzada} Avaliar antes de prescrever."
                    if "prescrever" in situation
                    else f"Reatividade cruzada com a alergia a {a.alergeno} ({origin}): {name} {situation}. "
                    f"{a.reatividade_cruzada}"
                )
                out.append(
                    MedSuggestion(texto=text, tipo="alergia", severidade="atencao", refs=refs, fonte=a.fonte_regra)
                )
        return out

    def _onset_days(self, refs: list[str]) -> int | None:
        """Há quantos dias o sintoma começou, pelo que o paciente disse na(s) fala(s) dele ou ao responder "desde
        quando?". None se a conversa não diz."""
        by_n = {s.n: i for i, s in enumerate(self.segments)}
        for ref in refs:
            i = by_n.get(int(ref.split("/")[1]))
            if i is None:
                continue
            for j in (i, i + 2):
                if j >= len(self.segments):
                    continue
                seg = self.segments[j]
                if j != i and not (
                    self.segments[i + 1].falante in TEAM_SIDE
                    and contains(self.norm[self.segments[i + 1].n], ONSET_QUESTION)
                    and seg.falante in PATIENT_SIDE
                ):
                    continue
                m = DURATION.search(self.norm[seg.n])
                if m:
                    raw = m.group(1)
                    n = int(raw) if raw.isdigit() else NUMBER_WORDS.get(raw, 0)
                    unit = m.group(2).rstrip("s")
                    unit = "mes" if unit.startswith("mes") else unit
                    return int(n * UNIT_DAYS.get(unit, 0)) or None
        return None

    def adverse_effects(self, present: dict[str, list[str]]) -> list[MedSuggestion]:
        """Sintoma que começou DEPOIS do início de um fármaco com padrão adverso (adverse_patterns.csv): sugere ao
        profissional avaliar a relação. Cita a fala do sintoma, a fala em que o paciente liga o remédio e a receita."""
        labels = {d.key: d.label for d in knowledge.symptoms()}
        now = self.chart.now
        out = []
        for m in self.chart.active_meds:
            start = m.start or m.authored
            if start is None or start > now:
                continue
            rows = [
                r
                for r in adverse_patterns()
                if r["tipo"] == "efeito_adverso"
                and r.get("sintomas")
                and matches(r["medicamento"], m.ingredient, m.drug_class)
            ]
            flagged: dict[str, tuple[list[str], int | None]] = {}
            for r in rows:
                for key in r["sintomas"].split("|"):
                    if key not in present or key in flagged:
                        continue
                    days = self._onset_days(present[key])
                    if days is not None:
                        gap = ((now - start).days) - days  # dias entre o início do fármaco e o do sintoma
                        if not 0 <= gap <= ADVERSE_WINDOW_DAYS:
                            continue
                    elif (now - start).days > ADVERSE_RECENT_DAYS:
                        continue
                    flagged[key] = (present[key], days)
            if not flagged:
                continue
            descriptions = list(
                dict.fromkeys(r["descricao"] for r in rows if set(r["sintomas"].split("|")) & set(flagged))
            )
            # "tontura ao levantar" é variante de "tontura": fica o sintoma-mãe
            parents = {d.key: d.group for d in knowledge.symptoms() if d.group}
            names = [labels[k] for k in flagged if parents.get(k) not in flagged]
            title = " e ".join([", ".join(names[:-1]), names[-1].lower()] if len(names) > 1 else names)
            known = [d for _, d in flagged.values() if d]
            timing = (
                f"sintoma há cerca de {max(known)} dias"
                if known
                else "início do sintoma não informado: confirmar se foi depois do remédio"
            )
            words = {norm(m.name), *(b for b, ing in brands().items() if ing == m.ingredient)}
            klass = norm(class_label(m.drug_class)).split()[0] if m.drug_class else ""
            if len(klass) >= 6:
                words.add(klass)
            link = [fala_ref(s.n) for s in self.patient_segments if contains(self.norm[s.n], tuple(sorted(words)))]
            refs = list(dict.fromkeys([r for refs_, _ in flagged.values() for r in refs_[:2]] + link[:2] + [m.ref]))
            out.append(
                MedSuggestion(
                    texto=f"{title} após o início de {m.name} em {fmt_date(start)} ({timing}): "
                    f"{'; '.join(descriptions)}. Avaliar a relação temporal e considerar ajustar o medicamento.",
                    tipo="efeito_adverso",
                    severidade="atencao",
                    refs=refs,
                    fonte="Padrões de efeito adverso curados (adverse_patterns.csv)",
                )
            )
        return out

    def med_safety(
        self,
        allergies: list[AllergyInfo],
        present: dict[str, list[str]] | None = None,
        planned: list[tuple[str, str, str]] | None = None,
    ) -> list[MedSuggestion]:
        """Alertas de medicamento. Alerta que depende de uma fala com medicamento, dose ou alergia de baixa confiança
        na transcrição (#38) ganha a marca "confirmar o que foi ouvido" até o profissional confirmar a fala (ou
        corrigir o texto). A gravidade NUNCA desce: regra de segurança não é rebaixada por dúvida do reconhecimento
        (ADR 0009); um alerta crítico sobre algo mal ouvido continua crítico e visível."""
        out = self._med_safety(allergies, present, planned)
        duvida = {fala_ref(s.n): s for s in self.segments if s.confirmar and not s.confirmado_em}
        if not duvida:
            return out
        for m in out:
            hits = [duvida[r] for r in m.refs if r in duvida]
            if not hits:
                continue
            m.confirmar_ouvido = True
            ouvido = ", ".join(dict.fromkeys(w for s in hits for w in s.confirmar))
            m.texto = f"Confirmar o que foi ouvido («{ouvido}»): {m.texto}"
        return out

    def _med_safety(
        self,
        allergies: list[AllergyInfo],
        present: dict[str, list[str]] | None = None,
        planned: list[tuple[str, str, str]] | None = None,
    ) -> list[MedSuggestion]:
        reported = self.reported_meds()
        planned = self.planned_meds() if planned is None else planned
        classes = self._med_classes(reported, planned)
        out: list[MedSuggestion] = []
        egfr = self.chart.egfr
        age = self.r.age or 0
        for ing, cls, name, ref in classes:
            fired: set[str] = set()
            is_planned = name.endswith(PLANNED_SUFFIX)
            for rule in sorted(knowledge.dose_alerts(), key=lambda r: r.limit):
                if not matches(rule.item, ing, cls) or rule.criterion in fired:
                    continue
                if rule.criterion == "tfg" and egfr and egfr[0] < rule.limit:
                    fired.add("tfg")
                    out.append(
                        MedSuggestion(
                            texto=f"{name}: {rule.recommendation} (TFG estimada ~{round(egfr[0])} mL/min/1,73 m²)",
                            tipo="alternativa"
                            if is_planned and rule.severity == "critico"
                            else "suspender"
                            if rule.severity == "critico"
                            else "ajuste_dose",
                            severidade=rule.severity,
                            refs=[ref, egfr[1].source_ref],
                            fonte=rule.source,
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
                            fonte=rule.source,
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
                                fonte=row.get("fonte", ""),
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
                    fonte="KDIGO: CKD guideline (2024); bulas dos AINE (ANVISA)",
                )
            )
        out += self._allergy_alerts(self._allergy_matches(allergies, reported, planned))
        out += dose_peso.alerts(self)  # criança: dose citada ou prescrita contra o último peso (mg/kg)
        out += self.adverse_effects(present or {})
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
        # selo de alto alerta (MPP): dupla checagem. O que está no prontuário usa os achados da reconciliação
        # (medrec); o que foi relatado ou será prescrito passa pelo mesmo motivo, com mais peso se for novo
        sealed: set[str] = set()
        for f in high_alert_findings(self.chart.active_meds, set()):
            sealed.update(f.medicamentos)
            out.append(
                MedSuggestion(
                    texto=f"{f.titulo}. {f.detalhe}.",
                    tipo="alto_alerta",
                    severidade=f.severidade,
                    refs=f.refs,
                    fonte=HIGH_ALERT_SOURCE,
                )
            )
        for ing, cls, name, ref in classes:
            reason = high_alert_reason(ing, cls)
            if reason and (ing or cls) not in sealed:
                sealed.add(ing or cls)
                out.append(
                    MedSuggestion(
                        texto=f"Dupla checagem: {name}. {reason}.",
                        tipo="alto_alerta",
                        severidade="atencao" if name.endswith(PLANNED_SUFFIX) else "info",
                        refs=[ref],
                        fonte=HIGH_ALERT_SOURCE,
                    )
                )
        out.sort(key=lambda m: SEVERITY[m.severidade])
        return out

    # ---------- Alergias relatadas ----------
    def allergies(self, planned: list[tuple[str, str, str]] | None = None) -> list[AllergyInfo]:
        reported_meds = self.reported_meds()
        planned = self.planned_meds() if planned is None else planned
        out = self.chart.chart_allergies([(ing, term) for ing, term, _ in reported_meds])
        known = {norm(a.alergeno) for a in out}
        for s in self.patient_segments:
            for clause in self.norm[s.n].split("."):
                # "alergia a penicilina, não" (vírgula posta pela transcrição) é o mesmo que "alergia a penicilina não"
                clause = re.sub(r",\s*nao\s*$", " nao", clause.strip())
                # "não tenho alergia a dipirona", "alergia a dipirona não", "tem alergia a algum remédio" (pergunta)
                if keyword_status(clause, ("alergi", "allergi"), skip_questions=True) != "presente":
                    continue
                if s.falante == "paciente" and RELATIVE_ALLERGY.search(clause) and not SELF_ALLERGY.search(clause):
                    continue  # "minha mãe tem alergia a sulfa" é da mãe, não do paciente
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
                            conflitos=[],
                            refs=[fala_ref(s.n)],
                            chave=d.allergen,
                            criticidade=self._reaction_severity(self.norm[s.n]),
                            fonte_regra=d.source,
                        )
                    )
        # conflitos (e reatividade cruzada) com o que está em uso, o que o paciente relatou e o que o médico vai passar
        for a, kind, name, _ref, situation in self._allergy_matches(out, reported_meds, planned):
            line = f"{name} {situation}" if kind == "direto" else f"possível reação cruzada: {name} {situation}"
            if line not in a.conflitos:
                a.conflitos.append(line)
        return out

    @staticmethod
    def _reaction_severity(clause: str) -> str:
        """Criticidade (FHIR) pelo que o paciente conta da reação: via aérea/anafilaxia = alta; só pele = baixa."""
        if contains(clause, REACTION_HIGH):
            return "alta"
        return "baixa" if contains(clause, REACTION_LOW) else "indeterminada"

    # ---------- Histórico relatado + cirurgia citada ----------
    def extraction(self) -> extract.Extraction:
        """Antecedentes, cirurgias e exames ditos na conversa, estruturados e conciliados (uma vez por análise)."""
        if self._extraction is None:
            self._extraction = extract.extract_all(self.segments, self.chart)
        return self._extraction

    def history(self) -> list[HistoryItem]:
        """Prontuário + o que foi dito. O item do prontuário que um relato já cobre sai (o relato o cita)."""
        found = self.extraction()
        covered = found.matched_chart_refs
        items = [h for h in self.chart.history() if not set(h.refs) & covered]
        items += self.reported_history() + found.antecedentes + found.cirurgias
        order = {"diagnostico": 0, "cirurgia": 1, "internacao": 2, "pronto_socorro": 3, "consulta": 4, "relatado": 5}
        items.sort(key=lambda i: -1 if i.condicao else order.get(i.tipo, 9))
        return items

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
        found = self.extraction()
        covered = found.matched_chart_refs
        items = [i for i in self.chart.surgical_history() if not set(i.refs) & covered]
        for s in self.segments:
            if keyword_status(s.texto, SURGERY_PLAN_WORDS) != "presente":  # "não vai precisar operar"
                continue
            items.append(
                SurgeryItem(texto=f"Citado na consulta: {self.quote(s, 110)}", tipo="planejada", refs=[fala_ref(s.n)])
            )
        items += [
            SurgeryItem(texto=f"Cirurgia prévia relatada: {h.texto}", tipo="historico", refs=h.refs)
            for h in found.cirurgias
        ]
        structured = {r for h in found.cirurgias for r in h.refs}
        for s in self.patient_segments:
            if fala_ref(s.n) in structured:  # a cirurgia desta fala já saiu estruturada (procedimento, ano, selo)
                continue
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
            self.rcri = extract.rcri(self.chart, found, planned)
            if self.rcri:
                # checagem pré-operatória como as do prontuário: cita os recursos FHIR (as falas estão no RCRI completo)
                refs = [r for r in self.rcri.refs if not r.startswith("Fala/")]
                items.append(SurgeryItem(texto=self.rcri.interpretacao, tipo="checagem", refs=refs))
        return items, [r for i in planned for r in i.refs]

    # ---------- Resumo e anamnese ----------
    def summary(self, tags: list[Tag], hyps: list[Hypothesis], meds: list[MedSuggestion], exams, emotions) -> Summary:
        sentences = summary.build(self, tags, hyps, exams, emotions)
        highlights: list[Highlight] = [
            Highlight(texto=m.texto, severidade=m.severidade, refs=m.refs) for m in meds if m.severidade == "critico"
        ]
        highlights += reminders.critical_vitals(self)
        for e in exams:
            if e.status != "normal" and e.chave in ("glicemia", "potassio", "sodio") and e.origem_dado == "prontuário":
                highlights.append(
                    Highlight(texto=f"{e.exame}: {e.valor} ({e.data})", severidade="atencao", refs=e.refs)
                )
        for t in tags:
            if t.status == "presente" and t.rotulo in ("Queda ou quase queda", "Dor no peito", "Falta de ar"):
                highlights.append(Highlight(texto=f"Relata: {t.rotulo.lower()}", severidade="atencao", refs=t.refs))
        return Summary(texto=summary.text_of(sentences), destaques=highlights[:8], frases=sentences)

    def alerts(self, meds: list[MedSuggestion]) -> list[Highlight]:
        """Críticos primeiro (medicamento e sinal vital da triagem), depois os de atenção, que a tela recolhe."""
        found = [Highlight(texto=m.texto, severidade=m.severidade, refs=m.refs) for m in meds if m.severidade != "info"]
        found += reminders.critical_vitals(self)
        return sorted(found, key=lambda h: SEVERITY[h.severidade])

    def protocol_alerts(self, protocols: list[ProtocolItem]) -> list[Highlight]:
        """Risco de suicídio dito ou item 9 do PHQ-9 positivo: crítico, com a fala ou a escala de prova."""
        return [
            Highlight(
                texto=f"{p.item}: {p.ultimo}. Avaliar o risco hoje (pergunta direta, plano, meios e rede de apoio) e "
                "decidir a conduta de segurança.",
                severidade="critico",
                refs=p.refs,
                categoria="protocolo_critico",
            )
            for p in protocols
            if p.status == "critico"
        ]

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
    def timeline_events(self, ins: Insights) -> list[dict]:
        """Cronologia viva: eventos tipados (queixa, negativa, achado, medicamento, exame, orientação, retorno) a cada
        fala, com Fala/N, falante e mm:ss (#15). Só regras: vale com ou sem IA."""
        return cronologia.build(self.segments, ins)

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
            report.cids, self.dropped = screen(
                report.cids, "cids", self.r.sex, primary=lambda c: c.tipo != "secundario", trusted=self.chart.icd_codes
            )
            report.cids = coding.enrich(report.cids, self.segments, self.chart)
            coding.attach_sigtap(report, self.r.sex, self.r.age)
            return Insights(
                tipo="cirurgia",
                especialidade=self.specialty,
                falas_analisadas=len(self.all_segments),
                modo="regras",
                descartados=self.dropped,
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
        protocols = self.protocols = self.chart.protocols(self.segments, self.extra.get("escalas"))
        questions, covered = self.questions(present, emotions, surgery_refs, hyps, sem, specialty_fields)
        planned = self.planned_meds()
        allergies = self.allergies(planned)
        meds_safety = self.med_safety(allergies, present, planned)
        reported = self.reported_meds()
        laudo_list = list(self.extra.get("laudos") or [])
        exams = self.chart.exams() + self.extraction().exames + laudos.exam_infos(laudo_list, self.chart)
        history = self.history()
        egfr = self.chart.egfr
        order_ctx = orders.OrderContext(
            chart=self.chart,
            age=self.r.age,
            in_use=self._med_classes(reported),
            allergies=orders.allergen_keys(self.chart, allergies),
            pending=self.r.pending_exams,
        )
        drafts = orders.build(self.segments, self.chart, order_ctx, protocols, hyps, self.extra.get("pedidos_decisoes"))
        referrals = [] if self.kind == "cirurgia" else encaminhamento.build(self.chart, self.segments, hyps, self.extra)
        alerts = self.alerts(meds_safety)
        alerts = sorted(
            [
                *self.translation_alerts(),
                *self.protocol_alerts(protocols),
                *laudos.alertas(laudo_list, self.chart),
                *alerts,
            ],
            key=lambda h: SEVERITY[h.severidade],
        )
        alerts += coding.compat_alerts(  # diagnóstico do prontuário que não combina com o sexo ou a idade do paciente
            [(icd, label, [f"Condition/{c['id']}"]) for icd, label, c in self.chart.conditions if icd],
            self.r.sex,
            self.r.age,
        )
        return Insights(
            tipo=self.kind,
            especialidade=self.specialty,
            falas_analisadas=len(self.all_segments),
            modo="regras",
            queixas=tags,
            emocoes=emotions,
            preocupacoes=sinais.unanswered_concerns(self.segments, not self.extra.get("sem_emocoes")),
            linha_emocional=timeline,
            comunicacao=metrics,
            perguntas=questions,
            perguntas_abordadas=covered,
            hipoteses=hyps,
            historico=history,
            alergias=allergies,
            exames=exams,
            medicamentos=self.chart.meds(reported, self.suspended_meds(), planned),
            sugestoes_medicamentos=meds_safety,
            cirurgia=surgery_items,
            protocolos=protocols,
            pedidos=drafts,
            encaminhamentos=referrals,
            resumo=self.summary(tags, hyps, meds_safety, exams, emotions),
            lembretes=reminders.build(self),
            anamnese=self.anamnese(allergies, reported, sem.queixas, specialty_fields),
            alertas=alerts,
            tfg=egfr[0] if egfr else None,
            rcri=self.rcri,
            descartados=self.dropped,
        )
