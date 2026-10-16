"""Camada de IA da consulta: Claude complementa os Smart Apps determinísticos, redige a nota e traduz falas.

Fluxo de segurança (igual aos pilares): dados pseudonimizados antes do envio → saída estruturada (JSON Schema) →
validação de `refs` (item sem fonte válida é descartado e registrado) → mescla com as regras, que nunca são
removidas pela IA.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel

from app.consult.cid import screen
from app.consult.engine import PATIENT_SIDE, TEAM_SIDE, Engine
from app.consult.knowledge import (
    SPECIALTIES,
    SPECIALTY_FOCUS,
    allergy_defs,
    anamnesis_question_ids,
    emotions,
    semiology,
    semiology_questions,
)
from app.consult.orders import parse_posology
from app.consult.schemas import (
    LANGUAGE_LABELS,
    AllergyInfo,
    Anamnese,
    CampoEspecialidade,
    CampoSemio,
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
    OrderDraft,
    Priority,
    QueixaDetalhada,
    Question,
    ReportField,
    Segment,
    Severity,
    Summary,
    SurgeryItem,
    Tag,
    fala_ref,
)
from app.consult.text import contains, keyword_status, norm
from app.llm.client import LLMResult, generate_structured
from app.llm.pseudonymize import Pseudonymizer
from app.reference import icd_entry

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


class AiAbordada(BaseModel):
    """Pergunta sugerida que o profissional já fez, mesmo com outras palavras (paráfrase)."""

    id: str  # id de `perguntas_ja_sugeridas`
    refs: list[str]  # falas do PROFISSIONAL em que perguntou
    resposta_refs: list[str]  # fala do paciente que respondeu (vazio se ainda não respondeu)


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
    tipo: Literal[
        "ajuste_dose",
        "alternativa",
        "suspender",
        "interacao",
        "registrar",
        "adesao",
        "alergia",
        "efeito_adverso",
        "alto_alerta",
    ]
    severidade: Severity
    refs: list[str]


class AiAlergia(BaseModel):
    """Alergia que o paciente (ou o acompanhante) contou e as regras não reconheceram: entra como "IA — confirmar"."""

    alergeno: str
    reacao: str
    criticidade: Literal["alta", "baixa", "indeterminada"]
    refs: list[str]  # falas do paciente/acompanhante em que ele contou


class AiText(BaseModel):
    texto: str
    refs: list[str]


class AiSpeaker(BaseModel):
    n: int
    falante: Literal["medico", "paciente", "acompanhante"]


class ConversaOutput(BaseModel):
    """Smart Apps da conversa (1ª chamada)."""

    resumo: str
    destaques: list[Highlight]
    queixas: list[AiTag]
    emocoes: list[AiEmotion]
    perguntas: list[AiQuestion]
    falantes: list[AiSpeaker]
    perguntas_abordadas: list[AiAbordada] = []


class AiCampo(BaseModel):
    """Um campo semiológico dito: só entra o que tem valor e fonte (o vazio é omitido)."""

    campo: Literal[
        "inicio_duracao", "localizacao", "intensidade", "piora", "melhora", "frequencia", "associados", "nega"
    ]
    valor: str
    refs: list[str]


class AiQueixa(BaseModel):
    chave: str
    rotulo: str
    campos: list[AiCampo]


class AiCampoEspecialidade(BaseModel):
    campo: str
    valor: str
    refs: list[str]


class AiAnamnese(BaseModel):
    """A `Anamnese` como a IA a devolve: a semiologia vai em lista de campos, e não em um objeto por campo, para a
    gramática da saída estruturada continuar pequena. `anamnese_from_ai` a converte na `Anamnese` das regras."""

    queixa_principal: str
    hda: str
    antecedentes: list[str]
    medicacoes_em_uso: list[str]
    alergias: list[str]
    habitos_e_contexto: list[str]
    revisao_de_sistemas: list[str]
    refs: list[str]
    queixas_detalhadas: list[AiQueixa] = []
    campos_especialidade: list[AiCampoEspecialidade] = []


class AiPedido(BaseModel):
    """Posologia e justificativa que a IA completa num rascunho que as regras JÁ detectaram (nunca cria rascunho)."""

    id: str  # id de `pedidos_em_rascunho`
    dose: str = ""
    via: str = ""
    frequencia: str = ""
    duracao: str = ""
    justificativa: str = ""
    refs: list[str]


class ClinicaOutput(BaseModel):
    """Smart Apps clínicos (2ª chamada, em paralelo)."""

    hipoteses: list[AiHypothesis]
    sugestoes_medicamentos: list[AiMedSuggestion]
    cirurgia: list[AiText]
    historico_relatado: list[AiText]
    anamnese: AiAnamnese
    alergias_relatadas: list[AiAlergia] = []
    pedidos_ia: list[AiPedido] = []


class ConsultLiveOutput(BaseModel):
    """União das duas chamadas (formato guardado e re-mesclado a cada fala)."""

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
    perguntas_abordadas: list[AiAbordada] = []
    alergias_relatadas: list[AiAlergia] = []
    pedidos_ia: list[AiPedido] = []


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


def _ref_order(ref: str) -> int:
    tail = ref.rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def anamnese_from_ai(a: AiAnamnese) -> Anamnese:
    labels = {f.key: f.label for fields in semiology().specialty_fields.values() for f in fields}
    detailed = []
    for q in a.queixas_detalhadas:
        fields: dict[str, CampoSemio] = {}
        for c in q.campos:
            if c.valor.strip():
                fields.setdefault(c.campo, CampoSemio(valor=c.valor, refs=c.refs))
        refs = sorted({r for f in fields.values() for r in f.refs}, key=_ref_order)
        detailed.append(QueixaDetalhada(chave=q.chave, rotulo=q.rotulo, refs=refs, **fields))
    extras = [
        CampoEspecialidade(campo=c.campo, rotulo=labels.get(c.campo, c.campo), valor=c.valor, refs=c.refs)
        for c in a.campos_especialidade
    ]
    base = a.model_dump(exclude={"queixas_detalhadas", "campos_especialidade"})
    return Anamnese(**base, queixas_detalhadas=detailed, campos_especialidade=extras)


def anamnese_to_ai(a: Anamnese) -> dict:
    """A anamnese das regras no formato que a IA devolve (e que o modo mock devolve no lugar dela)."""
    base = a.model_dump(include=set(AiAnamnese.model_fields) - {"queixas_detalhadas", "campos_especialidade"})
    return base | {
        "queixas_detalhadas": [
            {
                "chave": q.chave,
                "rotulo": q.rotulo,
                "campos": [
                    {"campo": f, "valor": getattr(q, f).valor, "refs": getattr(q, f).refs}
                    for f in (*SEMIO_FIELDS, "nega")
                    if getattr(q, f).valor
                ],
            }
            for q in a.queixas_detalhadas
        ],
        "campos_especialidade": [{"campo": c.campo, "valor": c.valor, "refs": c.refs} for c in a.campos_especialidade],
    }


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
        "perguntas_ja_sugeridas": [{"id": q.id, "pergunta": q.pergunta} for q in ins.perguntas],
        "perguntas_ja_abordadas": [q.pergunta for q in ins.perguntas_abordadas],
        "hipoteses_por_regra": [f"{h.hipotese} ({h.cid}) — confiança {h.confianca}" for h in ins.hipoteses],
        "alertas_de_seguranca": [{"texto": m.texto, "refs": m.refs} for m in ins.sugestoes_medicamentos],
        # o que o paciente disse que usa ("relatado") ou que parou ("suspenso"); o negado nem entra
        "medicamentos_relatados": [
            {"medicamento": m.medicamento, "status": m.status, "refs": m.refs}
            for m in ins.medicamentos
            if any(r.startswith("Fala/") for r in m.refs)
        ],
        # rascunhos que as regras detectaram na fala: a IA só completa o que está vazio (posologia e justificativa)
        "pedidos_em_rascunho": [
            {
                "id": d.id,
                "item": d.item,
                "acao": d.acao,
                "dose": d.dose,
                "via": d.via,
                "frequencia": d.frequencia,
                "duracao": d.duracao,
                "justificativa": d.justificativa,
                "refs": d.refs,
            }
            for d in ins.pedidos
        ],
        "protocolo_pendente": [f"{p.protocolo}: {p.item}" for p in ins.protocolos if p.status != "em_dia"],
        # semiologia que as regras já leram da conversa (cada campo com as falas de onde veio) e o que ainda falta
        "anamnese_por_regra": {
            "queixa_principal": ins.anamnese.qp_principal,
            "queixa_principal_fixada": ins.anamnese.qp_fixada,
            "queixas_detalhadas": [
                q | {"faltam": d.faltam}
                for q, d in zip(
                    anamnese_to_ai(ins.anamnese)["queixas_detalhadas"], ins.anamnese.queixas_detalhadas, strict=True
                )
            ],
            "campos_especialidade": anamnese_to_ai(ins.anamnese)["campos_especialidade"],
        }
        if ins.anamnese
        else {},
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
                        or getattr(it, "alergeno", None)
                        or getattr(it, "rotulo", ""),
                        "refs_invalidas": [r for r in it.refs if r not in self.valid],
                    }
                )
        return out


MAX_LIST = 12  # perguntas sugeridas (regras + IA), fora as de semiologia, que ficam no fim
SEMIO_FIELDS = ("inicio_duracao", "localizacao", "intensidade", "piora", "melhora", "frequencia", "associados")


def _fala(ref: str, segments: list[Segment]) -> Segment | None:
    if not ref.startswith("Fala/"):
        return None
    return next((s for s in segments if s.n == int(ref.split("/")[1])), None)


def _paraphrased(base: Insights, ai: ConsultLiveOutput, v: RefValidator, segments: list[Segment]) -> list[Question]:
    """Perguntas sugeridas que a IA viu o profissional fazer com outras palavras. Só vale com o id de uma pergunta da
    lista e a fala do PROFISSIONAL (ref válida) em que ele perguntou; a resposta, se houver, é fala do paciente."""
    by_id = {q.id: q for q in base.perguntas if q.id}
    out: dict[str, Question] = {}
    for a in ai.perguntas_abordadas:
        asked = [_fala(r, segments) for r in a.refs]
        if a.id not in by_id or not v.ok(a.refs) or not all(s and s.falante in TEAM_SIDE for s in asked):
            v.dropped.append(
                {
                    "onde": "perguntas_abordadas",
                    "texto": a.id,
                    "refs_invalidas": [r for r in a.refs if r not in v.valid] or a.refs,
                }
            )
            continue
        replies = [_fala(r, segments) for r in a.resposta_refs if v.ok([r])]
        reply = next((s for s in replies if s and s.falante in PATIENT_SIDE and s.n > asked[0].n), None)
        text = reply.texto.strip() if reply else ""
        out.setdefault(
            a.id,
            by_id[a.id].model_copy(
                update={
                    "refs": a.refs[:2],
                    "resposta": f"{text[:160]}{'…' if len(text) > 160 else ''}",
                    "resposta_refs": [fala_ref(reply.n)] if reply else [],
                    "origem": "ia",
                }
            ),
        )
    return list(out.values())


def _merge_anamnese(
    rule: Anamnese | None, ai: Anamnese, v: RefValidator, valid: set[str], segments: list[Segment]
) -> Anamnese:
    """Texto livre da IA; nos campos semiológicos vale o da regra que tem fonte e o da IA entra onde a regra não tem."""
    out = ai.model_copy(update={"refs": [r for r in ai.refs if r in valid]})
    if rule is None:
        out.queixas_detalhadas = []
        out.campos_especialidade = []
        return out
    out.qp_principal, out.qp_fixada = rule.qp_principal, rule.qp_fixada
    if rule.qp_fixada or not out.queixa_principal.strip():  # a queixa principal que o profissional fixou não muda
        out.queixa_principal = rule.queixa_principal
    if out.hda.strip() in ("", "—"):
        out.hda = rule.hda

    def usable(field: CampoSemio) -> bool:
        """Campo com valor e fonte válida, e a fonte é fala de quem tem a queixa (paciente ou acompanhante)."""
        if not field.valor.strip() or not field.refs:
            return False
        if not all(r in valid for r in field.refs):
            return False
        spoken = [_fala(r, segments) for r in field.refs]
        return all(x is not None and x.falante in PATIENT_SIDE for x in spoken)

    def sync(q: QueixaDetalhada) -> QueixaDetalhada:
        q.faltam = [f for f in semiology().group_of(q.chave).fields if not getattr(q, f).valor]  # type: ignore[misc]
        return q

    merged: list[QueixaDetalhada] = []
    taken: set[int] = set()
    for r in rule.queixas_detalhadas:
        q = r.model_copy(deep=True)
        for i, a in enumerate(ai.queixas_detalhadas):
            if a.chave == r.chave or _similar(a.rotulo, r.rotulo):
                taken.add(i)
                for f in (*SEMIO_FIELDS, "nega"):
                    if not getattr(q, f).valor and usable(getattr(a, f)):
                        setattr(q, f, getattr(a, f))
        q.refs = sorted({*q.refs, *(x for f in SEMIO_FIELDS for x in getattr(q, f).refs)}, key=_ref_order)
        merged.append(sync(q))
    for i, a in enumerate(ai.queixas_detalhadas):  # queixa que só a IA viu: entra com os campos que têm fonte válida
        if i in taken:
            continue
        q = QueixaDetalhada(chave=a.chave, rotulo=a.rotulo, refs=[])
        for f in (*SEMIO_FIELDS, "nega"):
            if usable(getattr(a, f)):
                setattr(q, f, getattr(a, f))
        q.refs = sorted({x for f in (*SEMIO_FIELDS, "nega") for x in getattr(q, f).refs}, key=_ref_order)
        if q.refs:
            merged.append(sync(q))
        else:
            v.dropped.append({"onde": "anamnese", "texto": a.rotulo, "refs_invalidas": a.refs})
    out.queixas_detalhadas = merged
    ai_extra = {c.campo: c for c in ai.campos_especialidade}
    out.campos_especialidade = [
        c
        if c.valor or c.campo not in ai_extra or not usable(ai_extra[c.campo])
        else c.model_copy(update={"valor": ai_extra[c.campo].valor, "refs": ai_extra[c.campo].refs})
        for c in rule.campos_especialidade
    ]
    return out


def _similar(a: str, b: str) -> bool:
    ta, tb = set(norm(a).split()), set(norm(b).split())
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.5


# ---------- mescla IA + regras ----------
def merge_live(
    base: Insights, ai: ConsultLiveOutput, valid: set[str], segments: list[Segment], sex: str = ""
) -> Insights:
    v = RefValidator(valid)
    labels = {d.key: d.label for d in emotions()}
    approach = {d.key: d.approach for d in emotions()}
    out = base.model_copy(deep=True)
    out.modo = "ia"

    tags = [
        Tag(
            rotulo=t.rotulo,
            status=t.status,
            refs=t.refs,
            origem="ia",
            detalhe=next(
                (b.detalhe for b in base.queixas if b.status == t.status and _similar(b.rotulo, t.rotulo)), ""
            ),
        )
        for t in v.keep(ai.queixas, "queixas")
    ]
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

    moved = _paraphrased(base, ai, v, segments)
    moved_ids = {q.id for q in moved}
    kept = [q for q in base.perguntas if q.id not in moved_ids]
    covered = [q.pergunta for q in base.perguntas_abordadas + moved]
    questions = [
        q
        for q in v.keep(ai.perguntas, "perguntas")
        if not any(_similar(q.pergunta, b.pergunta) for b in kept) and not any(_similar(q.pergunta, c) for c in covered)
    ]
    rank = {"alta": 0, "media": 1, "baixa": 2}
    ai_questions = [Question(**q.model_dump(), origem="ia") for q in questions]
    # as de semiologia ficam no fim da lista, como nas regras; as sugeridas pela IA entram por prioridade
    semio_ids = semiology_questions()
    tail = [q for q in kept if q.id.split(":")[0] in anamnesis_question_ids()]
    head = [q for q in kept if q.id.split(":")[0] not in anamnesis_question_ids()]
    out.perguntas = sorted(head + ai_questions, key=lambda q: rank[q.prioridade])[:MAX_LIST] + tail
    out.perguntas_abordadas = base.perguntas_abordadas + moved

    hyps = []
    # CID que a tabela oficial não aceita (inexistente, causa externa, restrito ao outro sexo) não vira hipótese
    proposed, bad_cids = screen(ai.hipoteses, "hipoteses", sex, code="cid", label="hipotese", primary=True)
    v.dropped += bad_cids
    for h in proposed:
        pro = [e for e in h.a_favor if v.ok(e.refs)]
        con = [e for e in h.contra if v.ok(e.refs)]
        refs = [r for r in h.refs if r in valid] or [r for e in pro for r in e.refs]
        if not pro or not refs:
            v.dropped.append({"onde": "hipoteses", "texto": h.hipotese, "refs_invalidas": h.refs})
            continue
        entry = icd_entry(h.cid)
        hyps.append(
            Hypothesis(
                **(h.model_dump() | {"a_favor": pro, "contra": con, "refs": refs}),
                origem="ia",
                cid_descricao=entry.description if entry else "",
            )
        )
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
    out.anamnese = _merge_anamnese(base.anamnese, ai.anamnese, v, valid, segments)
    if out.anamnese:  # campo semiológico que a IA preencheu com fonte: a pergunta correspondente sai da lista
        known = {(q.chave, f) for q in out.anamnese.queixas_detalhadas for f in SEMIO_FIELDS if getattr(q, f).valor}

        def answered(q: Question) -> bool:
            row, _, chave = q.id.partition(":")
            return row in semio_ids and (chave, semio_ids[row]) in known

        out.perguntas = [q for q in out.perguntas if not answered(q)]
    out.alergias = base.alergias + _ai_allergies(base, ai, v, segments)
    out.pedidos = _merge_pedidos(base.pedidos, ai.pedidos_ia, v)
    out.descartados = base.descartados + v.dropped
    return out


def _ai_allergies(base: Insights, ai: ConsultLiveOutput, v: RefValidator, segments: list[Segment]) -> list[AllergyInfo]:
    """Alergias que só a IA viu: valem com fonte (fala de quem tem a alergia, ou o acompanhante) e entram com o selo
    "IA — confirmar". A reatividade cruzada vem da base curada quando o alérgeno é conhecido."""
    known = [a.alergeno for a in base.alergias]
    out: list[AllergyInfo] = []
    for al in v.keep(ai.alergias_relatadas, "alergias"):
        spoken = [_fala(r, segments) for r in al.refs]
        if not al.alergeno.strip() or not all(x is not None and x.falante in PATIENT_SIDE for x in spoken):
            v.dropped.append({"onde": "alergias", "texto": al.alergeno, "refs_invalidas": al.refs})
            continue
        if all(keyword_status(norm(x.texto), ("alergi",)) == "negado" for x in spoken if x):
            # a fala citada nega a alergia ("não tenho alergia a dipirona"): a IA não pode transformá-la em alergia
            v.dropped.append(
                {
                    "onde": "alergias",
                    "texto": al.alergeno,
                    "refs_invalidas": [],
                    "motivo": "a fala citada nega a alergia",
                }
            )
            continue
        if any(_similar(al.alergeno, k) or norm(al.alergeno) in norm(k) for k in [*known, *(a.alergeno for a in out)]):
            continue  # as regras (ou a própria IA) já registraram
        d = next((x for x in allergy_defs() if contains(norm(al.alergeno), x.words)), None)
        out.append(
            AllergyInfo(
                alergeno=d.label if d else al.alergeno.strip(),
                reacao=f"Relatado: {al.reacao.strip() or '—'}",
                fonte="relatado",
                reatividade_cruzada=d.cross if d else "Sem dados de reatividade cruzada na base de demonstração.",
                evitar=d.avoid if d else al.alergeno.strip(),
                alternativas=d.alternatives if d else "—",
                dicas=d.tips if d else "Informe a alergia em toda consulta e atendimento.",
                conflitos=[],
                refs=al.refs,
                origem="ia",
                chave=d.allergen if d else "",
                criticidade=al.criticidade,
                fonte_regra=d.source if d else "",
            )
        )
    return out


def _merge_pedidos(base: list[OrderDraft], items: list[AiPedido], v: RefValidator) -> list[OrderDraft]:
    """A IA completa só campos vazios (posologia, justificativa) de rascunhos que as regras detectaram. Não cria
    rascunho, não mexe nas checagens e não sobrescreve o que a fala ou o profissional já definiram."""
    out = [d.model_copy(deep=True) for d in base]
    by_id = {d.id: d for d in out}
    for it in items:
        d = by_id.get(it.id)
        if d is None or not v.ok(it.refs):
            v.dropped.append(
                {"onde": "pedidos", "texto": it.id, "refs_invalidas": [r for r in it.refs if r not in v.valid]}
            )
            continue
        filled = False
        for f in ("dose", "via", "frequencia", "duracao", "justificativa"):
            value = getattr(it, f).strip()
            if value and not getattr(d, f) and (f != "dose" or d.tipo == "medicamento"):
                setattr(d, f, value[:160])
                filled = True
        if filled:
            d.origem = "ia"
            if d.tipo == "medicamento" and not d.periodicidade:
                pos = parse_posology(norm(f"{d.frequencia} {d.duracao}"))
                d.periodicidade, d.duracao_dias = pos.periodicidade, d.duracao_dias or pos.duracao_dias
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
        "alergias_relatadas": [],
        "anamnese": anamnese_to_ai(base.anamnese)
        if base.anamnese
        else AiAnamnese(
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
        "perguntas_abordadas": [],
    }


def reconcile_ai_allergies(engine: Engine, ins: Insights) -> None:
    """Alergia que só a IA achou também passa pela checagem determinística: medicamento em uso, relatado ou que o
    profissional disse que vai prescrever, do mesmo grupo (ou com reação cruzada), vira alerta e conflito."""
    extra = [a for a in ins.alergias if a.origem == "ia"]
    if not extra:
        return
    reported, planned = engine.reported_meds(), engine.planned_meds()
    matches_ = engine._allergy_matches(extra, reported, planned)
    for a, kind, name, _ref, situation in matches_:
        line = f"{name} {situation}" if kind == "direto" else f"possível reação cruzada: {name} {situation}"
        if line not in a.conflitos:
            a.conflitos.append(line)
    alerts = engine._allergy_alerts(matches_)
    ins.sugestoes_medicamentos += [
        m for m in alerts if not any(_similar(m.texto, b.texto) for b in ins.sugestoes_medicamentos)
    ]
    ins.alertas += [
        Highlight(texto=m.texto, severidade=m.severidade, refs=m.refs) for m in alerts if m.severidade == "critico"
    ]


def run_live(engine: Engine, base: Insights, pseudo: Pseudonymizer, valid: set[str]):
    variables = {
        "tipo": KIND_LABEL.get(engine.kind, engine.kind),
        "especialidade": SPECIALTIES.get(engine.specialty, engine.specialty),
        "foco": SPECIALTY_FOCUS.get(engine.specialty, ""),
        "prontuario": pseudo.apply(chart_payload(engine)),
        "deterministico": pseudo.apply(deterministic_payload(base)),
        "transcricao": transcript_text(engine.segments, pseudo),
    }
    # duas chamadas em paralelo: schemas menores (limite da saída estruturada) e resposta mais rápida
    mock = mock_live(base)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_conv = pool.submit(
            generate_structured, "consult_live_conversa", variables, ConversaOutput, lambda _v: mock, "low"
        )
        f_clin = pool.submit(
            generate_structured, "consult_live_clinica", variables, ClinicaOutput, lambda _v: mock, "low"
        )
        conv, clin = f_conv.result(), f_clin.result()
    clinical = clin.output.model_dump() | {"anamnese": anamnese_from_ai(clin.output.anamnese).model_dump()}
    result = LLMResult(
        output=ConsultLiveOutput(**conv.output.model_dump(), **clinical),
        model=conv.model,
        prompt_version=f"{conv.prompt_version}+{clin.prompt_version}",
        mode=conv.mode,
        repaired=conv.repaired or clin.repaired,
    )
    merged = merge_live(base, result.output, valid, engine.segments, engine.r.sex)
    reconcile_ai_allergies(engine, merged)
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
    planned_meds = [m for m in ins.medicamentos if m.status == "planejado"]
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
                if m.status not in ("suspenso", "planejado")
            )
            or no_record,
            "refs": [r for m in ins.medicamentos if m.status not in ("suspenso", "planejado") for r in m.refs],
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
            "texto": "; ".join(
                [f"Prescrição citada pelo profissional: {m.medicamento} (a confirmar)" for m in planned_meds]
                + [f"{m.texto} (a confirmar)" for m in plan]
            )
            or no_record,
            "refs": [r for m in planned_meds for r in m.refs] + [r for m in plan for r in m.refs],
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
    # CID da nota precisa existir na tabela oficial; diagnóstico/hipótese não pode ser causa externa nem de outro sexo
    note.cids, bad_cids = screen(
        note.cids,
        "cids",
        engine.r.sex,
        primary=lambda c: c.tipo in ("principal", "hipotese"),
        trusted=engine.chart.icd_codes,
    )
    v.dropped += bad_cids
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
            "modelo": report.modelo.model_dump(include={"rotulo", "especialidade_rotulo", "versao"})
            if report.modelo
            else {},
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
    proposed, bad_cids = screen(
        v.keep(result.output.cids, "cids"),
        "cids",
        engine.r.sex,
        primary=lambda c: c.tipo != "secundario",
        trusted=engine.chart.icd_codes,
    )
    v.dropped += bad_cids
    cids = proposed or report.cids
    out = ins.model_copy(deep=True)
    out.relatorio_cirurgico = report.model_copy(update={"campos": new_fields, "cids": cids})
    out.modo = "ia" if result.mode == "real" else "regras"
    out.descartados = v.dropped
    return out, result
