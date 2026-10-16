"""Semiologia por queixa: início/duração, localização, intensidade, piora, melhora, frequência e sintomas associados.

Para cada queixa presente o extrator lê, por regex, a fala da queixa e as 2 falas seguintes do paciente (ou do
acompanhante) e devolve cada campo como {valor, refs}. O que a anamnese ainda não sabe vira pergunta sugerida
("semiologia:<campo>", em consult_questions.csv), e a resposta do paciente à pergunta do profissional também alimenta o
campo. Quais campos valem para cada tipo de queixa, o vocabulário de piora e melhora e os campos extras por
especialidade ficam em data/reference/consult_semiology.yaml.

Também redige a HDA no modo regras ("Tontura há 3 semanas, piora ao levantar, … (falas 2, 4, 5)") e o detalhe das
tags ("3 sem · ao levantar"), sem colar citações.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.consult import knowledge
from app.consult.knowledge import SemiologyDef, SymptomDef
from app.consult.schemas import CampoEspecialidade, CampoSemio, QueixaDetalhada, Segment, fala_ref
from app.consult.text import clauses, contains, find, keyword_status, mentioned_drugs, norm

FIELD_ORDER = ("inicio_duracao", "localizacao", "intensidade", "piora", "melhora", "frequencia", "associados")
FOLLOWING = 2  # falas seguintes do paciente lidas depois da fala da queixa

NUMBERS = {
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
    "quinze": 15,
    "vinte": 20,
    "trinta": 30,
}
NUM = r"(\d{1,3}|" + "|".join(NUMBERS) + r")"
NUM_NC = r"(?:\d{1,3}|" + "|".join(NUMBERS) + r")"
UNIT = r"(minutos?|horas?|dias?|semanas?|mes|meses|anos?)"
UNIT_LABEL = {
    "minuto": ("minuto", "minutos"),
    "hora": ("hora", "horas"),
    "dia": ("dia", "dias"),
    "semana": ("semana", "semanas"),
    "mes": ("mês", "meses"),
    "meses": ("mês", "meses"),
    "ano": ("ano", "anos"),
}
UNIT_SHORT = {"min": "min", "hor": "h", "dia": "d", "sem": "sem", "mes": "m", "mês": "m", "ano": "a"}
# restaura a acentuação de substantivos lidos da fala já normalizada (sem acento)
ACCENT = {
    "braco": "braço",
    "bracos": "braços",
    "pe": "pé",
    "pes": "pés",
    "cabeca": "cabeça",
    "mao": "mão",
    "maos": "mãos",
    "coracao": "coração",
    "estomago": "estômago",
    "abdomen": "abdome",
}

# ---------- início e duração ----------
DURATION = re.compile(
    r"(?<!\w)(?P<lead>(?:ha|faz|fazem|desde|ja faz|ultimos?|ultimas?|durante|por) )?"
    r"(?:(?:cerca de|mais ou menos|uns|umas|quase|mais de|aproximadamente) )*(?P<n>"
    + NUM_NC
    + ") (?P<u>"
    + UNIT
    + r")(?: atras)?(?!\w)"
)
DURATION_NOT_AFTER = {"cada", "ate", "em", "depois", "antes", "apos", "dentro", "tomo", "tomei"}
DURATION_NOT_BEFORE = re.compile(r"^ (de|do|da) (tratamento|uso|internacao|repouso|licenca|atestado|sono)")
SINCE = re.compile(
    r"(?<!\w)(?:desde|comecou|comecei|iniciou|iniciei|apareceu|surgiu)(?: (?:a|o|na|no|de))? "
    r"(hoje cedo|hoje de manha|hoje|ontem a noite|ontem de manha|ontem a tarde|ontem|anteontem|semana passada"
    r"|mes passado|ano passado|segunda|terca|quarta|quinta|sexta|sabado|domingo)(?!\w)"
)
SINCE_LABEL = {
    "semana passada": "a semana passada",
    "mes passado": "o mês passado",
    "ano passado": "o ano passado",
    "segunda": "segunda",
    "terca": "terça",
    "sabado": "sábado",
}
SUDDEN = re.compile(
    r"(?<!\w)(de repente|subitamente|do nada|de uma hora pra outra|de uma hora para outra|bruscamente)(?!\w)"
)

# ---------- intensidade ----------
INTENSITY_NUMERIC = (
    re.compile(rf"(?<!\w){NUM} (?:de|em|numa escala de|na escala de) (?:0|zero) (?:a|ate) (?:10|dez)(?!\w)"),
    re.compile(rf"(?<!\w)nota (?:de )?{NUM}(?!\w)"),
    re.compile(r"(?<!\w)(\d{1,2}) ?/ ?10(?!\w)"),
    re.compile(rf"(?<!\w){NUM} de 10(?!\w)"),
    re.compile(
        rf"(?<!\w)(?:dor|intensidade|forca|nivel) (?:de |e de |ta |esta |em )?{NUM}(?!\w)"
        r"(?! ?(?:dias?|semanas?|meses|mes|anos?|vezes|horas?|minutos?))"
    ),
)
BARE_NUMBER = re.compile(
    r"(?<!\w)(\d{1,3}|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez)(?!\w)"
    r"(?! ?(?:vezes|vez|dias?|semanas?|horas?|anos?|meses|mes|minutos?|%|kg|mg))"
)
INTENSITY_WORDS = (
    (
        "forte",
        ("insuportavel", "fortissima", "pior dor", "muito forte", "bem forte", "bastante forte", "forte", "intensa"),
    ),
    ("forte", ("doi muito", "doem muito", "doendo muito", "dor muito", "muita dor")),
    ("fraca", ("fraquinha", "fraca", "levinha", "leve", "suportavel", "pouca dor", "pouquinho")),
    ("moderada", ("moderada", "mais ou menos forte")),
)

# ---------- frequência ----------
FREQ_TIMES = re.compile(
    rf"(?<!\w){NUM}(?: (?:ou|a) {NUM})? (vezes|vez)"
    r"(?: (?:por|ao|na|no|a|em|de|nesta|nessa|esta|essa) (dia|semana|mes|noite|manha|tarde|hora|ano))?(?!\w)"
)
FREQ_PERIOD = {
    "dia": "por dia",
    "semana": "por semana",
    "mes": "por mês",
    "hora": "por hora",
    "ano": "por ano",
    "noite": "à noite",
    "manha": "de manhã",
    "tarde": "à tarde",
}
FREQ_WORDS = (
    ("todos os dias", ("todo dia", "todos os dias", "diariamente")),
    ("todas as noites", ("toda noite", "todas as noites")),
    ("toda semana", ("toda semana", "todas as semanas")),
    ("constante", ("o tempo todo", "o dia todo", "sem parar", "constante", "direto", "toda hora")),
    ("às vezes", ("as vezes", "de vez em quando", "de vez em qdo", "esporadic", "ocasional")),
    ("raramente", ("raramente", "raro")),
    ("1 vez", ("uma unica vez", "so uma vez", "so uma")),
)

LATERAL = re.compile(r"(?<!\w)([a-z]+) (direito|direita|esquerdo|esquerda)(?!\w)")
LATERAL_SKIP = {"o", "a", "os", "as", "do", "da", "no", "na", "e", "ou", "um", "uma", "meu", "minha", "lado"}


def _accent(text: str) -> str:
    return " ".join(ACCENT.get(w, w) for w in text.split())


def _number(token: str) -> int:
    return int(token) if token.isdigit() else NUMBERS[token]


def _unit(unit: str, n: int) -> str:
    singular, plural = UNIT_LABEL[unit.rstrip("s") if unit not in UNIT_LABEL else unit]
    return singular if n == 1 else plural


def _plural_times(n: int) -> str:
    return "vez" if n == 1 else "vezes"


# ---------- extratores de campo (texto já normalizado, uma oração ou fala curta) ----------
def duration(text: str) -> str | None:
    for m in DURATION.finditer(text):
        before = text[: m.start()].split()[-1:]
        if before and before[0] in DURATION_NOT_AFTER:
            continue
        if DURATION_NOT_BEFORE.match(text[m.end() :]):
            continue
        n = _number(m.group("n"))
        if n == 1 and not m.group("lead"):  # "um dia eu caí" não é duração
            continue
        return f"{n} {_unit(m.group('u'), n)}"
    m = SINCE.search(text)
    if m:
        when = m.group(1)
        return "desde " + SINCE_LABEL.get(when, when)
    return "início súbito" if SUDDEN.search(text) else None


def intensity(text: str, answer: bool = False) -> str | None:
    """'7/10', 'forte', 'fraca' ou 'moderada'. `answer`: resposta à pergunta "de 0 a 10", em que o número solto vale."""
    for rx in INTENSITY_NUMERIC:
        for m in rx.finditer(text):
            n = _number(m.group(1))
            if 0 <= n <= 10:
                return f"{n}/10"
    if answer and len(text.split()) <= 8:
        for m in BARE_NUMBER.finditer(text):
            n = _number(m.group(1))
            if 0 <= n <= 10:
                return f"{n}/10"
    for label, words in INTENSITY_WORDS:
        if contains(text, words):
            return label
    return None


def frequency(text: str) -> str | None:
    m = FREQ_TIMES.search(text)
    if m:
        n1 = _number(m.group(1))
        n2 = _number(m.group(2)) if m.group(2) else None
        times = f"{n1} a {n2}" if n2 else f"{n1}"
        out = f"{times} {_plural_times(n2 or n1)}"
        period = FREQ_PERIOD.get(m.group(4) or "", "")
        if m.group(4) in ("semana", "mes") and re.search(r"(nesta|nessa|esta|essa)", m.group(0)):
            period = "na semana" if m.group(4) == "semana" else "no mês"
        return f"{out} {period}".strip()
    for label, words in FREQ_WORDS:
        if contains(text, words):
            return label
    return None


def _cue_parts(clause: str, sem: SemiologyDef) -> list[tuple[str, str]]:
    """Divide a oração nas pistas de piora/melhora: [(tipo, trecho)], tipo 'piora', 'melhora' ou 'neutro'."""
    marks: list[tuple[int, str]] = []
    for kind, cues in (("piora", sem.worsening_cues), ("melhora", sem.relief_cues)):
        for cue in cues:
            m = re.search(r"(?<![a-z])" + re.escape(cue), clause)
            if m:
                marks.append((m.start(), kind))
    if not marks:
        return [("neutro", clause)]
    marks.sort()
    first = marks[0][1]
    other = next(((p, k) for p, k in marks if k != first), None)
    if other is None:
        return [(first, clause)]
    return [(first, clause[: other[0]]), (other[1], clause[other[0] :])]


def _vocab(text: str, entries, with_cue: bool) -> list[str]:
    return [e.value for e in entries if (with_cue or not e.needs_cue) and contains(text, e.words)]


def worsening_relief(clause: str, sem: SemiologyDef, only: str | None = None) -> tuple[list[str], list[str]]:
    """(fatores de piora, fatores de melhora) da oração. `only`: resposta a uma pergunta de piora/melhora, em que o
    vocabulário vale sem pista."""
    if only == "piora":
        return _vocab(clause, sem.worsening, True), []
    if only == "melhora":
        return [], _vocab(clause, sem.relief, True)
    worse: list[str] = []
    relief: list[str] = []
    for kind, part in _cue_parts(clause, sem):
        if kind == "melhora":
            relief += _vocab(part, sem.relief, True)
        else:
            worse += _vocab(part, sem.worsening, kind == "piora")
    return worse, relief


def location(text: str, sem: SemiologyDef, implied: str = "") -> str | None:
    """Lateralidade ("joelho direito") e região do corpo da fala. `implied`: as palavras da própria queixa, em que a
    região já está dita ("dor no peito" não precisa de "no peito")."""
    found = []
    m = LATERAL.search(text)
    if m and m.group(1) not in LATERAL_SKIP:
        found.append(_accent(f"{m.group(1)} {m.group(2)}"))
    elif m and m.group(1) == "lado":
        found.append(f"lado {m.group(2)}")
    for entry in sem.location:
        if any(w.strip() in implied for w in entry.words):
            continue
        if contains(text, entry.words) and entry.value not in found and len(found) < 2:
            found.append(entry.value)
    return "; ".join(found) or None


# ---------- extração por queixa ----------
@dataclass
class _Acc:
    """Campos de uma queixa enquanto o extrator lê as falas."""

    values: dict[str, list[tuple[str, int]]] = field(default_factory=dict)  # campo -> [(valor, n da fala)]
    answers: dict[str, list[tuple[str, int]]] = field(default_factory=dict)  # os que respondem a uma pergunta

    def add(self, name: str, value: str | None, n: int, answer: bool = False) -> None:
        bucket = (self.answers if answer else self.values).setdefault(name, [])
        if value and (value, n) not in bucket:
            bucket.append((value, n))

    def get(self, name: str, many: bool = False) -> CampoSemio:
        # a resposta direta a uma pergunta do profissional vale mais que o que o paciente disse por conta própria
        items = self.answers.get(name, []) + self.values.get(name, [])
        if not items:
            return CampoSemio()
        if name == "intensidade":  # um número vale mais que um adjetivo
            numeric = [i for i in items if "/10" in i[0]]
            items = [numeric[-1]] if numeric else items[:1]
        elif many:
            seen: list[str] = []
            for v, _ in items:
                if v not in seen:
                    seen.append(v)
            items = [(v, next(n for x, n in items if x == v)) for v in seen[:3]]
            return CampoSemio(
                valor=" e ".join(v for v, _ in items) if name != "localizacao" else "; ".join(v for v, _ in items),
                refs=[fala_ref(n) for n in sorted({n for _, n in items})],
            )
        else:
            items = items[:1]
        return CampoSemio(valor=items[0][0], refs=[fala_ref(items[0][1])])


def _eligible(d: SymptomDef, age: int | None, sex: str) -> bool:
    return knowledge.fits(d.age_min, d.age_max, d.sex, age, sex)


def _ref_n(ref: str) -> int:
    return int(ref.split("/")[1])


def _sentences(text: str) -> list[str]:
    """Frases de uma fala, sem as que falam de medicamento (a duração e a frequência delas são do remédio)."""
    return [s for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip() and not mentioned_drugs(s)]


@dataclass
class Asked:
    """Pergunta de semiologia que o profissional fez: o campo, a queixa, a fala dele e a resposta do paciente."""

    campo: str
    chave: str
    pergunta_n: int
    resposta_n: int | None


@dataclass
class Semiology:
    queixas: list[QueixaDetalhada]
    perguntas: list[Asked]


def extract(
    segments: list[Segment],
    present: dict[str, list[str]],
    denied: dict[str, list[str]],
    *,
    age: int | None,
    sex: str,
    patient_side: tuple[str, ...],
    team_side: tuple[str, ...],
) -> Semiology:
    """Uma entrada por queixa presente (as variantes, como "tontura ao levantar", agrupam na principal)."""
    sem = knowledge.semiology()
    defs = [d for d in knowledge.symptoms() if _eligible(d, age, sex)]
    by_key = {d.key: d for d in defs}
    principal = {d.key: d.group or d.key for d in defs}
    members: dict[str, set[str]] = {}
    for k, p in principal.items():
        members.setdefault(p, set()).add(k)
    anchors: dict[str, list[int]] = {}
    for k, refs in present.items():
        if k in principal:
            anchors.setdefault(principal[k], [])
            anchors[principal[k]] += [_ref_n(r) for r in refs]
    anchors = {p: sorted(set(ns)) for p, ns in anchors.items()}
    if not anchors:
        return Semiology([], [])

    seg_norm = {s.n: norm(s.texto) for s in segments}
    by_n = {s.n: s for s in segments}
    patient_ns = [s.n for s in segments if s.falante in patient_side]

    def following(n: int) -> list[int]:
        return [m for m in patient_ns if m > n][:FOLLOWING]

    def mentions_other(text: str, own: str) -> bool:
        return any(contains(text, d.words) for d in defs if principal[d.key] != own)

    def chunk_of(clause: str, own: set[str]) -> list[str]:
        """O que a oração diz desta queixa: de cada nome de queixa até o próximo ("febre e tosse há três dias e falta
        de ar quando ando": o "quando ando" é da falta de ar). Queixa só nomeada e ligada à seguinte por "e"
        ("febre e tosse há três dias") divide o que vem depois. Oração sem outra queixa vale inteira."""
        marks = []
        for d in defs:
            hit = find(clause, d.words)
            if hit and not (d.excludes and find(clause, d.excludes)):
                marks.append((hit[1], hit[2], d.key in own))
        marks.sort()
        if not marks or all(mine for *_, mine in marks):
            return [clause]
        if not any(mine for *_, mine in marks):
            return []
        starts = [m[0] for m in marks] + [len(clause)]
        starts[0] = 0  # o que vem antes do 1º nome de queixa é dessa queixa
        out = []
        for i, (_, end, mine) in enumerate(marks):
            if not mine:
                continue
            text = clause[starts[i] : starts[i + 1]]
            j = i
            while clause[end : starts[j + 1]].strip() in ("e", "ou", "") and j + 1 < len(marks):
                j += 1  # só o nome e a conjunção: herda o que vem depois, como "há três dias"
                end = marks[j][1]
                text += " " + clause[starts[j] : starts[j + 1]]
            out.append(text)
        return out

    implied = {p: " ".join(w for k in members[p] for w in by_key[k].words) for p in anchors}
    accs = {p: _Acc() for p in anchors}
    window: dict[str, set[int]] = {p: set() for p in anchors}
    group_fields = {p: sem.group_of(p).fields for p in anchors}

    def read(p: str, text: str, n: int, only: str | None = None, answer: bool = False) -> None:
        """Lê os campos que valem para a queixa `p` neste trecho (uma oração, ou a fala toda na resposta)."""
        acc, fields = accs[p], group_fields[p]
        for name in (only,) if only else tuple(fields):
            if name == "inicio_duracao":
                acc.add(name, duration(text), n, answer)
            elif name == "intensidade":
                acc.add(name, intensity(text, answer), n, answer)
            elif name == "frequencia":
                acc.add(name, frequency(text), n, answer)
            elif name == "localizacao":
                acc.add(name, location(text, sem, implied[p]), n, answer)
            elif name in ("piora", "melhora"):
                worse, relief = worsening_relief(text, sem, only)
                for v in worse if name == "piora" else relief:
                    acc.add(name, v, n, answer)

    # a fala da queixa (só as orações dela) e as 2 falas seguintes do paciente (as que não são de outra queixa)
    for p, ns in anchors.items():
        own = members[p]

        def usable(sentence: str, p: str = p, own: set[str] = own) -> list[str]:
            """Orações da frase que falam desta queixa ou de nada em particular (não de outra queixa)."""
            text = norm(sentence)
            has_own = any(contains(text, by_key[k].words) for k in own)
            has_other = mentions_other(text, p)
            if has_other and not has_own:
                return []
            if has_own and has_other:
                return [t for c in clauses(sentence) for t in chunk_of(c, own)]
            return clauses(sentence)

        for n in ns:
            window[p].add(n)
            for m in (n, *following(n)):
                window[p].add(m)
                before = by_n.get(m - 1)
                if m != n and before is not None and before.falante in team_side:
                    # resposta a pergunta do profissional sobre OUTRA queixa não é desta
                    asked_text = norm(before.texto)
                    if mentions_other(asked_text, p) and not any(contains(asked_text, by_key[k].words) for k in own):
                        continue
                for sentence in _sentences(by_n[m].texto):
                    for piece in usable(sentence):
                        read(p, piece, m)

    # resposta do paciente à pergunta do profissional na fala anterior ("de 0 a 10?" → "uns sete")
    asked = {
        q.trigger.split(":", 1)[1]: q.asked_words for q in knowledge.questions() if q.trigger.startswith("semiologia:")
    }
    asks: list[Asked] = []
    for s in segments:
        if s.falante not in team_side:
            continue
        fields_asked = [name for name, words in asked.items() if name in sem.fields and contains(seg_norm[s.n], words)]
        if not fields_asked:
            continue
        reply = s.n + 1 if s.n + 1 in patient_ns else None
        mentioned = [p for p in anchors if any(contains(seg_norm[s.n], by_key[k].words) for k in members[p])]
        focus = mentioned[0] if mentioned else None
        if focus is None:
            recent = [(min(ns), p) for p, ns in anchors.items() if min(ns) < s.n]
            focus = max(recent)[1] if recent else None
        if focus is None:
            continue
        for name in fields_asked:
            if name in group_fields[focus]:
                asks.append(Asked(name, focus, s.n, reply))
                if reply:
                    pieces = [c for sentence in _sentences(by_n[reply].texto) for c in clauses(sentence)]
                    for piece in pieces or [seg_norm[reply]]:
                        read(focus, piece, reply, only=name, answer=True)

    out: list[QueixaDetalhada] = []
    for p, ns in anchors.items():
        acc, fields = accs[p], group_fields[p]
        # sintomas associados: outras queixas presentes (ou negadas) na mesma janela de falas
        others = [(q, [n for n in qn if n in window[p]]) for q, qn in anchors.items() if q != p and set(qn) & window[p]]
        associated = CampoSemio(
            valor=join_pt([by_key[q].label.lower() for q, _ in others]),
            refs=[fala_ref(n) for n in sorted({n for _, qn in others for n in qn})],
        )
        negated = [(k, [_ref_n(r) for r in refs]) for k, refs in denied.items() if k in principal]
        negated = [(k, [n for n in ns_ if n in window[p]]) for k, ns_ in negated if principal[k] != p]
        negated = [(k, ns_) for k, ns_ in negated if ns_]
        denial = CampoSemio(
            valor=join_pt([by_key[k].label.lower() for k, _ in negated]),
            refs=[fala_ref(n) for n in sorted({n for _, ns_ in negated for n in ns_})],
        )
        detail = QueixaDetalhada(
            chave=p,
            rotulo=by_key[p].label,
            inicio_duracao=acc.get("inicio_duracao"),
            localizacao=acc.get("localizacao", many=True),
            intensidade=acc.get("intensidade"),
            piora=acc.get("piora", many=True),
            melhora=acc.get("melhora", many=True),
            frequencia=acc.get("frequencia"),
            associados=associated,
            nega=denial,
            refs=[],
        )
        if "associados" not in fields:
            detail.associados = CampoSemio()
        shown = [getattr(detail, f) for f in (*FIELD_ORDER, "nega")]
        detail.refs = [fala_ref(n) for n in sorted({*ns, *(_ref_n(r) for c in shown for r in c.refs)})]
        detail.faltam = [f for f in fields if not getattr(detail, f).valor]
        out.append(detail)
    out.sort(key=lambda d: _ref_n(d.refs[0]))
    return Semiology(out, asks)


def specialty_fields(
    segments: list[Segment], specialty: str, age: int | None, patient_side: tuple[str, ...]
) -> list[CampoEspecialidade]:
    """Campos extras da especialidade (pediatria: gestação e parto, vacinas, DNPM), lidos da fala do paciente."""
    sem = knowledge.semiology()
    max_age = sem.specialty_max_age.get(specialty)
    if age is not None and max_age is not None and age > max_age:
        return []
    out = []
    for spec in sem.specialty_fields.get(specialty, ()):
        values: list[str] = []
        refs: list[str] = []
        for s in segments:
            if s.falante not in patient_side:
                continue
            for entry in spec.entries:
                if entry.value not in values and keyword_status(s.texto, entry.words) == "presente":
                    values.append(entry.value)
                    refs.append(fala_ref(s.n))
        out.append(
            CampoEspecialidade(
                campo=spec.key, rotulo=spec.label, valor="; ".join(values), refs=list(dict.fromkeys(refs))
            )
        )
    return out


# ---------- texto ----------
def join_pt(items: list[str]) -> str:
    """'a, b e c'."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} e {items[-1]}" if items else ""


def falas_text(refs: list[str]) -> str:
    ns = sorted({_ref_n(r) for r in refs})
    if not ns:
        return ""
    return f" (fala {ns[0]})" if len(ns) == 1 else f" (falas {', '.join(map(str, ns))})"


def _duration_phrase(valor: str) -> str:
    return f"há {valor}" if valor[:1].isdigit() else valor


def hda_sentence(q: QueixaDetalhada, main: bool = True) -> str:
    parts = [q.rotulo]
    if q.inicio_duracao.valor:
        parts[0] += f" {_duration_phrase(q.inicio_duracao.valor)}"
    if q.localizacao.valor:
        parts.append(f"localização: {q.localizacao.valor}")
    if q.intensidade.valor:
        parts.append(f"intensidade {q.intensidade.valor}")
    if q.frequencia.valor:
        parts.append("é constante" if q.frequencia.valor == "constante" else f"ocorre {q.frequencia.valor}")
    if q.piora.valor:
        parts.append(f"piora {q.piora.valor}")
    if q.melhora.valor:
        parts.append(f"melhora {q.melhora.valor}")
    if main and q.associados.valor:
        parts.append(f"associada a {q.associados.valor}")
    if main and q.nega.valor:
        parts.append(f"sem {q.nega.valor}")
    return ", ".join(parts) + falas_text(q.refs) + "."


def hda_text(queixas: list[QueixaDetalhada], extras: list[str]) -> str:
    """HDA do modo regras: um período por queixa, sem aspas de citação. `extras`: frases já prontas (ex.: remédio)."""
    sentences = []
    plain = []
    for i, q in enumerate(queixas):
        detailed = any(getattr(q, f).valor for f in FIELD_ORDER[:-1])
        if i == 0 or detailed:
            sentences.append(hda_sentence(q, main=i == 0))
        else:
            plain.append(q)
    if plain:
        sentences.append(
            f"Refere também {join_pt([q.rotulo.lower() for q in plain])}"
            f"{falas_text([r for q in plain for r in q.refs])}."
        )
    return " ".join(sentences + extras) or "—"


def complaint_text(queixas: list[QueixaDetalhada]) -> str:
    """Queixa principal: a primeira da lista (ou a fixada) com a duração; as outras entre parênteses."""
    if not queixas:
        return "Sem queixa registrada na conversa até agora."
    main, rest = queixas[0], queixas[1:]
    text = main.rotulo
    if main.inicio_duracao.valor:
        text += f" {_duration_phrase(main.inicio_duracao.valor)}"
    if rest:
        text += f" (também: {join_pt([q.rotulo.lower() for q in rest[:3]])})"
    return text


def short_duration(valor: str) -> str:
    m = re.match(r"^(\d+) (\w+)$", valor)
    if not m:
        return valor
    unit = UNIT_SHORT.get(m.group(2)[:3])
    return f"{m.group(1)} {unit}" if unit else valor


def tag_detail(q: QueixaDetalhada) -> str:
    """'3 sem · ao levantar': o que já se sabe da queixa, para a tag."""
    parts = [
        short_duration(q.inicio_duracao.valor) if q.inicio_duracao.valor else "",
        q.intensidade.valor,
        q.piora.valor,
        q.frequencia.valor,
    ]
    return " · ".join(p for p in parts if p)[:80]
