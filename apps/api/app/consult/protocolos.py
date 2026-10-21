"""Protocolos de APS vivos na conversa (#28): o que o profissional ou o paciente disse muda o status do item.

Determinístico: lê as falas pelas mesmas regras de negação das queixas (`keyword_status`). Um item tratado na consulta
vira "abordado — Fala/N"; gatilho crítico (ideação suicida, item 9 do PHQ-9) vira alerta crítico.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.consult import knowledge
from app.consult.schemas import Segment, fala_ref
from app.consult.text import NEG_WORDS, clauses, contains, find, is_question, keyword_status, norm

SEM_NEGACAO = (
    "!"  # prefixo de um termo que já carrega a negação ("não quero mais viver"): não se aplica a regra de negação
)
TRIMESTRE = re.compile(r"(\d{1,2}) semanas?")
GESTACAO = ("gestante", "gravida", "gestacao", "gravidez", "pre-natal", "prenatal", "barriga")


# adiada ou não feita ("deixei de examinar os pés", "não deu tempo de examinar", "preciso examinar depois"): o item
# continua pendente
ADIADO = re.compile(
    r"(?<!\w)(deixei de|deixou de|deixamos de|esqueci|esqueceu|nao deu tempo|nao consegui|nao conseguimos|nao pude"
    r"|fica para|ficou para|na proxima (consulta|vez)|proxima consulta|proximo retorno|outro dia|mais tarde"
    r"|(preciso|precisa|precisamos|vou|vamos|tenho que|temos que)\b[^.?!]*\bdepois)(?!\w)"
)


def falas_com(segments: Iterable[Segment], words: Iterable[str]) -> list[Segment]:
    """Falas que citam algum dos termos sem negar e sem ser pergunta ("o senhor examinou os pés?" não trata o item)."""
    plain = tuple(w for w in words if not w.startswith(SEM_NEGACAO))
    raw = tuple(w[len(SEM_NEGACAO) :] for w in words if w.startswith(SEM_NEGACAO))
    out = []
    for s in segments:
        if s.texto.strip().endswith("?") or ADIADO.search(norm(s.texto)):
            continue
        if (plain and keyword_status(s.texto, plain, skip_questions=True) == "presente") or (
            raw and contains(norm(s.texto), raw)
        ):
            out.append(s)
    return out


def _negado_antes(clause: str, start: int) -> bool:
    """Negação nas 5 palavras que antecedem o termo ("nunca pensei em me matar"): para o risco, a janela é maior."""
    return any(w in NEG_WORDS for w in clause[:start].split()[-5:])


# o paciente fala de outra pessoa ("meu irmão pensou em suicídio", "a vizinha tentou se matar"): não é a ideação dele,
# a não ser que ele diga que também sente ("e eu também")
OUTRA_PESSOA = re.compile(
    r"(?<!\w)(meu|minha|um|uma|o|a|nosso|nossa) (irmao|irma|pai|mae|filho|filha|marido|esposo|esposa|mulher|namorado"
    r"|namorada|noivo|noiva|amigo|amiga|colega|vizinho|vizinha|primo|prima|tio|tia|avo|avos|sogro|sogra|cunhado"
    r"|cunhada|genro|nora|neto|neta|conhecido|conhecida)(?!\w)"
)
TAMBEM_EU = re.compile(
    r"(?<!\w)eu (tambem|penso|pensei|ja pensei|quero|queria|tenho|ja tive)(?!\w)|(?<!\w)comigo(?!\w)"
    r"|(?<!\w)(igual|como) (a |o )?(ele|ela)(?!\w)|(?<!\w)o mesmo que (ele|ela)(?!\w)|(?<!\w)eu nao quero morrer"
)


def _de_outra_pessoa(s: Segment, clause: str, start: int) -> bool:
    return s.falante == "paciente" and bool(OUTRA_PESSOA.search(clause[:start])) and not TAMBEM_EU.search(norm(s.texto))


def falas_de_risco(segments: Iterable[Segment], words: Iterable[str]) -> list[Segment]:
    """Como `falas_com`, mas com a negação mais larga ("nunca pensei em me matar" não é ideação) e sem os relatos do
    paciente sobre outra pessoa ("meu irmão pensou em suicídio")."""
    plain = tuple(w for w in words if not w.startswith(SEM_NEGACAO))
    raw = tuple(w[len(SEM_NEGACAO) :] for w in words if w.startswith(SEM_NEGACAO))
    out = []
    for s in falas_com(segments, words):
        text = norm(s.texto)
        if _pergunta_de_risco(
            s, plain + raw
        ):  # o profissional perguntando ("a senhora pensa em se matar") não é ideação
            continue
        if raw and contains(text, raw):
            out.append(s)
            continue
        if any(
            (hit := find(c, plain)) and not _negado_antes(c, hit[1]) and not _de_outra_pessoa(s, c, hit[1])
            for c in clauses(s.texto)
        ):
            out.append(s)
    return out


# resposta curta e afirmativa à pergunta do profissional ("A senhora pensa em se matar?": "Sim.", "Às vezes.")
AFIRMA = re.compile(
    r"^(sim|tenho|tinha|penso|pensei|ja|as vezes|bastante|muito|aham|uhum|isso|claro|com certeza|todo dia|toda hora"
    r"|quase todo dia|frequentemente|diariamente|todos os dias|de vez em quando)(?!\w)"
)
SEGUNDA_PESSOA = re.compile(r"(?<!\w)(a senhora|o senhor|senhora|senhor|voce|vc|a sra|o sr)(?!\w)")


def _pergunta_de_risco(s: Segment, triggers: tuple[str, ...]) -> bool:
    """O profissional perguntou do risco (com ou sem "?"): o gatilho vem numa pergunta, não numa afirmação."""
    if s.falante not in ("medico", "desconhecido"):
        return False
    text = norm(s.texto)
    if not find(text, triggers):
        return False
    return s.texto.strip().endswith("?") or is_question(s.texto) or bool(SEGUNDA_PESSOA.search(text))


def _resposta_afirmativa(s: Segment) -> bool:
    first = clauses(s.texto)
    return s.falante in ("paciente", "acompanhante") and bool(first) and bool(AFIRMA.match(first[0]))


def respostas_a_perguntas_de_risco(segments: list[Segment], words: Iterable[str]) -> list[Segment]:
    """Pergunta de risco do profissional respondida com "sim" curto: a resposta (e a pergunta) vão como prova. Pergunta
    sem resposta, ou respondida com "não", não é ideação."""
    triggers = tuple(w.removeprefix(SEM_NEGACAO) for w in words)
    out: list[Segment] = []
    for i, s in enumerate(segments):
        if not _pergunta_de_risco(s, triggers):
            continue
        for answer in segments[i + 1 : i + 3]:  # a resposta vem logo depois; o profissional pode repetir a pergunta
            if answer.falante == s.falante:
                continue
            if _resposta_afirmativa(answer):
                out += [answer, s]
            break
    return out


def respostas_da_escala(saved: object) -> dict[str, str]:
    """Respostas salvas de uma escala (`{"respostas": {item: opção}}`) como texto; item em branco ou nulo não conta, e
    um registro malformado vira "sem respostas" em vez de derrubar a consulta."""
    answers = saved.get("respostas") if isinstance(saved, dict) else None
    if not isinstance(answers, dict):
        return {}
    return {str(k): str(v).strip() for k, v in answers.items() if v is not None and str(v).strip()}


def item9_phq9(answers: dict[str, str]) -> bool | None:
    """Item 9 do PHQ-9 (ideação): True se positivo, False se "nenhuma vez" (0), None se não respondido."""
    value = answers.get("ideacao")
    return None if value is None else value != "0"


def refs_das_falas(falas: list[Segment], limite: int = 3) -> list[str]:
    return [fala_ref(s.n) for s in falas[:limite]]


def trimestre_pela_fala(segments: Iterable[Segment]) -> int | None:
    """Trimestre da gestação pela idade gestacional dita ("de 30 semanas"), na fala que cita a gestação."""
    for s in segments:
        text = norm(s.texto)
        m = TRIMESTRE.search(text)
        if m and contains(text, GESTACAO + ("ig ", "idade gestacional")):
            weeks = int(m.group(1))
            if 1 <= weeks <= 42:
                return 1 if weeks <= 13 else 2 if weeks <= 27 else 3
    return None


def meses_de_vida(birth: str | None, today) -> int | None:
    if not birth:
        return None
    y, m, d = (int(x) for x in birth[:10].split("-"))
    months = (today.year - y) * 12 + today.month - m - (1 if today.day < d else 0)
    return max(months, 0)


def criticos(segments: Iterable[Segment]) -> dict[str, list[Segment]]:
    """Itens críticos (por id) cujo gatilho foi dito na consulta."""
    out = {}
    segments = list(segments)
    for d in knowledge.protocols():
        if d.critical and d.critical_triggers:
            hits = falas_de_risco(segments, d.critical_triggers)
            hits += [
                s
                for s in respostas_a_perguntas_de_risco(segments, d.critical_triggers + d.question_triggers)
                if s not in hits
            ]
            if hits:
                out[d.id] = hits
    return out
