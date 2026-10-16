"""Utilitários de texto da transcrição: normalização, orações, negação e menção de medicamentos/alergias.

A fala real chega sem pontuação (microfone), com vocativo no meio ("não doutora só tontura quando levanto"), com
perguntas sem "?" e com negativas no passado ("parei", "não tomo mais"). As regras daqui são medidas a cada build
pelo corpus de regressão `data/consultations/regressao_falas.json` (tests/test_consult_regressao.py).
"""

import re
from functools import lru_cache
from typing import Literal, NamedTuple

from app.reference import drugs
from app.timeline.record import normalize

NEG_WORDS = {"nao", "nunca", "nem", "sem", "nega", "negou", "nenhum", "nenhuma", "jamais"}
# orações: pontuação, marcadores que a fala sem pontuação usa no lugar da vírgula ("só", "mas", "porém", "agora")
# e quebra logo depois do vocativo ("não doutora | só tontura"), que fica na oração da esquerda
CLAUSE_SPLIT = re.compile(r"[.,;:!?\n]|(?<!\w)(?:mas|porem|so|agora)(?!\w)|(?<=\bdoutora)(?=\s)|(?<=\bdoutor)(?=\s)")

# alcance da negação: entre "não" e o termo só cabem palavras "leves" (sujeito, artigo, ter/sentir/estar, auxiliar).
# "não tenho mais tontura" nega; "não consigo dormir com essa tosse" e "não sei se é tontura" não negam a tosse/tontura.
NEG_WINDOW = 4
LIGHT_WORDS = set(
    (
        "eu ela ele a o as os um uma uns umas de da do das dos na no essa esse esta este aquela aquele isso "
        "nada muito muita mais tambem ja ainda nunca mesmo bem assim vida graca gracas deus ai sim senhora "
        "senhor doutora doutor nenhum nenhuma tenho tive tem teve tinha tido sinto senti sente sentiu sentia "
        "sentindo sentido estou to tou ta estava estive ando andei anda fico fiquei sou foi era e apresento "
        "apresentei notei percebi vai vou vamos precisa precisar preciso precisou quer quero pode posso deve "
        "crise crises sintoma sintomas sinal problema problemas episodio queixa vez vezes"
    ).split()
)
# depois do termo, a negação só vale como negativa se fecha a oração ou vem com ter/sentir: "dor no peito eu não
# tenho", "falta de ar também não" — mas "a tontura não passa" e "dor que não melhora" continuam presentes
DENIAL_AFTER = {"tenho", "tive", "tem", "teve", "sinto", "senti", "sente", "sentiu", "sentia", "tinha", "mais", "nunca"}
DENIAL_AFTER |= {"apresento", "apresentei", "senhora", "senhor", "doutora", "doutor"}

# pergunta dirigida a quem escuta (2ª pessoa): "a senhora tem sentido dor no peito" não é queixa de quem fala
SECOND_PERSON = r"(a senhora|o senhor|senhora|senhor|voce|vc|a sra|o sr)"
EXPERIENCE = r"(tem|teve|tinha|sente|sentiu|sentia|esta|ta|anda|andou|apresenta|apresentou|notou|percebeu|costuma)"
LISTENER_QUESTION = re.compile(
    rf"(?<!\w){SECOND_PERSON}( (ainda|ja|nao|tambem|nunca|alguma vez))? {EXPERIENCE}(?!\w)"
    r"|^(e )?(tem sentido|tem tido|tem notado|tem percebido|anda sentindo|esta sentindo|ta sentindo"
    r"|sente algum|sente alguma|sentiu algum|sentiu alguma|teve algum|teve alguma|quando comecou|desde quando"
    r"|ha quanto tempo|faz quanto tempo)(?!\w)"
)
VOCATIVE = re.compile(r"(?<!\w)(doutora|doutor|dra|dr)(?!\w)")
# "a doutora falou", "o doutor Paulo": referência a outro profissional, não vocativo
NOT_VOCATIVE_BEFORE = {"o", "a", "do", "da", "no", "na", "pelo", "pela", "com", "pro", "pra", "ao", "outro", "outra"}
NOT_VOCATIVE_BEFORE |= {"seu", "sua", "meu", "minha", "esse", "essa", "aquele", "aquela", "um", "uma"}

# nomes comerciais comuns -> princípio ativo (data/reference/drugs.csv)
BRANDS = {
    "advil": "ibuprofeno",
    "alivium": "ibuprofeno",
    "buscofem": "ibuprofeno",
    "novalgina": "dipirona",
    "anador": "dipirona",
    "tylenol": "paracetamol",
    "cataflam": "diclofenaco",
    "voltaren": "diclofenaco",
    "aspirina": "aas",
    "rivotril": "clonazepam",
    "glifage": "metformina",
    "daonil": "glibenclamida",
}
GENERIC_CLASSES = {"anti-inflamatorio": "aine", "antiinflamatorio": "aine", "anti inflamatorio": "aine"}

# ---------- status do medicamento citado ----------
DrugStatus = Literal["em_uso", "suspenso_relatado", "negado"]
TAKE = r"(tomo|tomei|toma|tomou|tomando|tomar|tomava|tomado|uso|usei|usa|usou|usando|usar|usava|usado)"
# antes do medicamento: "parei o", "deixei de tomar", "não tomo mais", "o médico tirou o", "tomava", "nunca mais"
STOPPED_BEFORE = re.compile(
    r"(?<!\w)(parei|paramos)(?! de (?!tomar|usar))(?!\w)"
    r"|(?<!\w)(deixei|deixou|deixamos) de (tomar|usar)(?!\w)"
    r"|(?<!\w)(suspendi|suspendeu|suspenderam|suspenso|suspensa|tirei|tiraram|tirou|cortaram)(?!\w)"
    r"|(?<!\w)(tomava|usava|tomei por um tempo|usei por um tempo)(?!\w)"
    rf"|(?<!\w)(nao|nem) (\w+ ){{0,2}}?{TAKE} mais(?!\w)(?! (que|de|do que)(?!\w))"
    r"|(?<!\w)(nao|nem) (\w+ )?(preciso|precisa) mais(?!\w)"
    r"|(?<!\w)(nunca mais|ja nao)(?!\w)|(?<!\w)(faz|ha) (\w+ ){1,3}que (nao|nem)(?!\w)"
    r"|(?<!\w)(mandou|mandaram|pediu|pediram|falou|disse) (eu |pra |para |que )?(parar|suspender|tirar)(?!\w)"
)
# "não tomo ibuprofeno todo dia" nega a frequência, não o uso
FREQUENCY = re.compile(r"^(o |a |os |as )?(todo dia|todos os dias|sempre|direto|toda hora|muito|tanto|com frequencia)")
# depois do medicamento (só 1ª pessoa: "a dor parou" não é suspensão): "ibuprofeno eu parei faz um mês"
STOPPED_AFTER = re.compile(
    r"^((eu|ja|tambem|esse|essa|isso|ai|faz|tempo) )*"
    rf"(parei|deixei de (tomar|usar)|suspendi|tomava|usava|nao {TAKE} mais|nunca mais {TAKE})(?!\w)"
)
# negação que governa o tomar/usar e chega ao medicamento só com artigo/"remédio" no meio
NEG_TAKE = re.compile(
    r"(?<!\w)(nao|nunca|nem|jamais)"
    r"( (eu|ja|mais|estou|to|tou|tenho|ando|costumo|posso|consigo|quero|vou|cheguei a|gosto de|na vida|sequer))*"
    rf" {TAKE}"
    r"( (o|a|os|as|um|uma|nenhum|nenhuma|de|do|da|esse|essa|isso|remedio|remedios|comprimido|comprimidos))*$"
)
NEG_DIRECT = re.compile(r"(?<!\w)(nem|sem|nenhum|nenhuma|nada de|nao)( (o|a|um|uma|de))?$")
NEG_AFTER = re.compile(
    rf"^((eu|tambem|ja|isso|esse|essa|nunca) )*(nao|nunca|nem)( ((eu|ja|nunca|tambem) )*{TAKE}(?!\w).*)?$"
)
# oração sem medicamento que comenta o citado logo antes: "O ibuprofeno? Parei faz um mês." / "Não, nunca tomei."
BARE_TAIL = r"( (faz|ha|tem|desde|porque|pq|pois|quando|semana|mes|ano|ontem|depois|ja|tambem|uns|umas|um|uma|mais"
BARE_TAIL += r"|antes|no|na|doutora|doutor|senhora|senhor)(?!\w).*)?$"
BARE_STOPPED = re.compile(
    r"^((eu|ja|e|a) )*(parei|deixei de tomar|deixei de usar|suspendi|nao tomo mais|nao uso mais|nao estou mais tomando"
    r"|nao to mais tomando|nunca mais tomei|nunca mais usei|tomava|usava)( de tomar| de usar| com ele| com ela)?"
    + BARE_TAIL
)
BARE_NEGATED = re.compile(
    r"^((eu|ja|e) )*(nao|nunca|nem)( ((eu|ja|nunca|tambem) )*(tomo|tomei|uso|usei))?"
    r"( (nao|nunca|na vida|sim|doutora|doutor|senhora|senhor))?$"
)
_STATUS_RANK = {"em_uso": 0, "suspenso_relatado": 1, "negado": 2}


class DrugMention(NamedTuple):
    ingredient: str  # princípio ativo ou "@classe"
    term: str  # como foi dito
    status: DrugStatus


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", normalize(text)).strip()


def _split(sentence: str) -> list[str]:
    return [c.strip() for c in CLAUSE_SPLIT.split(f" {sentence} ") if c.strip()]


def clauses(text: str) -> list[str]:
    return _split(norm(text))


@lru_cache(maxsize=4096)
def _keyword(w: str) -> re.Pattern:
    # o termo começa no início de uma palavra ("operar" não casa com "cooperar"); termos com espaço à esquerda já
    # marcam a fronteira sozinhos
    return re.compile(re.escape(w) if w.startswith(" ") else r"(?<![a-z0-9])" + re.escape(w))


def find(text: str, words: tuple[str, ...] | list[str]) -> tuple[str, int, int] | None:
    """(primeira palavra-chave presente, início, fim da palavra em que termina) no texto já normalizado, ou None.

    Palavra-chave composta ("joelho+doi") exige todas as partes no texto, em qualquer ordem ("dói muito o joelho").
    """
    padded = f" {text} "
    for w in words:
        if "+" in w:
            spans = [find(text, (part,)) for part in w.split("+")]
            if all(spans):
                return w, min(sp[1] for sp in spans if sp), max(sp[2] for sp in spans if sp)
            continue
        m = _keyword(w).search(padded)
        if m:
            start = m.start() if w.startswith(" ") else m.start() - 1
            end = text.find(" ", start + len(w.strip()))
            return w, start, len(text) if end < 0 else end
    return None


def contains(text: str, words: tuple[str, ...] | list[str]) -> str | None:
    """Primeira palavra-chave presente (texto já normalizado), ou None."""
    hit = find(text, words)
    return hit[0] if hit else None


def _negated(before: str, after: str, trailing: bool = True) -> bool:
    """A negação alcança o termo? `before`/`after` = resto da oração antes/depois do termo.

    `trailing`: um "não" solto no fim da oração também nega ("falta de ar também não").
    """
    words = before.split()
    for i, w in enumerate(reversed(words)):
        if w in NEG_WORDS:
            return True
        if w not in LIGHT_WORDS or i >= NEG_WINDOW:
            break
    tail = after.split()
    for i, w in enumerate(tail):
        if w in NEG_WORDS:
            return (trailing and i + 1 == len(tail)) or (i + 1 < len(tail) and tail[i + 1] in DENIAL_AFTER)
        if w not in LIGHT_WORDS or i >= NEG_WINDOW:
            break
    return False


def has_vocative(text: str) -> bool:
    """Fala dirigida ao profissional ("bom dia, doutora"): sinal de paciente/acompanhante."""
    t = norm(text)
    for m in VOCATIVE.finditer(t):
        prev = re.sub(r"[^\w\s]", " ", t[: m.start()]).split()[-1:]
        if not prev or prev[0] not in NOT_VOCATIVE_BEFORE:
            return True
    return False


def listener_question(clause: str) -> bool:
    """Oração que pergunta da experiência de quem escuta ("a senhora tem sentido...", "tem tido febre")."""
    return bool(LISTENER_QUESTION.search(clause))


def keyword_status(
    text: str,
    words: tuple[str, ...],
    skip_questions: bool = False,
    excludes: tuple[str, ...] = (),
    trailing_negation: bool = True,
) -> str | None:
    """'presente', 'negado' ou None para um grupo de palavras-chave numa fala.

    `skip_questions`: ignora as orações que perguntam da experiência de quem escuta (2ª pessoa), a não ser que a fala
    seja dirigida ao profissional por vocativo ("doutora, a senhora acha que essa tontura é da pressão?").
    `excludes`: termos que, na mesma oração, anulam a palavra-chave ("a pressão caiu" não é queda).
    `trailing_negation=False`: o "não" solto no fim responde à pergunta e não nega o termo ("graças a Deus, não"
    é alívio); "medo eu não tenho" continua negativa.
    """
    found = None
    skip = skip_questions and not has_vocative(text)
    for clause in clauses(text):
        hit = find(clause, words)
        if not hit or (excludes and find(clause, excludes)):
            continue
        if skip and listener_question(clause):
            continue
        word, start, end = hit
        # a própria palavra-chave pode ter negação ("nao consigo urinar"): ela não conta como negativa; na composta,
        # vale a negação entre as partes ("o joelho não dói")
        inner = "+" in word and any(w in NEG_WORDS for w in clause[start:end].split())
        if not inner and not _negated(clause[:start], clause[end:], trailing_negation):
            return "presente"
        found = "negado"
    return found


# ---------- medicamentos citados ----------
@lru_cache
def _catalog_names() -> tuple[tuple[str, str, str], ...]:
    """(nome normalizado, princípio ativo, nome) do catálogo; nomes curtos (< 5 letras) geram falso positivo."""
    return tuple((norm(d.name), ing, d.name) for ing, d in drugs().items() if len(norm(d.name)) >= 5)


def _drug_hits(clause: str) -> list[tuple[int, int, str, str]]:
    """(início, fim, princípio ou @classe, termo dito) na oração, em ordem."""
    t = f" {clause} "
    catalog = drugs()
    hits: list[tuple[int, int, str, str]] = []
    for name, ing, label in _catalog_names():
        idx = t.find(f" {name}")
        if idx >= 0:
            hits.append((idx, idx + 1 + len(name), ing, label))
    for brand, ing in BRANDS.items():
        idx = t.find(f" {brand}")
        if idx >= 0 and ing in catalog and all(ing != h[2] for h in hits):
            hits.append((idx, idx + 1 + len(brand), ing, brand.capitalize()))
    if not any(catalog[h[2]].drug_class == "aine" for h in hits if h[2] in catalog):
        for term, cls in GENERIC_CLASSES.items():
            idx = t.find(term)
            if idx >= 0:
                hits.append((idx, idx + len(term), f"@{cls}", "anti-inflamatório"))
                break
    return sorted(hits)


def _drug_status(before: str, after: str) -> DrugStatus:
    before, after = before.strip(), after.strip()
    if STOPPED_BEFORE.search(before) or STOPPED_AFTER.search(after):
        return "suspenso_relatado"
    if FREQUENCY.search(after):
        return "em_uso"
    if NEG_TAKE.search(before) or NEG_DIRECT.search(before) or NEG_AFTER.search(after):
        return "negado"
    return "em_uso"


@lru_cache(maxsize=4096)  # roda a cada fala, para todas as falas: a mesma frase não é reanalisada
def mentioned_drugs(text: str) -> tuple[DrugMention, ...]:
    """Medicamentos citados na fala, com status: em uso, suspenso (relatado) ou negado.

    Cada oração decide o status dos medicamentos que cita; o trecho entre dois medicamentos vale para o seguinte
    ("tomo ibuprofeno e não tomo mais a dipirona"). Uma oração sem medicamento que só diz "parei" ou "nunca tomei"
    comenta o citado logo antes ("O ibuprofeno? Parei faz um mês.").
    """
    found: dict[str, DrugMention] = {}
    last: list[str] = []
    for clause in clauses(text):
        t = f" {clause} "
        hits = _drug_hits(clause)
        if not hits:
            bare = "suspenso_relatado" if BARE_STOPPED.search(clause) else None
            bare = bare or ("negado" if BARE_NEGATED.search(clause) else None)
            if bare:
                for ing in last:
                    if found[ing].status == "em_uso":
                        found[ing] = found[ing]._replace(status=bare)
            elif not VOCATIVE.fullmatch(clause):
                last = []
            continue
        last = []
        for i, (start, end, ing, term) in enumerate(hits):
            lo = hits[i - 1][1] if i else 0
            after = t[end:] if i + 1 == len(hits) else ""
            status = _drug_status(t[lo:start], after)
            prev = found.get(ing)
            if prev is None or _STATUS_RANK[status] < _STATUS_RANK[prev.status]:
                found[ing] = DrugMention(ing, prev.term if prev else term, status)
            last.append(ing)
    return tuple(found.values())
