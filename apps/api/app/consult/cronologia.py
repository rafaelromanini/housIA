"""Cronologia viva da consulta (#15): um evento tipado por fato dito, com Fala/N, falante e mm:ss.

Tudo por regras, a cada fala, sem IA: queixa e negativa vêm das etiquetas do motor; o resto (achado verbalizado,
decisão sobre medicamento, exame pedido, orientação, retorno) vem de padrões da fala do profissional, e a decisão
de medicamento também da fala do paciente ("comecei aquele diurético"). Pergunta nunca é evento: "o senhor tem
edema?" não registra edema. O mesmo detector alimenta o Plano e o exame físico da nota quando não há IA.
"""

from __future__ import annotations

import re

from app.consult.schemas import Insights, Segment, fala_ref
from app.consult.text import _drug_hits, is_question, norm, planned_drugs

TEAM = ("medico", "anestesista", "circulante", "instrumentador")
PATIENT = ("paciente", "acompanhante")
_SENTENCES = re.compile(r"(?<=[.!?])\s+")

# ---------- achados verbalizados pelo profissional ----------
# (rótulo, padrão sobre o texto normalizado, entra no exame físico da nota)
FINDINGS: tuple[tuple[str, re.Pattern, bool], ...] = (
    ("Pressão arterial", re.compile(r"(?<![a-z])(pressao|pa)(?![a-z])[^.?!]{0,60}?\d{2,3}"), True),
    (
        "Frequência cardíaca",
        re.compile(r"(frequencia cardiaca|(?<![a-z])fc(?![a-z])|pulso|batimentos?|coracao)[^.?!]{0,30}?\d{2,3}"),
        True,
    ),
    ("Saturação", re.compile(r"(saturacao|(?<![a-z])sat(?![a-z])|spo2|oximetria)[^.?!]{0,30}?\d{2,3}"), True),
    ("Temperatura", re.compile(r"(temperatura|(?<![a-z])temp(?![a-z]))[^.?!]{0,30}?\d{2}"), True),
    ("Glicemia", re.compile(r"glicemia[^.?!]{0,30}?\d{2,3}"), False),
    (
        "Ausculta",
        re.compile(r"(ausculta|murmurio vesicular|estertor|sibilo|crepita|(?<![a-z])sopro|bulhas (normofon|hipofon))"),
        True,
    ),
    (
        "Abdome",
        re.compile(
            r"(abdome|abdomen|barriga)[^.?!]{0,50}(flacid|globoso|doloros|distendid|indolor|sem dor|macio|ruidos|massa|"
            r"timpanic|palpac)|palpacao d[oa] (abdome|abdomen|barriga)"
        ),
        True,
    ),
    ("Edema", re.compile(r"(?<![a-z])edema|(pernas|tornozelos|pes) inchad|inchaco (nas|nos|em) "), True),
)

# ---------- decisões ----------
CLASS_WORDS = {  # classes ditas sem o nome do remédio: aparecem como o paciente ou o médico falou
    "diuretico": "diurético",
    "anti-hipertensivo": "anti-hipertensivo",
    "anti hipertensivo": "anti-hipertensivo",
    "antibiotico": "antibiótico",
    "antidepressivo": "antidepressivo",
    "ansiolitico": "ansiolítico",
    "anticoagulante": "anticoagulante",
    "corticoide": "corticoide",
    "analgesico": "analgésico",
    "insulina": "insulina",
    "betabloqueador": "betabloqueador",
    "remedio novo": "remédio novo",
    "remedio da pressao": "remédio da pressão",
    "remedio do acucar": "remédio do açúcar",
}
_START = {
    "comecei": "Iniciou",
    "iniciei": "Iniciou",
    "comecou": "Iniciou",
    "iniciou": "Iniciou",
    "comecaram": "Iniciou",
    "iniciaram": "Iniciou",
    "me passou": "Iniciou",
    "me passaram": "Iniciou",
    "me receitou": "Iniciou",
    "me receitaram": "Iniciou",
    "me deu": "Iniciou",
    "me deram": "Iniciou",
    "trocaram": "Trocou",
    "trocou": "Trocou",
    "mudou": "Trocou",
    "troquei": "Trocou",
    "mudaram": "Trocou",
    "aumentaram": "Aumentou",
    "aumentou": "Aumentou",
    "diminuiram": "Reduziu",
    "reduziram": "Reduziu",
    "reduziu": "Reduziu",
    "suspenderam": "Suspenderam",
    "suspendeu": "Suspenderam",
    "parei": "Parou",
    "suspendi": "Parou",
}
PATIENT_DECISION = re.compile(
    r"(?<![a-z])(" + "|".join(sorted(map(re.escape, _START), key=len, reverse=True)) + r")(?![a-z])"
)
NEGATION = {"nao", "nunca", "nem", "jamais", "sem"}
STOP_DRUG = re.compile(
    r"(?<![a-z])(suspender|suspenda|suspendo|parar|pare|para de tomar|interromper|interrompa|tirar|tire|retirar|retire|"
    r"cortar|corte|evitar|evite)(?![a-z])"
)
CONDUCT = re.compile(
    r"(?<![a-z])(vou|vamos|iremos|irei|precisamos|precisa|temos que|tem que|podemos|posso|quero|devemos|deve)\s+"
    r"(?:(?:te|lhe|a|o|ja|tambem|entao|ate|bem|com)\s+){0,3}"
    r"(ajustar|suspender|trocar|mudar|reduzir|diminuir|aumentar|retirar|tirar|manter|parar|interromper|iniciar|"
    r"comecar|corrigir|rever|revisar|reavaliar|orientar|monitorar|controlar|encaminhar|investigar)(?![a-z])"
)
MED_VERBS = {
    "ajustar",
    "suspender",
    "trocar",
    "mudar",
    "reduzir",
    "diminuir",
    "aumentar",
    "retirar",
    "tirar",
    "manter",
    "parar",
    "interromper",
    "iniciar",
    "comecar",
    "corrigir",
    "rever",
    "revisar",
}
MED_CONTEXT = re.compile(r"(?<![a-z])(remedio|medicamento|medicacao|dose|comprimido|receita|tomar|tomando|insulina)")
EXAM_ASK = re.compile(
    r"(pedir|pedido|pedirei|solicit\w*|mandar|vou mandar|repetir|repetiremos|colher|coletar|realizar|fazer)[^.?!]{0,50}"
    r"(exame|hemograma|ecg|eletro|ultrassom|raio|tomografia|ressonancia|creatinina|sangue|urina|colesterol|hemoglobina|"
    r"glicada|eletrocardiograma|laboratorio)"
)
ORIENT = re.compile(
    r"(?<![a-z])(evit[ae]|oriento|recomendo|lembr[ae]|procur[ae]|mantenh[ae]|beb[ae]|tome|faca|caminh[ae]|"
    r"nao use|nao tome|repouso|dieta|se alimente|coma |hidrate)"
)
RETURN = re.compile(
    r"(retorno|retornar|retorne|voltar|volte|volta em|reavalia|proxima consulta|me procure|nos vemos|"
    r"em \d+ (dias|semanas|meses)|daqui a \d+)"
)
VERB_LABEL = {
    "passar": "Prescrever",
    "prescrever": "Prescrever",
    "receitar": "Prescrever",
    "dar": "Prescrever",
    "iniciar": "Iniciar",
    "comecar": "Iniciar",
    "introduzir": "Iniciar",
    "colocar": "Iniciar",
    "reiniciar": "Reiniciar",
    "retomar": "Retomar",
    "trocar": "Trocar para",
    "mudar": "Mudar para",
    "substituir": "Substituir por",
    "aumentar": "Aumentar",
    "dobrar": "Dobrar",
    "reforcar": "Reforçar",
    "associar": "Associar",
    "adicionar": "Associar",
    "acrescentar": "Associar",
    "incluir": "Incluir",
    "indicar": "Indicar",
    "ministrar": "Administrar",
    "usar": "Usar",
    "tentar": "Tentar",
}


def clock(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCES.split(text.strip()) if s.strip()]


def _trim(text: str, size: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 1].rstrip() + "…"


def _med_context(segments: list[Segment], n: int) -> bool:
    """A fala (ou as duas anteriores) fala de remédio: o "vamos ajustar" do médico é conduta sobre medicamento."""
    for s in segments:
        if n - 2 <= s.n <= n:
            t = f" {norm(s.texto)} "
            if _drug_hits(t.strip()) or any(w in t for w in CLASS_WORDS) or MED_CONTEXT.search(t):
                return True
    return False


def findings(segments: list[Segment]) -> list[dict]:
    """Achados do exame dito pelo profissional (PA, FC, ausculta, abdome, edema…), sem perguntas."""
    out = []
    for s in segments:
        if s.falante not in TEAM:
            continue
        for sent in sentences(s.texto):
            if is_question(sent):
                continue
            t = norm(sent)
            for label, pattern, physical in FINDINGS:
                if pattern.search(t):
                    out.append({"n": s.n, "t": s.t, "rotulo": label, "texto": _trim(sent), "exame_fisico": physical})
    return out


def _patient_decisions(s: Segment) -> list[tuple[str, str]]:
    """(verbo, termo) do que o paciente diz que começou, trocou ou parou ("comecei aquele diurético")."""
    out: list[tuple[str, str]] = []
    for sent in sentences(s.texto):
        if is_question(sent):
            continue
        t = f" {norm(sent)} "
        for m in PATIENT_DECISION.finditer(t):
            if NEGATION & set(t[: m.start()].split()[-3:]):
                continue
            rest = t[m.end() :]
            clause = re.split(r"[.,;:!?]", rest, maxsplit=1)[0]
            terms = [h[3] for h in _drug_hits(clause.strip())] or [
                label for key, label in CLASS_WORDS.items() if f" {key}" in f" {clause}"
            ]
            if terms:
                out.append((_START[m.group(1)], terms[0]))
    return out


def _team_events(s: Segment, segments: list[Segment]) -> list[tuple[str, str, str]]:
    """(tipo, texto, chave) das decisões e orientações ditas pelo profissional na fala."""
    out: list[tuple[str, str, str]] = []
    for sent in sentences(s.texto):
        if is_question(sent):
            continue
        t = f" {norm(sent)} "
        for p in planned_drugs(sent):
            verb = VERB_LABEL.get(p.verb.removesuffix("r"), VERB_LABEL.get(p.verb, "Prescrever"))
            out.append(("medicamento", f"{verb} {p.term}", "prescricao"))
        stop = STOP_DRUG.search(t)
        if stop:
            for _, _, _, term in _drug_hits(t.strip()):
                out.append(("medicamento", f"Suspender ou evitar {term}", "suspensao"))
        if EXAM_ASK.search(t):
            out.append(("exame", f"Exame pedido: {_trim(sent, 140)}", "exame"))
        if RETURN.search(t):
            out.append(("retorno", f"Retorno: {_trim(sent, 140)}", "retorno"))
        if ORIENT.search(t):
            out.append(("orientacao", f"Orientação: {_trim(sent, 140)}", "orientacao"))
        m = CONDUCT.search(t)
        if m:
            verb = m.group(2)
            kind = "medicamento" if verb in MED_VERBS and _med_context(segments, s.n) else "orientacao"
            out.append((kind, f"Conduta: {_trim(s.texto)}", "conduta"))
    return out


def build(segments: list[Segment], ins: Insights) -> list[dict]:
    """Eventos tipados da consulta em ordem de fala: {n, quando, texto, refs, tipo, falante, chave}."""
    by_n = {s.n: s for s in segments}
    events: list[dict] = []

    def add(n: int, tipo: str, texto: str, chave: str = "") -> None:
        s = by_n.get(n)
        if s is None:
            return
        item = {
            "n": n,
            "quando": clock(s.t),
            "texto": texto,
            "refs": [fala_ref(n)],
            "tipo": tipo,
            "falante": s.falante,
            "chave": chave,
        }
        if not any(e["n"] == n and e["tipo"] == tipo and e["texto"] == texto for e in events):
            events.append(item)

    for tag in ins.queixas:
        n = int(tag.refs[0].split("/")[1])
        label = tag.rotulo.lower()
        add(
            n,
            "queixa" if tag.status == "presente" else "negativa",
            f"Relata {label}" if tag.status == "presente" else f"Nega {label}",
        )
    for emo in ins.emocoes:
        add(int(emo.refs[-1].split("/")[1]), "emocao", f"{emo.rotulo} ({emo.quem})")
    for f in findings(segments):
        add(f["n"], "achado", f"{f['rotulo']} (verbalizado): {f['texto']}", "achado")
    for s in segments:
        if s.falante in PATIENT:
            for verb, term in _patient_decisions(s):
                add(s.n, "medicamento", f"{verb} {term}", "relato")
        elif s.falante in TEAM:
            team = _team_events(s, segments)
            specific = {t for t, _, k in team if k != "conduta"}
            for tipo, texto, chave in team:
                if chave == "conduta" and tipo in specific:
                    continue  # o evento específico (prescrição, exame, retorno) já diz a conduta
                add(s.n, tipo, texto, chave)
    for d in ins.pedidos:
        if d.tipo == "exame" and d.fonte_pedido == "fala":
            n = next((int(r.split("/")[1]) for r in d.refs if r.startswith("Fala/")), None)
            if n is not None and not any(e["n"] == n and e["tipo"] == "exame" for e in events):
                add(n, "exame", f"Exame pedido: {d.item}", "exame")
    order = {
        "queixa": 0,
        "negativa": 1,
        "achado": 2,
        "medicamento": 3,
        "exame": 4,
        "orientacao": 5,
        "retorno": 6,
        "emocao": 7,
    }
    events.sort(key=lambda e: (e["n"], order[e["tipo"]]))
    return events


def conducts(events: list[dict]) -> list[dict]:
    """O que o profissional decidiu falando (para o Plano): medicamento, exame, orientação e retorno ditos por ele."""
    return [
        e for e in events if e["falante"] in TEAM and e["tipo"] in ("medicamento", "exame", "orientacao", "retorno")
    ]
