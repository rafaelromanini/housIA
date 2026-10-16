"""Verificações cirúrgicas determinísticas sobre o ditado e o prontuário, cada uma com a sua fonte.

- Cirurgia Segura (OMS): sign-in, time-out e sign-out marcados por voz ("time-out realizado") ou por botão;
  só citar a etapa ("vamos fazer o time-out?", "sign-in pendente") não a marca. Termo de consentimento (Consent no
  prontuário ou citado no ditado).
- Sítio e lateralidade: o lado ditado é conferido com o bodySite do ServiceRequest/Appointment agendado; antecedentes
  ("joelho esquerdo operado há 3 anos") e perguntas não contam.
- Contagem com sentido: "confere"/"correta"/"nenhuma faltando" é ok; "não confere"/"não está batendo"/"faltando" é
  crítico.
- Antibioticoprofilaxia (data/reference/surgical_prophylaxis.csv, ASHP/IDSA 2013): agente recomendado para o
  procedimento, janela antes da incisão, dose pelo peso do prontuário (mg/kg em pediatria) e redose pela duração.
- Garrote: tempo de isquemia acima de 2 horas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.consult import knowledge
from app.consult.knowledge import ProphylaxisDef
from app.consult.schemas import Highlight, SafetyCheck, Segment, fala_ref
from app.consult.templates import ResolvedTemplate, first_hit, surgical_templates, syn
from app.consult.text import norm
from app.fhir_client import codes
from app.timeline.record import PatientRecord

if TYPE_CHECKING:
    from app.consult.chart import ChartView

WORD_NUMBERS = {
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
    "onze": 11,
    "doze": 12,
}
MINUTE_WORDS = {
    "cinco": 5,
    "dez": 10,
    "quinze": 15,
    "vinte": 20,
    "meia": 30,
    "trinta": 30,
    "quarenta": 40,
    "cinquenta": 50,
}
NUM = r"(\d+(?:[,.]\d+)?|" + "|".join(WORD_NUMBERS) + ")"
CLAUSE_SPLIT = re.compile(r"(?<!\d)[.;!?](?!\d)|,\s+|\n| mas ")
SENTENCE_SPLIT = re.compile(r"(?<!\d)[.;!](?!\d)|\n")
QUESTION = re.compile(r"[^.;!?\n]*\?")  # pergunta ("vamos fazer o time-out?") não registra que algo foi feito
NEG_WORD = re.compile(r"(?<![a-z])(?:nao|sem|nunca|nenhum|nenhuma)(?![a-z])")
# "time-out não realizado", "time-out ainda não foi feito", "cefazolina 2 g não foi feita"
NEG_AFTER = re.compile(r"^\W*(?:\d+(?:[,.]\d+)?\s*(?:g|mg|gramas?|miligramas?)\s+)?(?:ainda\s+)?(?:nao|nunca)(?![a-z])")
CONJUNCTION = re.compile(r"(?<![a-z])(?:e|mas|porem)(?![a-z])")
SIDE_RE = re.compile(r"(?<![a-z])(direit[oa]s?|esquerd[oa]s?|bilateral|bilaterais|ambos os lados)(?![a-z])")
# antecedente ("colecistectomia prévia", "artroplastia do joelho esquerdo há 3 anos"): não é o que está sendo operado
HISTORY = re.compile(
    r"(?<![a-z])(?:previ[ao]s?|antecedentes?|anteriormente|pregress[ao]s?|historia de|historico de|"
    r"ja (?:fez|foi|teve|realizou|operou|operad[ao]|submetid[ao])|em (?:19|20)\d\d|ano passado|anos atras|"
    r"ha (?:\d+|" + "|".join(WORD_NUMBERS) + r") (?:anos?|mes|meses))(?![a-z])"
)
_MINUTE = r"(\d{2}|" + "|".join(MINUTE_WORDS) + r")(?:\s+e\s+(cinco))?"
_NOT_UNIT = r"(?!\s*(?:mg|g|ml|kg|gramas?|miligramas?|mililitros?)(?![a-z]))"
CLOCK_AS = re.compile(
    r"(?<![a-z])(?:as|a partir das|desde as)\s+(\d{1,2}|" + "|".join(WORD_NUMBERS) + r")"
    r"(?:\s*(?:h|:|horas?)(?![a-z]))?(?:\s*(?:e\s+)?" + _MINUTE + _NOT_UNIT + r")?(?:\s*min(?:utos)?)?(?![a-z0-9])"
)
CLOCK_H = re.compile(r"(?<![\d,.])(\d{1,2})(?:h|:)(\d{2})?(?!\d)")
DURATION_H = re.compile(NUM + r"\s*(?:h|horas?)(?![a-z0-9])(?:\s*e\s*(\d{1,2})\s*(?:min|minutos?))?")
DURATION_MIN = re.compile(r"(\d{1,3})\s*(?:min|minutos?)(?![a-z])")
DOSE_RE = re.compile(NUM + r"\s*(g|gramas?|mg|miligramas?)(?![a-z])(\s*(?:/|por)\s*(?:kg|quilo))?")
VOLUME_RE = re.compile(r"(\d{1,2}\.\d{3}|\d+)\s*(?:ml|mililitros?)(?![a-z])")
VOLUME_L_RE = re.compile(NUM + r"\s*(?:l|litros?)(?![a-z])")

# etapa: (rótulo, palavras do ditado, o que a etapa confere)
WHO_STEPS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "sign_in": (
        "Sign-in (antes da indução anestésica)",
        ("sign in", "sign-in", "signin", "checklist de entrada"),
        "identidade, sítio, procedimento e consentimento confirmados; oxímetro e alergias checados",
    ),
    "time_out": (
        "Time-out (antes da incisão)",
        ("time out", "time-out", "timeout", "pausa cirurgica", "pausa de seguranca"),
        "equipe confirma paciente, sítio e procedimento; profilaxia nos últimos 60 minutos",
    ),
    "sign_out": (
        "Sign-out (antes de sair da sala)",
        ("sign out", "sign-out", "signout", "checklist de saida"),
        "procedimento registrado, contagens conferidas e peças identificadas",
    ),
}
# etapa feita: a palavra da etapa sozinha ("vamos fazer o time-out", "sign-in pendente") não registra a etapa
WHO_DONE = (
    "realizad",
    "feito",
    "feita",
    "fizemos",
    "conclui",
    "complet",
    "ok ",
    "confirmad",
    "checad",
    "conferid",
    "efetuad",
    "executad",
    "finalizad",
    "aplicad",
)
CONSENT_WORDS = ("consentimento", "tcle", "termo assinado")
CONSENT_REFUSED = ("nega ", "negou", "recus", "revog")
COUNT_WORDS = ("contagem", "compressas", "compressa", "gazes", "gaze ", "instrumentais", "agulhas")
# o sentido vem da negação: "não confere"/"não está batendo" é divergência; "nenhuma compressa faltando" confere
COUNT_BAD = (
    "falt",
    "incorret",
    "errad",
    "divergen",
    "discrepan",
    "incomplet",
    "extraviad",
    "sumiu",
    "nao encontrad",
    "nao localizad",
)
COUNT_OK = (
    "confere",
    "conferem",
    "conferid",
    "corret",
    "complet",
    "fechad",
    "fecha ",
    "bate",
    "batem",
    "ok ",
    "certa ",
    "certo ",
    "encontrad",
    "localizad",
)
ALLERGY_WORDS = ("alergi", "alergic")
REDOSE_WORDS = ("redose", "repique", "nova dose", "repetid", "segunda dose", "dose adicional", "reforco")
INCISION_WORDS = (
    "incisao",
    "inicio da cirurgia",
    "inicio do procedimento",
    "cirurgia iniciada",
    "procedimento iniciado",
    "iniciada as",
    "iniciado as",
)
END_WORDS = ("termino", "fim da cirurgia", "fim do procedimento", "final da cirurgia", "encerrad", "terminou")
DURATION_WORDS = ("duracao", "durou", "tempo cirurgico", "tempo de cirurgia", "cirurgia de", "procedimento de")
TOURNIQUET_WORDS = ("garrote", "torniquete", "isquemia")
SOURCE_OMS = "oms_cirurgia_segura"
SOURCE_MS = "ms_cirurgia_segura"
SOURCE_RSI = "aorn_rsi"
SOURCE_TOURNIQUET = "aorn_garrote"
SOURCE_ATB = "ashp_2013"
TOURNIQUET_MAX_MIN = 120
PATIENT_VOICES = ("paciente", "acompanhante")  # quem fala de fora da equipe: não registra etapa, dose nem horário
SIDE_NEG_REACH = 40  # a negação do lado ("não foi o esquerdo") pode estar mais longe que a de uma palavra-chave
ADULT_AGE = 18  # abaixo disso, dose pediátrica (mg/kg)
BLOOD_LOSS_REDOSE_ML = 1500  # ASHP/IDSA 2013: redose também quando a perda passa de 1.500 mL
DAY = 24 * 60


# ---------- utilidades de texto ----------
def clauses(text: str) -> list[tuple[str, str]]:
    """Orações da fala: (texto original, texto normalizado). Os separadores são ASCII, então cortam igual."""
    out = []
    for raw in CLAUSE_SPLIT.split(text):
        if raw and raw.strip():
            out.append((raw.strip(), norm(raw)))
    return out


def _num(token: str) -> float:
    return float(WORD_NUMBERS[token]) if token in WORD_NUMBERS else float(token.replace(",", "."))


def negated(clause: str, words: tuple[str, ...]) -> bool:
    """Negação junto da palavra-chave na mesma oração ("sem radioscopia", "time-out não realizado").

    "Não houve intercorrências e a radioscopia..." não nega a radioscopia: a negação precisa estar perto (até 25
    caracteres) e sem conjunção no meio.
    """
    hit = first_hit(clause, words)
    if hit is None:
        return False
    idx = clause.find(hit)
    before = clause[:idx]
    found = list(NEG_WORD.finditer(before))
    last = found[-1] if found else None
    if last and len(before) - last.end() <= 25 and not CONJUNCTION.search(before[last.end() :]):
        return True
    return bool(NEG_AFTER.match(clause[idx + len(hit) :]))


def side_negated(clause: str) -> bool:
    """Lado negado na oração ("não foi o joelho esquerdo", "esquerdo não"): não é o lado operado."""
    m = SIDE_RE.search(clause)
    if not m:
        return False
    before = clause[: m.start()]
    found = list(NEG_WORD.finditer(before))
    last = found[-1] if found else None
    if last and len(before) - last.end() <= SIDE_NEG_REACH and not CONJUNCTION.search(before[last.end() :]):
        return True
    return bool(NEG_AFTER.match(clause[m.end() :]))


def side_of(text: str) -> str | None:
    m = SIDE_RE.search(norm(text))
    if not m:
        return None
    word = m.group(1)
    if word.startswith("direit"):
        return "direito"
    if word.startswith("esquerd"):
        return "esquerdo"
    return "bilateral"


def affirmative(text: str) -> str:
    """Fala sem as perguntas: "vamos fazer o time-out?" ou "precisa de RX de controle?" não registram nada."""
    return QUESTION.sub(" ", text)


def is_history(clause: str) -> bool:
    """Oração de antecedente ("colecistectomia prévia", "joelho esquerdo operado há 3 anos")."""
    return bool(HISTORY.search(clause))


def _minute(token: str | None, extra: str | None) -> int:
    if not token:
        return 0
    value = MINUTE_WORDS[token] if token in MINUTE_WORDS else int(token)
    return value + (5 if extra else 0)


def clocks_in(clause: str) -> list[tuple[int, int]]:
    """Horários citados, na ordem: (posição, minutos desde a meia-noite).

    Aceita "às 8h", "8h30", "às 14:10", "às oito horas", "às 9 e meia", "às oito e quarenta".
    """
    out: list[tuple[int, int]] = []
    taken: list[tuple[int, int]] = []
    for m in CLOCK_AS.finditer(clause):
        hour, minute = int(_num(m.group(1))), _minute(m.group(2), m.group(3))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            out.append((m.start(), hour * 60 + minute))
            taken.append(m.span())
    for m in CLOCK_H.finditer(clause):
        if any(a <= m.start() < b for a, b in taken):
            continue
        if clause[: m.start()].endswith(("cada ", "de ", "por ", "durante ", "apos ", "ha ")):
            continue  # "a cada 4h", "garrote de 2h": duração, não horário
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            out.append((m.start(), hour * 60 + minute))
    return sorted(out)


def clock_in(clause: str) -> int | None:
    """Primeiro horário citado, em minutos desde a meia-noite."""
    found = clocks_in(clause)
    return found[0][1] if found else None


def clock_near(clause: str, hit: str, stop: tuple[str, ...] = ()) -> int | None:
    """Horário da palavra-chave: o dito logo depois dela (até a palavra de outro evento) ou, senão, o dito antes.

    "cefazolina às 8h incisão às 9h30": a incisão é às 9h30 e o antibiótico às 8h, mesmo sem pontuação.
    """
    idx = clause.find(hit)
    if idx < 0:
        return None
    after = clause[idx:]
    cut = first_hit(after[len(hit) :], stop) if stop else None
    if cut:
        after = after[: len(hit) + after[len(hit) :].find(cut)]
    when = clock_in(after)
    if when is not None:
        return when
    before = clause[:idx]
    return None if stop and first_hit(before, stop) else clock_in(before)


def duration_in(clause: str) -> int | None:
    """Duração citada, em minutos ("5 horas", "2h e 30 min", "90 minutos"); horários ("às 8h") não contam."""
    for m in DURATION_H.finditer(clause):
        if clause[: m.start()].endswith(("as ", "das ")):
            continue
        return round(_num(m.group(1)) * 60) + int(m.group(2) or 0)
    m = DURATION_MIN.search(clause)
    return int(m.group(1)) if m else None


def hhmm(minutes: int) -> str:
    hour, minute = (minutes // 60) % 24, minutes % 60
    return f"{hour}h{minute:02d}" if minute else f"{hour}h"


def fmt_g(grams: float) -> str:
    return f"{grams:g} g".replace(".", ",") if grams >= 1 else f"{round(grams * 1000)} mg"


def fmt_h(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if not hours:
        return f"{mins} min"
    return f"{hours} h" + (f" {mins:02d} min" if mins else "")


# ---------- agendamento ----------
@dataclass
class Scheduled:
    ref: str
    refs: list[str]
    procedimento: str
    sitio: str | None
    lado: str | None
    consentimento: str | None
    template_id: str | None


def _is_surgical(sr: dict) -> bool:
    return any(c.get("code") == codes.SR_SURGICAL[0] for cat in sr.get("category", []) for c in cat.get("coding", []))


def scheduled_surgery(record: PatientRecord) -> Scheduled | None:
    """Cirurgia agendada do paciente: ServiceRequest cirúrgico (com bodySite) e/ou Appointment da sala."""
    requests = {
        sr["id"]: sr
        for sr in record.resources.get("ServiceRequest", [])
        if sr.get("status") not in ("revoked", "entered-in-error") and _is_surgical(sr)
    }
    appointments = sorted(
        (
            a
            for a in record.resources.get("Appointment", [])
            if a.get("status") not in ("cancelled", "noshow", "entered-in-error")
        ),
        key=lambda a: a.get("start") or "",
    )
    appointment = appointments[-1] if appointments else None
    chosen = None
    if appointment:
        based = [(b.get("reference") or "").split("/")[-1] for b in appointment.get("basedOn", [])]
        chosen = next((requests[i] for i in based if i in requests), None)
    if chosen is None and requests:
        open_ = [sr for sr in requests.values() if sr.get("status") != "completed"] or list(requests.values())
        chosen = max(open_, key=lambda sr: (sr.get("occurrencePeriod") or {}).get("start") or "")
    if chosen is None and appointment is None:
        return None
    refs: list[str] = []
    procedure, site = "", None
    if chosen:
        refs.append(f"ServiceRequest/{chosen['id']}")
        procedure = (chosen.get("code") or {}).get("text", "")
        site = ", ".join(b["text"] for b in chosen.get("bodySite", []) if b.get("text")) or None
    if appointment:
        refs.append(f"Appointment/{appointment['id']}")
        procedure = procedure or appointment.get("description", "")
    side = side_of(site or "") or side_of((appointment or {}).get("description", ""))
    consent = None
    for c in record.resources.get("Consent", []):
        linked = [(d.get("reference") or {}).get("reference") for d in (c.get("provision") or {}).get("data", [])]
        if c.get("status") == "active" and (not chosen or not linked or f"ServiceRequest/{chosen['id']}" in linked):
            consent = f"Consent/{c['id']}"
    detected = surgical_templates().detect(norm(f"{procedure} {site or ''}"))
    return Scheduled(
        ref=refs[0],
        refs=refs,
        procedimento=procedure,
        sitio=site,
        lado=side,
        consentimento=consent,
        template_id=detected[0].id if detected else None,
    )


# ---------- extração do ditado ----------
@dataclass
class Mention:
    clause: str  # normalizado, para as regras
    ref: str
    n: int
    raw: str = ""  # como foi dito, para mostrar

    @property
    def quote(self) -> str:
        return self.raw.rstrip(" .") or self.clause


def mentions(segments: list[Segment], words: tuple[str, ...]) -> list[Mention]:
    """Orações afirmativas (perguntas fora) que citam alguma das palavras."""
    return [
        Mention(c, fala_ref(s.n), s.n, raw)
        for s in segments
        for raw, c in clauses(affirmative(s.texto))
        if first_hit(c, words)
    ]


def sentence_mentions(segments: list[Segment], words: tuple[str, ...]) -> list[tuple[Mention, str]]:
    """Como `mentions`, com a frase inteira (normalizada) de cada oração: "time-out: lado e paciente confirmados"."""
    out = []
    for s in segments:
        for sentence in SENTENCE_SPLIT.split(affirmative(s.texto)):
            whole = norm(sentence)
            out += [(Mention(c, fala_ref(s.n), s.n, raw), whole) for raw, c in clauses(sentence) if first_hit(c, words)]
    return out


@dataclass
class DrugDose:
    key: str
    grams: float | None
    per_kg: float | None  # mg/kg, quando ditado assim
    clock: int | None
    ref: str
    redose: bool


def _drug_words() -> dict[str, tuple[str, ...]]:
    return {key: tuple(syn(w) for w in d.words) for key, d in knowledge.prophylaxis().items()}


def drug_mentions(segments: list[Segment]) -> list[DrugDose]:
    """Antibióticos dados: fora os negados ("cefazolina não foi feita"), as alergias e as perguntas."""
    words = _drug_words()
    every = tuple(w for ws in words.values() for w in ws)
    out: list[DrugDose] = []
    for s in segments:
        parts = [c for _, c in clauses(affirmative(s.texto))]
        for i, clause in enumerate(parts):
            for key in knowledge.prophylaxis():
                hit = first_hit(clause, words[key])
                if not hit:
                    continue
                if negated(clause, words[key]) or first_hit(clause[: clause.find(hit)], ALLERGY_WORDS):
                    continue
                m = DOSE_RE.search(clause[clause.find(hit) :])
                grams = per_kg = None
                if m:
                    value = _num(m.group(1))
                    in_mg = m.group(2).startswith("m")
                    if m.group(3):
                        per_kg = value if in_mg else value * 1000
                    else:
                        grams = value / 1000 if in_mg else value
                # o horário pode vir na oração seguinte ("cefazolina 2 g, às 8h"), desde que não seja de outro evento
                others = tuple(w for w in every if w not in words[key]) + INCISION_WORDS + END_WORDS
                when = clock_near(clause, hit, others)
                if when is None and i + 1 < len(parts) and not first_hit(parts[i + 1], others):
                    when = clock_in(parts[i + 1])
                out.append(DrugDose(key, grams, per_kg, when, fala_ref(s.n), bool(first_hit(clause, REDOSE_WORDS))))
    return out


def event_clock(
    segments: list[Segment], words: tuple[str, ...], others: tuple[str, ...] = ()
) -> tuple[int, str] | None:
    """Horário do evento (incisão, término): o dito junto da palavra, não o de outro evento na mesma oração."""
    stop = others + tuple(w for ws in _drug_words().values() for w in ws)
    for m in mentions(segments, words):
        when = clock_near(m.clause, first_hit(m.clause, words) or "", stop)
        if when is not None:
            return when, m.ref
    return None


def dictated_duration(segments: list[Segment]) -> tuple[int, str] | None:
    for m in mentions(segments, DURATION_WORDS):
        minutes = duration_in(m.clause)
        if minutes:
            return minutes, m.ref
    return None


def dictated_sides(segments: list[Segment], site_words: tuple[str, ...]) -> list[tuple[str, Mention]]:
    """(lado, trecho) de cada oração que cita o sítio do modelo e um lado."""
    out = []
    for s in segments:
        for raw, clause in clauses(affirmative(s.texto)):
            if site_words and not first_hit(clause, site_words):
                continue
            if is_history(clause) or side_negated(clause):
                continue  # "artroplastia do joelho esquerdo há 3 anos" é antecedente, não o lado operado agora
            side = side_of(clause)
            if side:
                out.append((side, Mention(clause, fala_ref(s.n), s.n, raw)))
    return out


def count_verdict(clause: str) -> str | None:
    """ "confere", "nao_confere" ou None (sem resultado) para uma oração da contagem, pelo sentido.

    "não confere", "não está batendo", "faltando uma compressa" → nao_confere; "confere", "correta", "nenhuma
    compressa faltando", "sem divergência" → confere. Na dúvida (as duas coisas), vale a divergência.
    """
    bad = first_hit(clause, COUNT_BAD)
    ok = first_hit(clause, COUNT_OK)
    if (bad and not negated(clause, COUNT_BAD)) or (ok and negated(clause, COUNT_OK)):
        return "nao_confere"
    if ok or bad:
        return "confere"
    return None


# ---------- verificações ----------
class Checks:
    def __init__(
        self,
        segments: list[Segment],
        tpl: ResolvedTemplate,
        chart: ChartView,
        scheduled: Scheduled | None,
        marks: dict | None,
        dictated_template: str | None,
        all_segments: list[Segment] | None = None,
    ):
        self.segments = segments
        self.all_segments = all_segments if all_segments is not None else segments  # o termo vale dito por quem for
        self.tpl = tpl
        self.chart = chart
        self.scheduled = scheduled
        self.marks = marks or {}
        self.dictated_template = dictated_template
        self.ts = surgical_templates()
        self.items: list[SafetyCheck] = []

    def add(self, chave: str, grupo: str, rotulo: str, status: str, texto: str, fonte: str, refs: list[str], **kw):
        item = SafetyCheck(
            chave=chave,
            grupo=grupo,
            rotulo=rotulo,
            status=status,
            texto=texto,
            fonte=self.ts.source(fonte),
            refs=list(dict.fromkeys(r for r in refs if r)),
            **kw,
        )
        self.items.append(item)
        return item

    def run(self) -> list[SafetyCheck]:
        self.who()
        self.consent()
        self.site()
        self.count()
        self.tourniquet()
        self.prophylaxis()
        return self.items

    # ----- Cirurgia Segura (OMS) -----
    def who(self) -> None:
        for step, (label, words, what) in WHO_STEPS.items():
            mark = self.marks.get(step) or {}
            # a última menção que decide: negada ("time-out ainda não foi feito") ou feita ("time-out realizado")
            said = sentence_mentions(self.segments, words)
            decided = [
                ("nao" if negated(m.clause, words) else "feito", m)
                for m, whole in said
                if negated(m.clause, words) or first_hit(whole, WHO_DONE)
            ]
            if mark.get("feito"):
                self.add(
                    step,
                    "cirurgia_segura",
                    label,
                    "ok",
                    f"Marcado na tela: {what}.",
                    SOURCE_OMS,
                    [],
                    marcado_por=mark.get("por"),
                    em=mark.get("em"),
                )
            elif decided and decided[-1][0] == "nao":
                m = decided[-1][1]
                text = f"Ditado como não realizado (“{m.quote}”): fazer a etapa antes de prosseguir."
                self.add(step, "cirurgia_segura", label, "critico", text, SOURCE_OMS, [m.ref])
            elif decided:
                text = f"Realizado por voz: {what}."
                self.add(step, "cirurgia_segura", label, "ok", text, SOURCE_OMS, [decided[-1][1].ref])
            elif said:
                m = said[-1][0]
                text = (
                    f"Citado sem confirmar a realização (“{m.quote}”): diga “{words[1]} realizado” ou marque na tela."
                )
                self.add(step, "cirurgia_segura", label, "pendente", text, SOURCE_OMS, [m.ref])
            else:
                text = f"Ainda não registrado: diga “{words[1]} realizado” ou marque na tela."
                self.add(step, "cirurgia_segura", label, "pendente", text, SOURCE_OMS, [])

    def consent(self) -> None:
        label = "Termo de consentimento"
        said = mentions(self.all_segments, CONSENT_WORDS)
        in_chart = self.scheduled.consentimento if self.scheduled else None
        refused = said and (
            negated(said[-1].clause, CONSENT_WORDS)
            or first_hit(said[-1].clause, tuple(syn(w) for w in CONSENT_REFUSED))
        )
        if refused:
            text = "Consentimento ditado como ausente ou recusado: cirurgia eletiva não começa sem o termo assinado."
            self.add("consentimento", "cirurgia_segura", label, "critico", text, SOURCE_MS, [said[-1].ref])
        elif in_chart or said:
            where = "no prontuário" if in_chart else "citado no ditado"
            refs = [in_chart or "", said[-1].ref if said else ""]
            self.add(
                "consentimento", "cirurgia_segura", label, "ok", f"Termo de consentimento {where}.", SOURCE_MS, refs
            )
        else:
            text = "Termo de consentimento não localizado no prontuário nem citado no ditado."
            self.add("consentimento", "cirurgia_segura", label, "atencao", text, SOURCE_MS, [])

    # ----- procedimento, sítio e lateralidade -----
    def site(self) -> None:
        sch = self.scheduled
        if sch and sch.template_id and self.dictated_template:
            label = "Procedimento × agendamento"
            if sch.template_id != self.dictated_template:
                dictated = self.ts.procedures[self.dictated_template].rotulo
                text = (
                    f"Procedimento ditado ({dictated}) difere do agendado ({sch.procedimento}): confirmar com a "
                    "equipe antes de prosseguir."
                )
                self.add("procedimento", "sitio", label, "critico", text, SOURCE_MS, sch.refs)
            else:
                self.add(
                    "procedimento",
                    "sitio",
                    label,
                    "ok",
                    f"Confere com o agendado: {sch.procedimento}.",
                    SOURCE_MS,
                    sch.refs,
                )
        if not self.tpl.lateral:
            return
        sides = dictated_sides(self.segments, self.tpl.sitio)
        found = list(dict.fromkeys(side for side, _ in sides))
        label = "Lateralidade × agendamento"
        site_txt = (sch.sitio if sch and sch.sitio else "sítio").lower()
        sch_refs = sch.refs if sch else []
        if len(found) > 1:
            text = f"Lateralidade inconsistente no ditado ({' e '.join(found)}): confirmar o lado com a equipe."
            self.add("lateralidade", "sitio", label, "critico", text, SOURCE_MS, [m.ref for _, m in sides] + sch_refs)
        elif found and sch and sch.lado and found[0] != sch.lado:
            m = sides[0][1]
            text = (
                f"Lateralidade divergente: ditado {found[0]} (“{m.quote}”) × agendado {site_txt}. Pare e confirme o"
                " sítio com a equipe e o paciente (marcação do sítio e termo de consentimento)."
            )
            self.add("lateralidade", "sitio", label, "critico", text, SOURCE_MS, [m.ref, *sch_refs])
        elif found and sch and sch.lado:
            text = f"Lado ditado ({found[0]}) confere com o agendado ({site_txt})."
            self.add("lateralidade", "sitio", label, "ok", text, SOURCE_MS, [sides[0][1].ref, *sch_refs])
        elif found:
            text = f"Lado ditado: {found[0]}. Sem agendamento com sítio no prontuário para conferir."
            self.add("lateralidade", "sitio", label, "info", text, SOURCE_MS, [sides[0][1].ref])
        else:
            expected = f" Agendado: {site_txt}." if sch and sch.sitio else ""
            self.add(
                "lateralidade", "sitio", label, "pendente", f"Lado ainda não ditado.{expected}", SOURCE_MS, sch_refs
            )

    # ----- contagem -----
    def _count_answers(self) -> list[Mention]:
        """Resposta à pergunta da contagem na fala seguinte ("Contagem de compressas?" → "Confere, dez de dez")."""
        out = []
        for i, s in enumerate(self.segments[:-1]):
            if any(first_hit(norm(q), COUNT_WORDS) for q in QUESTION.findall(s.texto)):
                nxt = self.segments[i + 1]
                first = clauses(affirmative(nxt.texto))[:1]
                out += [Mention(c, fala_ref(nxt.n), nxt.n, raw) for raw, c in first if count_verdict(c)]
        return out

    def count(self) -> None:
        label = "Contagem de compressas e instrumentais"
        said = sorted([*mentions(self.segments, COUNT_WORDS), *self._count_answers()], key=lambda m: m.n)
        if not said:
            self.add("contagem", "contagem", label, "pendente", "Contagem ainda não ditada.", SOURCE_RSI, [])
            return
        results = [(verdict, m) for m in said if (verdict := count_verdict(m.clause))]
        if not results:
            text = f"Contagem citada sem o resultado (“{said[-1].quote}”): ditar se confere ou não confere."
            self.add("contagem", "contagem", label, "atencao", text, SOURCE_RSI, [said[-1].ref])
            return
        last, m = results[-1]
        if last == "nao_confere":
            text = (
                f"Contagem NÃO confere (“{m.quote}”): não fechar a cavidade antes de localizar o item, considerar "
                "radiografia intraoperatória e registrar a conduta."
            )
            self.add("contagem", "contagem", label, "critico", text, SOURCE_RSI, [m.ref])
        else:
            earlier = next((x for status, x in results if status == "nao_confere"), None)
            text = f"Contagem confere (“{m.quote}”)."
            if earlier:
                text += f" A divergência da fala {earlier.n} foi resolvida."
            self.add("contagem", "contagem", label, "ok", text, SOURCE_RSI, [earlier.ref if earlier else "", m.ref])
        sign_out = next((i for i in self.items if i.chave == "sign_out"), None)
        if sign_out and sign_out.status == "ok" and last != "confere":
            sign_out.status = "atencao"
            sign_out.texto += " A contagem ainda não confere: o sign-out exige a contagem conferida."

    # ----- garrote -----
    def tourniquet(self) -> None:
        if not self.tpl.get("garrote"):
            return
        said = mentions(self.segments, TOURNIQUET_WORDS)
        found = next(((minutes, [m.ref]) for m in said if (minutes := duration_in(m.clause)) is not None), None)
        # sem a duração ditada: "garrote insuflado às 8h50" ... "desinsuflado às 11h10"
        clocks = [(m.ref, when) for m in said for _, when in clocks_in(m.clause)]
        if found is None and len(clocks) >= 2:
            found = ((clocks[-1][1] - clocks[0][1]) % DAY, [clocks[0][0], clocks[-1][0]])
        if found is None:
            return
        minutes, refs = found
        if minutes > TOURNIQUET_MAX_MIN:
            text = (
                f"Garrote por {fmt_h(minutes)} (acima de 2 h): avaliar reperfusão e examinar o membro no "
                "pós-operatório."
            )
            self.add("garrote", "intraoperatorio", "Tempo de garrote", "atencao", text, SOURCE_TOURNIQUET, refs)
        else:
            text = f"Garrote por {fmt_h(minutes)} (até 2 h)."
            self.add("garrote", "intraoperatorio", "Tempo de garrote", "ok", text, SOURCE_TOURNIQUET, refs)

    # ----- antibioticoprofilaxia -----
    def prophylaxis(self) -> None:
        table = knowledge.prophylaxis()
        doses = drug_mentions(self.segments)
        first = [d for d in doses if not d.redose]
        redoses = [d for d in doses if d.redose]
        given = list(dict.fromkeys(d.key for d in first))
        label = "Profilaxia: agente"
        if not given:
            if self.tpl.profilaxia:
                text = "Antibiótico profilático ainda não ditado (fármaco, dose e horário)."
                self.add("profilaxia_agente", "profilaxia", label, "pendente", text, SOURCE_ATB, [])
            return
        names = " + ".join(table[k].name for k in given)
        refs = [d.ref for d in first]
        if self.tpl.profilaxia:
            procedure = self.tpl.rotulo.lower()
            if any(set(option) <= set(given) for option in self.tpl.profilaxia):
                text = f"{names}: recomendado para {procedure}."
                self.add("profilaxia_agente", "profilaxia", label, "ok", text, SOURCE_ATB, refs)
            else:
                options = " ou ".join(" + ".join(table[k].name for k in opt) for opt in self.tpl.profilaxia)
                text = (
                    f"Para {procedure}, a recomendação é {options}; ditado {names}. Confirmar a escolha (ex.: alergia)."
                )
                self.add("profilaxia_agente", "profilaxia", label, "info", text, SOURCE_ATB, refs)
        main = first[0]
        drug = table[main.key]
        incision = event_clock(self.segments, INCISION_WORDS, END_WORDS)
        self._window(drug, main, incision)
        for d in first:
            self._dose(table[d.key], d)
        self._redose(drug, main, incision, redoses)

    def _window(self, drug: ProphylaxisDef, main: DrugDose, incision: tuple[int, str] | None) -> None:
        label = "Profilaxia: janela antes da incisão"
        if main.clock is None or incision is None:
            missing = [x for x, ok in (("do antibiótico", main.clock is not None), ("da incisão", incision)) if not ok]
            text = f"Horário {' e '.join(missing)} não ditado: sem como conferir a janela de {drug.window_min} min."
            refs = [main.ref, incision[1] if incision else ""]
            self.add("profilaxia_janela", "profilaxia", label, "pendente", text, SOURCE_ATB, refs)
            return
        delta = incision[0] - main.clock
        if delta < -12 * 60:
            delta += DAY  # passou da meia-noite
        refs = [main.ref, incision[1]]
        when = f"{drug.name} às {hhmm(main.clock)}, incisão às {hhmm(incision[0])}"
        if delta < 0:
            text = f"{when}: antibiótico depois da incisão; a dose deve terminar antes de incisar."
            self.add("profilaxia_janela", "profilaxia", label, "atencao", text, SOURCE_ATB, refs)
        elif delta > drug.window_min:
            text = (
                f"{when}: {delta} min antes da incisão, acima da janela de {drug.window_min} min. Eficácia reduzida:"
                " considerar nova dose antes de incisar e registrar."
            )
            self.add("profilaxia_janela", "profilaxia", label, "atencao", text, SOURCE_ATB, refs)
        else:
            text = f"{when}: {delta} min antes da incisão (janela de até {drug.window_min} min)."
            self.add("profilaxia_janela", "profilaxia", label, "ok", text, SOURCE_ATB, refs)

    def _dose(self, drug: ProphylaxisDef, d: DrugDose) -> None:
        key, label = f"profilaxia_dose_{d.key}", f"Profilaxia: dose de {drug.name.lower()}"
        weight = self.chart.weight()
        refs = [d.ref, weight[1] if weight else ""]
        if d.grams is None and not (d.per_kg and weight):
            text = f"{drug.name} citado sem dose" + (" (ou dose por kg sem peso no prontuário)." if d.per_kg else ".")
            self.add(key, "profilaxia", label, "pendente", text, SOURCE_ATB, refs)
            return
        given = d.grams if d.grams is not None else d.per_kg * weight[0] / 1000
        age = self.chart.r.age
        if age is not None and age < ADULT_AGE and drug.dose_mg_kg_ped:
            self._pediatric_dose(drug, key, label, given, age, weight, refs)
            return
        if drug.dose_mg_kg:
            if weight is None:
                text = f"{drug.name} é dosado por peso ({drug.dose_mg_kg:g} mg/kg) e o prontuário não tem peso."
                self.add(key, "profilaxia", label, "pendente", text, SOURCE_ATB, refs)
                return
            expected = drug.dose_mg_kg * weight[0] / 1000
            text = f"{drug.name} {fmt_g(given)} para {weight[0]:g} kg; esperado ~{fmt_g(expected)} ({drug.note})."
            if 0.8 * expected <= given <= 1.2 * expected:
                self.add(key, "profilaxia", label, "ok", text, SOURCE_ATB, refs)
            else:
                self.add(key, "profilaxia", label, "atencao", f"Dose fora do esperado: {text}", SOURCE_ATB, refs)
            return
        heavy = bool(weight and drug.heavy_kg and drug.dose_g_heavy and weight[0] >= drug.heavy_kg)
        expected = drug.dose_g_heavy if heavy else drug.dose_g
        if expected is None:
            return
        if heavy and given < expected - 1e-6:
            text = (
                f"Subdose provável: {drug.name} {fmt_g(given)} para paciente de {weight[0]:g} kg; recomendado "
                f"{fmt_g(expected)} a partir de {drug.heavy_kg:g} kg ({drug.note})."
            )
            self.add(key, "profilaxia", label, "atencao", text, SOURCE_ATB, refs)
        elif given < expected - 1e-6:
            text = f"{drug.name} {fmt_g(given)} abaixo da dose recomendada de {fmt_g(expected)} ({drug.note})."
            self.add(key, "profilaxia", label, "atencao", text, SOURCE_ATB, refs)
        elif given > 1.5 * expected:
            text = f"{drug.name} {fmt_g(given)} acima da dose usual de {fmt_g(expected)}: conferir."
            self.add(key, "profilaxia", label, "atencao", text, SOURCE_ATB, refs)
        elif weight is None and drug.heavy_kg:
            text = (
                f"{drug.name} {fmt_g(given)}: sem peso no prontuário para conferir (a dose sobe a partir de "
                f"{drug.heavy_kg:g} kg)."
            )
            self.add(key, "profilaxia", label, "pendente", text, SOURCE_ATB, refs)
        else:
            weight_txt = f" para {weight[0]:g} kg" if weight else ""
            text = f"{drug.name} {fmt_g(given)}{weight_txt}: dose recomendada ({drug.note})."
            self.add(key, "profilaxia", label, "ok", text, SOURCE_ATB, refs)

    def _pediatric_dose(
        self,
        drug: ProphylaxisDef,
        key: str,
        label: str,
        given: float,
        age: int,
        weight: tuple[float, str] | None,
        refs: list[str],
    ) -> None:
        """Criança e adolescente: mg/kg da tabela pediátrica, sem passar da dose de adulto."""
        per_kg = drug.dose_mg_kg_ped or 0
        if weight is None:
            text = f"Paciente de {age} anos sem peso no prontuário: {drug.name} em pediatria é {per_kg:g} mg/kg."
            self.add(key, "profilaxia", label, "pendente", text, SOURCE_ATB, refs)
            return
        adult = drug.dose_g or (drug.dose_mg_kg * weight[0] / 1000 if drug.dose_mg_kg else None)
        expected = per_kg * weight[0] / 1000
        expected = min(expected, adult) if adult else expected
        text = (
            f"{drug.name} {fmt_g(given)} para {weight[0]:g} kg ({age} anos); esperado ~{fmt_g(expected)} "
            f"({per_kg:g} mg/kg em pediatria, sem passar da dose de adulto)."
        )
        if given < 0.8 * expected - 1e-6:
            self.add(key, "profilaxia", label, "atencao", f"Subdose provável: {text}", SOURCE_ATB, refs)
        elif (adult and given > adult + 1e-6) or given > 1.5 * expected + 1e-6:
            self.add(key, "profilaxia", label, "atencao", f"Dose acima do esperado: {text}", SOURCE_ATB, refs)
        else:
            self.add(key, "profilaxia", label, "ok", text, SOURCE_ATB, refs)

    def _redose(
        self, drug: ProphylaxisDef, main: DrugDose, incision: tuple[int, str] | None, redoses: list[DrugDose]
    ) -> None:
        label = "Profilaxia: redose"
        end = event_clock(self.segments, END_WORDS, INCISION_WORDS)
        duration = dictated_duration(self.segments)
        refs = [main.ref]
        elapsed: int | None = None  # minutos desde a dose pré-operatória
        if main.clock is not None and end is not None:
            elapsed = (end[0] - main.clock) % DAY
            refs.append(end[1])
        elif duration is not None:
            lead = incision[0] - main.clock if incision and main.clock is not None else 0
            lead = lead + DAY if lead < -12 * 60 else lead
            elapsed = duration[0] + max(0, lead)
            refs.append(duration[1])
        elif incision is not None and end is not None:
            elapsed = (end[0] - incision[0]) % DAY
            refs += [incision[1], end[1]]
        blood = None
        for m in mentions(self.segments, ("sangramento", "perda sanguinea")):
            v, liters = VOLUME_RE.search(m.clause), VOLUME_L_RE.search(m.clause)
            if v:
                blood = (int(v.group(1).replace(".", "")), m.ref)
            elif liters:
                blood = (round(_num(liters.group(1)) * 1000), m.ref)  # "sangramento de 2 litros"
        if redoses:
            text = f"Redose de {drug.name} registrada."
            self.add("profilaxia_redose", "profilaxia", label, "ok", text, SOURCE_ATB, [*refs, redoses[-1].ref])
        elif blood and blood[0] > BLOOD_LOSS_REDOSE_ML:
            text = f"Perda sanguínea de {blood[0]} mL (acima de {BLOOD_LOSS_REDOSE_ML} mL) sem redose de {drug.name}."
            self.add("profilaxia_redose", "profilaxia", label, "atencao", text, SOURCE_ATB, [*refs, blood[1]])
        elif drug.redose_h is None:
            text = f"{drug.name}: sem redose prevista ({drug.note})."
            self.add("profilaxia_redose", "profilaxia", label, "ok", text, SOURCE_ATB, [main.ref])
        elif elapsed is None:
            text = f"Duração não ditada: {drug.name} pede redose a cada {drug.redose_h:g} h."
            self.add("profilaxia_redose", "profilaxia", label, "pendente", text, SOURCE_ATB, [main.ref])
        elif elapsed > round(drug.redose_h * 60):
            text = (
                f"Cirurgia de {fmt_h(elapsed)} desde a dose de {drug.name} sem redose: a redose é a cada "
                f"{drug.redose_h:g} h. Registrar a redose ou o motivo."
            )
            self.add("profilaxia_redose", "profilaxia", label, "atencao", text, SOURCE_ATB, refs)
        else:
            text = f"{fmt_h(elapsed)} desde a dose: redose de {drug.name} só após {drug.redose_h:g} h."
            self.add("profilaxia_redose", "profilaxia", label, "ok", text, SOURCE_ATB, refs)


HIGHLIGHT_OF = {"critico": "critico", "atencao": "atencao", "info": "info"}


def run_checks(
    segments: list[Segment],
    tpl: ResolvedTemplate,
    chart: ChartView,
    scheduled: Scheduled | None,
    marks: dict | None,
    dictated_template: str | None,
    all_segments: list[Segment] | None = None,
) -> tuple[list[SafetyCheck], list[Highlight]]:
    """Verificações + as pendências que elas geram (crítico, atenção ou informação), com as mesmas fontes."""
    items = Checks(segments, tpl, chart, scheduled, marks, dictated_template, all_segments).run()
    highlights = [
        Highlight(texto=f"{i.rotulo}: {i.texto}", severidade=HIGHLIGHT_OF[i.status], refs=i.refs)
        for i in items
        if i.status in HIGHLIGHT_OF
    ]
    return items, highlights
