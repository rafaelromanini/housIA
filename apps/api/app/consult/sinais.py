"""Sinais de comunicação (#43): intensidade da emoção por léxico e dúvidas ou preocupações sem resposta.

São sinais de comunicação, não diagnóstico: servem para o profissional não encerrar a consulta com uma pergunta do
paciente ou da família sem resposta. Só regras (rodam com ou sem IA), sempre com `Fala/N` de origem e, quando
respondida, a `Fala/N` da resposta. A ligação entre a dúvida e a resposta é por sobreposição de termos (radicais de 5
letras, sem palavras genéricas); a resposta direta ("pode", "sim", "não") vale só na fala imediatamente seguinte do
profissional, e "já explico" ou "depois" adiam, não respondem.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.consult import knowledge
from app.consult.schemas import Concern, Segment, fala_ref
from app.consult.text import clauses, find, keyword_status, norm

AVISO = "sinal de comunicação, não diagnóstico"
PATIENT_SIDE = ("paciente", "acompanhante")
TEAM_SIDE = ("medico", "anestesista", "circulante", "instrumentador")
LEVELS = ("baixa", "media", "alta")
WINDOW = 30  # caracteres de cada lado da palavra da emoção em que o intensificador vale
CONCERN_EMOTIONS = ("ansiedade", "preocupacao", "confusao")  # emoções que contam como preocupação do paciente

# termos genéricos que não provam que a dúvida foi respondida
GENERIC = frozenset(
    "isso essa esse essas esses coisa coisas senhora senhor doutor doutora dona tenho tinha pode posso podem sera "
    "fazer feito estou estava fico fiquei ficar vamos vai vou como quando onde porque porquê quanto qual quais para "
    "pela pelo muito muita mais menos tambem ainda sempre nunca depois antes agora hoje ontem aqui entao assim bom "
    "bem tudo todo toda nada algo alguma algum ter ser estar tomar queria queria saber sobre entender entendi "
    "grave serio seria minha meu minhas meus quase junto mesmo mesma outra outro desde".split()
)
DIRECT_ANSWER = re.compile(r"^(sim|nao|pode|claro|isso|exato|exatamente|pode ser|e sim|e verdade|nao precisa|calma)\b")
ACKNOWLEDGE = re.compile(
    r"\b(entendo|compreendo|imagino|e normal|fica tranquil|calma|vamos resolver|nao se preocupe)\b"
)
DEFER = re.compile(
    r"\b(ja explico|ja vou|ja te explico|ja falo|ja respondo|depois|mais tarde|em seguida|antes disso)\b"
)
INTERROGATIVE = re.compile(  # sem "?" (microfone) só estes começos valem; com "?" qualquer frase vale
    r"^(e )?(o que|por que|porque|qual|quais|posso|podemos|devo|sera que|e se|isso e|e grave|e perigoso|da pra|"
    r"da para)\b"
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]


def terms(text: str) -> list[str]:
    """Termos de conteúdo (sem acento, 4 letras ou mais, sem palavras genéricas), na ordem em que aparecem."""
    out: list[str] = []
    for w in re.findall(r"[a-z0-9]{4,}", norm(text)):
        if w not in GENERIC and not w.isdigit() and w not in out:
            out.append(w)
    return out


def stem(w: str) -> str:
    return w[:5]


# ---------- intensidade por léxico ----------
def intensity_of(text: str, words: Iterable[str]) -> tuple[str, list[str]]:
    """("baixa"|"media"|"alta", intensificadores encontrados) da emoção na fala, olhando uma janela em volta da palavra
    da emoção em cada oração: "tenho muito medo" é alta; "um pouco preocupada", baixa; sem intensificador, baixa."""
    lex = knowledge.intensity_lexicon()
    best, found = "baixa", []
    for clause in clauses(text):
        hit = find(clause, tuple(words))
        if not hit:
            continue
        _, start, end = hit
        win = f" {clause[max(0, start - WINDOW) : end + WINDOW]} "
        alta = [w.strip() for w in lex.get("alta", ()) if find(win, (w.strip(),))]
        media = [w.strip() for w in lex.get("media", ()) if find(win, (w.strip(),))]
        baixa = [w.strip() for w in lex.get("baixa", ()) if find(win, (w.strip(),))]
        if alta:
            level, marks = "alta", alta
        elif media and baixa:
            level, marks = "baixa", baixa  # "um pouco muito" não existe; o atenuador vence o "muito"
        elif media:
            level, marks = "media", media
        else:
            level, marks = "baixa", baixa
        if LEVELS.index(level) > LEVELS.index(best) or (level == best and marks and not found):
            best, found = level, marks
    return best, found


def combine(levels: Iterable[tuple[str, list[str]]]) -> tuple[str, list[str]]:
    """O maior nível entre as falas (repetir a mesma emoção não a torna mais intensa), com os intensificadores."""
    best, found = "baixa", []
    for level, marks in levels:
        if LEVELS.index(level) > LEVELS.index(best):
            best, found = level, list(marks)
        elif level == best:
            found += [m for m in marks if m not in found]
    return best, found


# ---------- dúvidas e preocupações sem resposta ----------
def _doubts(s: Segment, emotions_on: bool) -> list[tuple[str, str]]:
    """(tipo, frase) de cada dúvida ou preocupação dita pelo paciente ou pelo acompanhante na fala."""
    out: list[tuple[str, str]] = []
    defs = [d for d in knowledge.emotions() if d.key in CONCERN_EMOTIONS] if emotions_on else []
    doubt_defs = [d for d in defs if d.key == "confusao"]
    for sentence in _sentences(s.texto):
        t = norm(sentence)
        if not terms(sentence):
            continue
        worry = any(
            keyword_status(t, d.words, trailing_negation=False) == "presente" and not find(t, d.excludes)
            for d in defs
            if d.key != "confusao"  # "não entendi", "será que" são dúvida, não preocupação
        )
        question = "?" in sentence or bool(INTERROGATIVE.match(t))
        question = question or any(
            keyword_status(t, d.words, trailing_negation=False) == "presente" for d in doubt_defs
        )
        if worry:
            out.append(("preocupacao", sentence))
        elif question:
            out.append(("duvida", sentence))
    return out


def _answers(concern_terms: list[str], reply: str, immediate: bool, kind: str) -> bool:
    t = norm(reply)
    if {stem(w) for w in concern_terms} & {stem(w) for w in terms(reply)}:
        return True
    if not immediate or DEFER.search(t):
        return False
    if kind == "preocupacao":
        return bool(ACKNOWLEDGE.search(t))
    return bool(DIRECT_ANSWER.match(t))


def unanswered_concerns(segments: list[Segment], emotions_on: bool = True) -> list[Concern]:
    """Cada dúvida ou preocupação do paciente/família ligada às falas seguintes do profissional.

    `respondida` só fica verdadeira com a fala do profissional que cita o assunto (ou, na fala imediatamente seguinte,
    uma resposta direta). Sem `emocoes` no consentimento, a análise de sinais fica desligada (como as emoções)."""
    if not emotions_on:
        return []
    out: list[Concern] = []
    for i, s in enumerate(segments):
        if s.falante not in PATIENT_SIDE:
            continue
        later = [x for x in segments[i + 1 :] if x.falante in TEAM_SIDE]
        for kind, sentence in _doubts(s, emotions_on):
            keys = terms(sentence)[:6]
            reply = None
            for j, r in enumerate(later):
                if _answers(keys, r.texto, j == 0, kind):
                    reply = r
                    break
            quote = sentence if len(sentence) <= 110 else sentence[:110] + "…"
            out.append(
                Concern(
                    tipo=kind,  # type: ignore[arg-type]
                    quem=s.falante,
                    trecho=f"“{quote}”",
                    termos=keys[:4],
                    refs=[fala_ref(s.n)],
                    respondida=reply is not None,
                    resposta_refs=[fala_ref(reply.n)] if reply else [],
                    teach_back=s.falante == "acompanhante" or kind == "preocupacao",
                    aviso=AVISO,
                )
            )
    return out
