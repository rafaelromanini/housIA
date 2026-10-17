"""#10 — Relevância de cada fala (clínica × social × administrativa) e cobertura da nota.

A regra decide na hora, a cada fala: é CLÍNICA se casa sintoma, medicamento, alergia, sinal de alarme, sinal vital,
exame, antecedente, contexto de vida (CONTEXT_WORDS) ou o que uma pergunta sugerida procura (asked_words). Fala com
medicamento, dose, alergia ou sinal de alarme tem TRAVA: nunca vira social, nem quando a IA a reclassifica. A
cobertura mostra quantas falas clínicas alguma seção da nota cita; o que ficou de fora o médico decide se inclui.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from app.consult import knowledge
from app.consult.engine import CONTEXT_WORDS, REACTION_HIGH
from app.consult.schemas import Segment, fala_ref
from app.consult.text import contains, drug_hits
from app.consult.text import norm as _norm

Relevancia = Literal["clinica", "social", "administrativa"]
LEVELS: tuple[str, ...] = ("clinica", "social", "administrativa")

# Sinais de alarme ditos de forma corrida (além das reações graves de alergia).
ALARM_WORDS = (
    "dor no peito",
    "aperto no peito",
    "falta de ar",
    "desmai",
    "convuls",
    "sangramento",
    "sangue nas fezes",
    "sangue na urina",
    "vomitando sangue",
    "vomitei sangue",
    "tosse com sangue",
    "perdi a visao",
    "visao turva",
    "boca torta",
    "fraqueza de um lado",
    "nao consigo falar",
    "dor de cabeca forte",
    "rigidez no pescoco",
    "ideia de morte",
    "quero morrer",
    "me matar",
    "tentativa de suicidio",
)
ALLERGY_WORDS = ("alergi", "alergic", "anafila", "reacao ao", "reacao a ")
VITAL_WORDS = (
    "pressao",
    "glicemia",
    "glicose",
    "saturacao",
    "oximetria",
    "temperatura",
    "batimento",
    "frequencia cardiaca",
    "pulso",
    "peso",
    "altura",
    "imc",
    "dextro",
    "hgt",
)
EXAM_WORDS = (
    "exame",
    "resultado",
    "raio x",
    "radiografia",
    "tomografia",
    "ressonancia",
    "ultrassom",
    "ecocardiograma",
    "eletrocardiograma",
    "ecg",
    "biopsia",
    "colesterol",
    "creatinina",
    "potassio",
    "hemograma",
    "hemoglobina",
    "sodio",
)
CLINICAL_CONDUCT = (
    "cirurgia",
    "operar",
    "internar",
    "internacao",
    "diagnostico",
    "tratamento",
    "remedio",
    "medicamento",
    "medicacao",
    "receita",
    "tomar",
    "tomo",
    "tomei",
    "tomando",
    "dose",
    "comprimido",
    "gotas",
    "injecao",
    "insulina",
    "sintoma",
    "doenca",
    "diabet",
    "hipertens",
    "pressao alta",
    "doi",
    "dor ",
    "dores",
    "enjoo",
    "vomit",
    "tontura",
    "tonta",
    "zonza",
    "febre",
    "tosse",
    "cansaco",
    "fraqueza",
    "suor frio",
    "sinto ",
    "sentindo",
    "caroco",
    "nodulo",
    "inchaco",
    "diarre",
    "cagando",
    "fezes",
    "urina",
    "xixi",
    "engasg",
    "intestino",
    "coceira",
    "ferida",
)
ADMIN_WORDS = (
    "convenio",
    "carteirinha",
    "plano de saude",
    "autorizacao",
    "agendar",
    "agendamento",
    "marcar retorno",
    "marcar o retorno",
    "marcar a consulta",
    "remarcar",
    "pagamento",
    "pagar",
    "boleto",
    "recepcao",
    "protocolo de atendimento",
    "senha",
    "documento",
    "cpf",
    "guia ",
    "reembolso",
    "nota fiscal",
    "estacionamento",
    "atestado de comparecimento",
    "declaracao",
    "horario de atendimento",
    "fila",
)
# resposta curta ("sim", "só de manhã") ao que a outra pessoa acabou de perguntar vale pelo assunto da pergunta
ACK_ONLY = re.compile(r"^(bom dia|boa tarde|boa noite|oi|ola|obrigad[oa]|tchau|ate (logo|mais)|de nada|tudo bem)\b")
SHORT_REPLY_WORDS = 4
DOSE = re.compile(
    r"\b\d+([.,]\d+)?\s*(mg|mcg|ml|g|ui|u|gotas?|comprimidos?|cp|ampolas?|capsulas?|unidades?|vezes? (ao|por|no) dia)\b"
    r"|\b(meio|um|uma|dois|duas|tres)\s+(comprimidos?|capsulas?|gotas?|ampolas?)\b"
    r"|\b(1|2|3|4)\s*x\s*(ao|por|no)?\s*dia\b"
)
MEASURE = re.compile(r"\b\d{2,3}\s*(por|x|/)\s*\d{2,3}\b|\b(deu|esta|ta|foi|marcou|marca)\s+\d+\b")


def norm(text: str) -> str:
    """Sem acento, minúsculas e sem pontuação (a pontuação atrapalha as palavras-chave com espaço no fim)."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s+/-]", " ", _norm(text))).strip()


@lru_cache
def _vocab() -> dict[str, tuple[str, ...]]:
    symptom_words = tuple(w for d in knowledge.symptoms() for w in d.words)
    asked = tuple(w for q in knowledge.questions() for w in q.asked_words)
    history = tuple(t for c in knowledge.conditions() for t in c.terms) + tuple(
        t for p in knowledge.procedures() for t in p.terms
    )
    allergens = tuple(w for a in knowledge.allergy_defs() for w in a.words)
    labs = tuple(s for a in knowledge.analytes() for s in a.synonyms if len(s) >= 4)  # "fa" casaria "fazer"
    return {
        "sintoma": symptom_words,
        "perguntado": asked,
        "historico": history,
        "alergeno": allergens,
        "exame": labs,
    }


def travada(texto: str) -> str | None:
    """Motivo da trava (medicamento, dose, alergia ou sinal de alarme), ou None: a fala nunca pode ficar fora da nota
    por ser conversa social."""
    t = norm(texto)
    if drug_hits(t):
        return "medicamento"
    if DOSE.search(t):
        return "dose"
    if contains(t, ALLERGY_WORDS) or contains(t, _vocab()["alergeno"]):
        return "alergia"
    if contains(t, ALARM_WORDS) or contains(t, REACTION_HIGH):
        return "sinal_de_alarme"
    return None


WEAK_REASONS = ("pergunta_sugerida", "conduta", "contexto")  # palavras genéricas: "horário", "remédio", "trabalho"


def clinical_by_rule(texto: str) -> str | None:
    """Por que a fala é clínica (rótulo da regra), ou None."""
    t = norm(texto)
    if (lock := travada(texto)) is not None:
        return lock
    v = _vocab()
    checks: tuple[tuple[str, bool], ...] = (
        ("sintoma", bool(contains(t, v["sintoma"]))),
        ("sinal_vital", bool(contains(t, VITAL_WORDS)) or bool(MEASURE.search(t))),
        ("exame", bool(contains(t, EXAM_WORDS)) or bool(contains(t, v["exame"]))),
        ("historico", bool(contains(t, v["historico"]))),
        ("contexto", bool(contains(f"{t} ", CONTEXT_WORDS))),
        ("pergunta_sugerida", bool(contains(t, v["perguntado"]))),
        ("conduta", bool(contains(t, CLINICAL_CONDUCT))),
    )
    return next((name for name, hit in checks if hit), None)


def classify(texto: str, previous: Segment | None = None, speaker: str = "") -> Relevancia:
    """Regra de relevância de uma fala. `previous` é a fala anterior (a resposta curta herda o assunto dela)."""
    reason = clinical_by_rule(texto)
    t = norm(texto)
    admin = contains(f"{t} ", ADMIN_WORDS)
    if reason and not (admin and reason in WEAK_REASONS):
        return "clinica"
    if admin:
        return "administrativa"
    if (
        previous is not None
        and previous.relevancia == "clinica"
        and previous.falante != speaker
        and 0 < len(t.split()) <= SHORT_REPLY_WORDS
        and not ACK_ONLY.match(t)
    ):
        return "clinica"
    return "social"


def ensure(segments: list[Segment]) -> list[Segment]:
    """Completa a relevância das falas que ainda não têm (consultas antigas e falas novas), em ordem."""
    prev: Segment | None = None
    for s in segments:
        if s.relevancia is None:
            s.relevancia = classify(s.texto, prev, s.falante)
            s.relevancia_origem = "regra"
        prev = s
    return segments


def apply_ai(segments: list[Segment], fixes: dict[int, str]) -> list[int]:
    """Aplica a reclassificação da IA e devolve as falas alteradas. Fala com trava (medicamento, dose, alergia ou sinal
    de alarme) não sai de "clínica"; a classificação só muda por uma das três categorias válidas."""
    changed: list[int] = []
    for s in segments:
        new = fixes.get(s.n)
        if new not in LEVELS or new == s.relevancia:
            continue
        if new != "clinica" and travada(s.texto):
            continue
        s.relevancia = new  # type: ignore[assignment]
        s.relevancia_origem = "ia"
        changed.append(s.n)
    return changed


# ---------- cobertura da nota ----------
_FALA = re.compile(r"^Fala/(\d+)$")
NOTE_SECTIONS = (
    "queixa_principal",
    "hda",
    "antecedentes",
    "medicacoes",
    "alergias",
    "exame_fisico",
    "exames_complementares",
    "avaliacao",
    "plano",
)
MIN_WORDS = 3  # "sim", "tá bom": não precisam constar da nota


def cited_falas(note: dict) -> set[int]:
    refs: list[str] = []
    for key in NOTE_SECTIONS:
        refs += (note.get(key) or {}).get("refs", [])
    for key in ("cids", "cronologia", "pendencias", "secoes"):
        for item in note.get(key) or []:
            refs += item.get("refs", [])
    return {int(m.group(1)) for r in refs if (m := _FALA.match(r))}


def coverage(segments: list[Segment], note: dict | None) -> dict | None:
    """{"total", "citadas", "percentual", "fora": [...]}: falas clínicas que alguma seção da nota cita × as que não."""
    if not note or "relatorio_cirurgico" in note:
        return None
    segments = [s for s in segments if s.origem != "marco"]
    cited = cited_falas(note)
    clinical = [s for s in segments if s.relevancia == "clinica" and len(norm(s.texto).split()) >= MIN_WORDS]
    out = [s for s in clinical if s.n not in cited]
    done = len(clinical) - len(out)
    return {
        "total": len(clinical),
        "citadas": done,
        "percentual": round(100 * done / len(clinical)) if clinical else 100,
        "rotulo": f"{done} de {len(clinical)} falas clínicas citadas",
        "fora": [{"n": s.n, "ref": fala_ref(s.n), "falante": s.falante, "t": s.t, "texto": s.texto} for s in out],
        "sociais": sum(1 for s in segments if s.relevancia != "clinica"),
    }


def include(note: dict, seg: Segment, destino: str, by: str, when: str) -> dict:
    """Acrescenta o trecho da fala (com a ref Fala/N) à seção; repetir a mesma fala na mesma seção não duplica."""
    note = dict(note)
    ref = fala_ref(seg.n)
    done = list(note.get("incluidas", []))
    if any(i["n"] == seg.n and i["destino"] == destino for i in done):
        return note | {"incluidas": done}
    line = seg.texto.strip()
    if destino == "pendencia":
        note["pendencias"] = [*note.get("pendencias", []), {"texto": line, "refs": [ref]}]
    else:
        sec = dict(note.get(destino) or {"texto": "", "refs": []})
        base = sec.get("texto", "")
        sec["texto"] = (base + "\n" if base and base != "Sem registro na consulta" else "") + line
        sec["refs"] = list(dict.fromkeys([*sec.get("refs", []), ref]))
        note[destino] = sec
    done.append({"n": seg.n, "ref": ref, "destino": destino, "por": by, "em": when})
    note["incluidas"] = done
    return note
