"""Camada de IA da consulta: Claude complementa os Smart Apps determinísticos, redige a nota e traduz falas.

Fluxo de segurança (igual aos pilares): dados pseudonimizados antes do envio → saída estruturada (JSON Schema) →
validação de `refs` (item sem fonte válida é descartado e registrado) → mescla com as regras, que nunca são
removidas pela IA.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from app.consult.engine import Engine
from app.consult.knowledge import SPECIALTIES, SPECIALTY_FOCUS, emotions
from app.consult.schemas import (
    LANGUAGE_LABELS,
    Anamnese,
    CidSuggestion,
    Confidence,
    ConsultNote,
    Emotion,
    Evidence,
    Highlight,
    HistoryItem,
    Hypothesis,
    Insights,
    MedSuggestion,
    NoteSection,
    Priority,
    Question,
    ReportField,
    Segment,
    Severity,
    Summary,
    SurgeryItem,
    Tag,
    fala_ref,
)
from app.consult.text import norm
from app.llm.client import generate_structured
from app.llm.pseudonymize import Pseudonymizer

KIND_LABEL = {"consulta": "consulta", "visita": "visita à beira do leito", "cirurgia": "ditado de relatório cirúrgico"}
EmotionKey = Literal["ansiedade", "preocupacao", "tristeza", "frustracao", "confusao", "dor", "alivio", "confianca"]


# ---------- schemas de saída do LLM ----------
class AiTag(BaseModel):
    rotulo: str
    status: Literal["presente", "negado"]
    refs: list[str]


class AiEmotion(BaseModel):
    emocao: EmotionKey
    quem: Literal["paciente", "acompanhante"]
    intensidade: Literal["baixa", "media", "alta"]
    trecho: str
    abordagem: str
    refs: list[str]


class AiQuestion(BaseModel):
    pergunta: str
    motivo: str
    prioridade: Priority
    refs: list[str]


class AiHypothesis(BaseModel):
    hipotese: str
    cid: str
    confianca: Confidence
    a_favor: list[Evidence]
    contra: list[Evidence]
    proximos_passos: str
    sinais_de_alarme: str
    refs: list[str]


class AiMedSuggestion(BaseModel):
    texto: str
    tipo: Literal["ajuste_dose", "alternativa", "suspender", "interacao", "registrar", "adesao", "alergia"]
    severidade: Severity
    refs: list[str]


class AiText(BaseModel):
    texto: str
    refs: list[str]


class AiSpeaker(BaseModel):
    n: int
    falante: Literal["medico", "paciente", "acompanhante"]


class ConsultLiveOutput(BaseModel):
    resumo: str
    destaques: list[Highlight]
    queixas: list[AiTag]
    emocoes: list[AiEmotion]
    perguntas: list[AiQuestion]
    hipoteses: list[AiHypothesis]
    sugestoes_medicamentos: list[AiMedSuggestion]
    cirurgia: list[AiText]
    historico_relatado: list[AiText]
    anamnese: Anamnese
    falantes: list[AiSpeaker]


class AiTranslation(BaseModel):
    n: int
    traducao: str


class TranslationOutput(BaseModel):
    traducoes: list[AiTranslation]


class AiField(BaseModel):
    campo: str
    texto: str
    refs: list[str]


class SurgicalOutput(BaseModel):
    campos: list[AiField]
    cids: list[CidSuggestion]


# ---------- entrada pseudonimizada ----------
def fmt_clock(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def transcript_text(segments: list[Segment], pseudo: Pseudonymizer) -> str:
    return (
        "\n".join(f"[{fala_ref(s.n)} · {s.falante} · {fmt_clock(s.t)}] {pseudo.text(s.texto)}" for s in segments)
        or "(sem falas ainda)"
    )


def chart_payload(engine: Engine) -> dict:
    c = engine.chart
    vitals = [
        {"ref": e.source_ref, "quando": e.timestamp.isoformat(), "texto": e.summary}
        for e in c.events
        if e.kind == "vital" and (c.now - e.timestamp).days <= 1
    ]
    notes = [
        {"ref": e.source_ref, "quando": e.timestamp.isoformat(), "texto": (e.text or "")[:700]}
        for e in c.notes_text(400)[-3:]
    ]
    return {
        "idade": engine.r.age,
        "sexo": engine.r.sex,
        "tfg_estimada": round(c.egfr[0], 1) if c.egfr else None,
        "diagnosticos": [{"ref": h.refs[0], "texto": h.texto} for h in c.history()],
        "alergias": [{"ref": a.source_ref, "texto": a.summary} for a in c.ctx.alergias]
        or ("nega alergias conhecidas" if c.ctx.nega_alergias else "sem registro"),
        "medicamentos": [{"ref": m.refs[0], "texto": f"{m.posologia} [{m.status}]"} for m in c.meds([])],
        "exames": [
            {"ref": e.refs[0], "texto": f"{e.exame}: {e.valor} em {e.data} ({e.status})", "tendencia": e.tendencia}
            for e in c.exams()
        ],
        "sinais_vitais_24h": vitals,
        "evolucoes_recentes": notes,
    }


def deterministic_payload(ins: Insights) -> dict:
    return {
        "perguntas_ja_sugeridas": [q.pergunta for q in ins.perguntas],
        "perguntas_ja_abordadas": [q.pergunta for q in ins.perguntas_abordadas],
        "hipoteses_por_regra": [f"{h.hipotese} ({h.cid}) — confiança {h.confianca}" for h in ins.hipoteses],
        "alertas_de_seguranca": [{"texto": m.texto, "refs": m.refs} for m in ins.sugestoes_medicamentos],
        "protocolo_pendente": [f"{p.protocolo}: {p.item}" for p in ins.protocolos if p.status != "em_dia"],
    }


# ---------- validação de refs ----------
class RefValidator:
    def __init__(self, valid: set[str]):
        self.valid = valid
        self.dropped: list[dict] = []

    def ok(self, refs: list[str]) -> bool:
        return bool(refs) and all(r in self.valid for r in refs)

    def keep(self, items: list, where: str) -> list:
        out = []
        for it in items:
            if self.ok(it.refs):
                out.append(it)
            else:
                self.dropped.append(
                    {
                        "onde": where,
                        "texto": getattr(it, "texto", None)
                        or getattr(it, "pergunta", None)
                        or getattr(it, "hipotese", None)
                        or getattr(it, "rotulo", ""),
                        "refs_invalidas": [r for r in it.refs if r not in self.valid],
                    }
                )
        return out


def _similar(a: str, b: str) -> bool:
    ta, tb = set(norm(a).split()), set(norm(b).split())
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.5


# ---------- mescla IA + regras ----------
def merge_live(base: Insights, ai: ConsultLiveOutput, valid: set[str], segments: list[Segment]) -> Insights:
    v = RefValidator(valid)
    labels = {d.key: d.label for d in emotions()}
    approach = {d.key: d.approach for d in emotions()}
    out = base.model_copy(deep=True)
    out.modo = "ia"

    tags = [Tag(rotulo=t.rotulo, status=t.status, refs=t.refs, origem="ia") for t in v.keep(ai.queixas, "queixas")]
    out.queixas = tags + [t for t in base.queixas if not any(_similar(t.rotulo, x.rotulo) for x in tags)]

    emo = [
        Emotion(
            emocao=e.emocao,
            rotulo=labels.get(e.emocao, e.emocao),
            quem=e.quem,
            intensidade=e.intensidade,
            trecho=e.trecho,
            abordagem=e.abordagem or approach.get(e.emocao, ""),
            refs=e.refs,
            origem="ia",
        )
        for e in v.keep(ai.emocoes, "emocoes")
        if all(r.startswith("Fala/") for r in e.refs)
    ]
    out.emocoes = emo + [e for e in base.emocoes if not any(x.emocao == e.emocao and x.quem == e.quem for x in emo)]

    covered = [q.pergunta for q in base.perguntas_abordadas]
    questions = [
        q
        for q in v.keep(ai.perguntas, "perguntas")
        if not any(_similar(q.pergunta, b.pergunta) for b in base.perguntas)
        and not any(_similar(q.pergunta, c) for c in covered)
    ]
    rank = {"alta": 0, "media": 1, "baixa": 2}
    ai_questions = [Question(**q.model_dump(), origem="ia") for q in questions]
    out.perguntas = sorted(base.perguntas + ai_questions, key=lambda q: rank[q.prioridade])[:10]

    hyps = []
    for h in ai.hipoteses:
        pro = [e for e in h.a_favor if v.ok(e.refs)]
        con = [e for e in h.contra if v.ok(e.refs)]
        refs = [r for r in h.refs if r in valid] or [r for e in pro for r in e.refs]
        if not pro or not refs:
            v.dropped.append({"onde": "hipoteses", "texto": h.hipotese, "refs_invalidas": h.refs})
            continue
        hyps.append(Hypothesis(**(h.model_dump() | {"a_favor": pro, "contra": con, "refs": refs}), origem="ia"))
    rule_cids = {h.cid[:3] for h in base.hipoteses}
    out.hipoteses = base.hipoteses + [h for h in hyps if h.cid[:3] not in rule_cids]

    meds = [MedSuggestion(**m.model_dump(), origem="ia") for m in v.keep(ai.sugestoes_medicamentos, "medicamentos")]
    out.sugestoes_medicamentos = base.sugestoes_medicamentos + [
        m for m in meds if not any(_similar(m.texto, b.texto) for b in base.sugestoes_medicamentos)
    ]
    out.cirurgia = base.cirurgia + [
        SurgeryItem(texto=c.texto, tipo="checagem", refs=c.refs, origem="ia") for c in v.keep(ai.cirurgia, "cirurgia")
    ]
    out.historico = base.historico + [
        HistoryItem(texto=h.texto, tipo="relatado", refs=h.refs, origem="ia")
        for h in v.keep(ai.historico_relatado, "historico")
    ]
    highlights = v.keep(ai.destaques, "destaques")
    rule_highlights = base.resumo.destaques if base.resumo else []
    out.resumo = Summary(
        texto=ai.resumo or (base.resumo.texto if base.resumo else ""),
        destaques=rule_highlights
        + [h for h in highlights if not any(_similar(h.texto, r.texto) for r in rule_highlights)],
    )
    anamnese = ai.anamnese.model_copy(update={"refs": [r for r in ai.anamnese.refs if r in valid]})
    out.anamnese = anamnese
    out.descartados = v.dropped
    return out


def mock_live(base: Insights) -> dict:
    """Modo mock: a "IA" devolve resumo e anamnese das regras (sem inventar itens)."""
    return {
        "resumo": base.resumo.texto if base.resumo else "",
        "destaques": [],
        "queixas": [],
        "emocoes": [],
        "perguntas": [],
        "hipoteses": [],
        "sugestoes_medicamentos": [],
        "cirurgia": [],
        "historico_relatado": [],
        "anamnese": base.anamnese.model_dump()
        if base.anamnese
        else Anamnese(
            queixa_principal="",
            hda="",
            antecedentes=[],
            medicacoes_em_uso=[],
            alergias=[],
            habitos_e_contexto=[],
            revisao_de_sistemas=[],
            refs=[],
        ).model_dump(),
        "falantes": [],
    }


def run_live(engine: Engine, base: Insights, pseudo: Pseudonymizer, valid: set[str]):
    variables = {
        "tipo": KIND_LABEL.get(engine.kind, engine.kind),
        "especialidade": SPECIALTIES.get(engine.specialty, engine.specialty),
        "foco": SPECIALTY_FOCUS.get(engine.specialty, ""),
        "prontuario": pseudo.apply(chart_payload(engine)),
        "deterministico": pseudo.apply(deterministic_payload(base)),
        "transcricao": transcript_text(engine.segments, pseudo),
    }
    result = generate_structured(
        "consult_live", variables, ConsultLiveOutput, mock=lambda _v: mock_live(base), effort="low"
    )
    merged = merge_live(base, result.output, valid, engine.segments)
    if result.mode == "mock":
        merged.modo = "regras"
    return merged, result


# ---------- nota final ----------
def mock_note(engine: Engine, ins: Insights, language: str) -> dict:
    a = ins.anamnese
    first_refs = a.refs[:1] if a and a.refs else []
    hyps = ins.hipoteses[:4]
    vitals = [e for e in engine.chart.events if e.kind == "vital" and (engine.chart.now - e.timestamp).days <= 1]
    abnormal = [e for e in ins.exames if e.status != "normal"]
    plan = [m for m in ins.sugestoes_medicamentos if m.severidade in ("critico", "atencao")]
    pending_protocol = [p for p in ins.protocolos if p.status in ("sem_registro", "pendente")]
    cids = [{"codigo": h.cid, "descricao": h.hipotese, "tipo": "hipotese", "refs": h.refs[:3]} for h in hyps] + [
        {"codigo": icd, "descricao": text, "tipo": "secundario", "refs": [f"Condition/{c['id']}"]}
        for icd, text, c in engine.chart.conditions
        if icd
    ]
    chrono = []
    for t in ins.queixas:
        if t.status == "presente":
            n = int(t.refs[0].split("/")[1])
            seg = next(s for s in engine.segments if s.n == n)
            chrono.append({"quando": fmt_clock(seg.t), "texto": f"Relata {t.rotulo.lower()}", "refs": t.refs[:1]})
    for e in ins.emocoes:
        n = int(e.refs[-1].split("/")[1])
        seg = next(s for s in engine.segments if s.n == n)
        chrono.append({"quando": fmt_clock(seg.t), "texto": f"{e.rotulo} ({e.quem})", "refs": e.refs[-1:]})
    chrono.sort(key=lambda c: c["quando"])
    pendencias = (
        [
            {"texto": f"Registrar alergia relatada: {al.alergeno}", "refs": al.refs}
            for al in ins.alergias
            if al.fonte == "relatado"
        ]
        + [
            {"texto": f"Registrar medicamento em uso por conta própria: {m.medicamento}", "refs": m.refs}
            for m in ins.medicamentos
            if m.status == "relatado"
        ]
        + [
            {"texto": f"Protocolo {p.protocolo}: {p.item} ({p.status.replace('_', ' ')})", "refs": p.refs}
            for p in pending_protocol
        ]
    )
    tips = [f"{e.exame}: {e.explicacao}" for e in abnormal[:3]]
    tips += ["Tome os remédios nos horários combinados e não use remédios por conta própria sem falar com a equipe."]
    if any(m.tipo == "suspender" for m in plan):
        tips.append("Alguns remédios podem ser trocados ou suspensos: siga a receita nova que a equipe vai entregar.")
    if language != "pt-BR":
        tips.insert(0, f"[Tradução para {LANGUAGE_LABELS.get(language, language)} disponível com o LLM real.]")
    no_record = "Sem registro na consulta."
    return {
        "queixa_principal": {"texto": a.queixa_principal if a else no_record, "refs": first_refs},
        "hda": {"texto": a.hda if a else no_record, "refs": a.refs if a else []},
        "antecedentes": {
            "texto": "; ".join(h.texto for h in ins.historico if h.tipo in ("diagnostico", "cirurgia", "familiar"))
            or no_record,
            "refs": [r for h in ins.historico if h.tipo in ("diagnostico", "cirurgia", "familiar") for r in h.refs],
        },
        "medicacoes": {
            "texto": "; ".join(
                f"{m.posologia}" if m.status != "relatado" else f"{m.medicamento} (relatado)"
                for m in ins.medicamentos
                if m.status != "suspenso"
            )
            or no_record,
            "refs": [r for m in ins.medicamentos if m.status != "suspenso" for r in m.refs],
        },
        "alergias": {
            "texto": "; ".join(
                f"{al.alergeno} ({'prontuário' if al.fonte == 'prontuario' else 'relatada na consulta'})"
                for al in ins.alergias
            )
            or ("Nega alergias conhecidas." if engine.chart.ctx.nega_alergias else no_record),
            "refs": [r for al in ins.alergias for r in al.refs],
        },
        "exame_fisico": {
            "texto": "; ".join(e.summary for e in vitals) or no_record,
            "refs": [e.source_ref for e in vitals],
        },
        "exames_complementares": {
            "texto": "; ".join(
                f"{e.exame} {e.valor} ({e.data}{', ' + e.tendencia if e.tendencia else ''})" for e in ins.exames
            )
            or no_record,
            "refs": [r for e in ins.exames for r in e.refs[:1]],
        },
        "avaliacao": {
            "texto": "Hipóteses: " + "; ".join(f"{h.hipotese} ({h.cid}, confiança {h.confianca})" for h in hyps)
            if hyps
            else no_record,
            "refs": [r for h in hyps for r in h.refs[:2]],
        },
        "plano": {
            "texto": "; ".join(f"{m.texto} (a confirmar)" for m in plan) or no_record,
            "refs": [r for m in plan for r in m.refs],
        },
        "cids": cids,
        "cronologia": chrono,
        "pendencias": pendencias,
        "orientacoes_paciente": tips,
    }


def run_note(engine: Engine, ins: Insights, pseudo: Pseudonymizer, valid: set[str], language: str):
    variables = {
        "tipo": KIND_LABEL.get(engine.kind, engine.kind),
        "especialidade": SPECIALTIES.get(engine.specialty, engine.specialty),
        "idioma": LANGUAGE_LABELS.get(language, language),
        "prontuario": pseudo.apply(chart_payload(engine)),
        "achados": pseudo.apply(
            {
                "queixas": [t.model_dump(include={"rotulo", "status", "refs"}) for t in ins.queixas],
                "hipoteses": [h.model_dump(include={"hipotese", "cid", "confianca", "refs"}) for h in ins.hipoteses],
                "seguranca_medicamentos": [
                    m.model_dump(include={"texto", "severidade", "refs"}) for m in ins.sugestoes_medicamentos
                ],
                "alergias": [a.model_dump(include={"alergeno", "fonte", "refs"}) for a in ins.alergias],
                "protocolos": [p.model_dump(include={"protocolo", "item", "status", "refs"}) for p in ins.protocolos],
                "emocoes": [e.model_dump(include={"rotulo", "quem", "refs"}) for e in ins.emocoes],
            }
        ),
        "transcricao": transcript_text(engine.segments, pseudo),
    }
    result = generate_structured(
        "consult_note", variables, ConsultNote, mock=lambda _v: mock_note(engine, ins, language), effort="medium"
    )
    note = result.output
    v = RefValidator(valid)
    for name in (
        "queixa_principal",
        "hda",
        "antecedentes",
        "medicacoes",
        "alergias",
        "exame_fisico",
        "exames_complementares",
        "avaliacao",
        "plano",
    ):
        section: NoteSection = getattr(note, name)
        section.refs = [r for r in section.refs if r in valid]
    note.cids = v.keep(note.cids, "cids")
    note.cronologia = [c for c in note.cronologia if all(r in valid for r in c.refs)]
    note.pendencias = v.keep(note.pendencias, "pendencias")
    return note, result, v.dropped


# ---------- tradução ----------
def run_translation(segments: list[Segment], targets: dict[int, str], pseudo: Pseudonymizer) -> dict[int, str]:
    items = [
        {
            "n": s.n,
            "de": s.idioma,
            "para": LANGUAGE_LABELS.get(targets[s.n], targets[s.n]),
            "texto": pseudo.text(s.texto),
        }
        for s in segments
        if s.n in targets
    ]
    if not items:
        return {}
    result = generate_structured(
        "consult_translate",
        {"falas": json.dumps(items, ensure_ascii=False)},
        TranslationOutput,
        mock=lambda _v: {
            "traducoes": [
                {"n": i["n"], "traducao": f"[{i['para']} — tradução com LLM real] {i['texto']}"} for i in items
            ]
        },
        effort="low",
    )
    return {t.n: t.traducao for t in result.output.traducoes if t.n in targets}


# ---------- relatório cirúrgico ----------
def run_surgical(engine: Engine, ins: Insights, pseudo: Pseudonymizer, valid: set[str]):
    report = ins.relatorio_cirurgico
    fields = [f.model_dump(include={"campo", "rotulo", "texto", "refs"}) for f in report.campos]
    result = generate_structured(
        "surgical_report",
        {
            "prontuario": pseudo.apply(chart_payload(engine)),
            "campos": pseudo.apply(fields),
            "transcricao": transcript_text(engine.segments, pseudo),
        },
        SurgicalOutput,
        mock=lambda _v: {
            "campos": [{"campo": f["campo"], "texto": f["texto"], "refs": f["refs"]} for f in fields],
            "cids": [c.model_dump() for c in report.cids],
        },
        effort="low",
    )
    by_key = {f.campo: f for f in result.output.campos}
    v = RefValidator(valid)
    new_fields: list[ReportField] = []
    for f in report.campos:
        ai = by_key.get(f.campo)
        if ai and ai.texto.strip() and f.texto and all(r in valid for r in ai.refs):
            new_fields.append(f.model_copy(update={"texto": ai.texto, "refs": ai.refs or f.refs}))
        else:
            new_fields.append(f)  # campo não ditado continua vazio: a pendência é do cirurgião, não da IA
    cids = v.keep(result.output.cids, "cids") or report.cids
    out = ins.model_copy(deep=True)
    out.relatorio_cirurgico = report.model_copy(update={"campos": new_fields, "cids": cids})
    out.modo = "ia" if result.mode == "real" else "regras"
    out.descartados = v.dropped
    return out, result
