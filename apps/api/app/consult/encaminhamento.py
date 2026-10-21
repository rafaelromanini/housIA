"""Smart App "Antes de encaminhar" (#29): critérios, exames mínimos e manejo possível na APS antes da fila.

Determinístico, roda com ou sem LLM. O card abre por dois gatilhos: o profissional diz que vai encaminhar para uma
especialidade ("vou te encaminhar para o urologista") ou uma hipótese da análise já cumpre critério de encaminhamento.
A base é `data/reference/referral_criteria.csv` (protocolos de encaminhamento do Ministério da Saúde e do TelessaúdeRS).
Condição sem linha na base não abre card. As escolhas "encaminhar" e "manejar na APS" ficam em
`extra_json["encaminhamentos"]` e na auditoria (`consulta.encaminhamento`); a métrica de evitados sai dali.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.consult import knowledge, protocolos
from app.consult.chart import ChartView, fmt_date
from app.consult.schemas import (
    CriterioEnc,
    DecisaoEnc,
    Encaminhamento,
    ExameEnc,
    Hypothesis,
    Segment,
)
from app.consult.text import NEG_WORDS, is_question, norm
from app.fhir_client import codes

ESCOLHAS = ("pendente", "encaminhar", "manejar_aps")
MIN_MOTIVO = 10
SNOMED_REFERRAL = ("3457005", "Patient referral")
SYSTEM = "urn:housia:encaminhamento"

# "vou te encaminhar", "precisamos mandar o senhor", "encaminho a senhora"... (futuro, intenção ou presente)
INTENCAO = re.compile(
    r"(?<![a-z])(?:(?:vou|vamos|irei|iremos|preciso|precisamos|quero|queremos|estou|estamos|devo|devemos|tenho que"
    r"|temos que)\b[^.?!;]*?\b(?P<v1>encaminhar|encaminhando|referenciar|mandar|passar|enviar)"
    r"|(?P<v2>encaminho))(?![a-z])"
)
# a oração é condicional ou adiada ("se piorar eu encaminho", "talvez precise encaminhar"): ainda não é a decisão
CONDICIONAL = re.compile(
    r"(?<![a-z])(?:se|caso|quando|talvez|quem sabe|depois|futuramente|se for preciso|pensando|cogitando|avaliando|"
    r"considerando)(?![a-z])"
)
FRASE = re.compile(r"(?<=[.?!;])\s+|\n+")
# "tá bom?", "certo?", "né?" só pedem concordância: a frase continua sendo uma decisão, não uma pergunta
CONCORDANCIA = re.compile(r"(?<![a-z])(?:ta bom|esta bom|ta|ok|certo|ne|combinado|tudo bem|beleza|entendeu)\s*\?+\s*$")
FALANTES = (
    "medico",
    "desconhecido",
)  # quem decide encaminhar é o profissional (a fala automática sem dono também vale)
JANELA = 8  # palavras depois do verbo em que a especialidade precisa aparecer


def _rows() -> tuple[knowledge.ReferralDef, ...]:
    return knowledge.referrals()


def _specialties() -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for r in _rows():
        out.setdefault(r.specialty, r.specialty_terms)
    return out


def falas_que_encaminham(segments: Iterable[Segment]) -> dict[str, list[Segment]]:
    """Especialidade → falas em que o profissional diz que vai encaminhar para ela. Pergunta, negação ("não vou
    encaminhar"), condição ("se piorar, encaminho") e o paciente falando não valem."""
    found: dict[str, list[Segment]] = {}
    specialties = _specialties()
    for s in segments:
        if s.falante not in FALANTES or s.marco:
            continue
        for raw in FRASE.split(s.texto.strip()):
            sentence = norm(raw)
            if raw.rstrip().endswith("?") and not CONCORDANCIA.search(sentence) or is_question(raw) and "?" not in raw:
                continue  # "devo encaminhar?" pergunta; não decide nada
            sentence = sentence.rstrip("?! ")
            m = INTENCAO.search(sentence)
            if not m:
                continue
            before = sentence[: m.start("v1") if m.group("v1") else m.start("v2")]
            if any(w in NEG_WORDS for w in before.split()) or CONDICIONAL.search(before):
                continue
            after = " ".join(sentence[m.end() :].split()[:JANELA])
            for esp, terms in specialties.items():
                if any(re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", after) for t in terms):
                    found.setdefault(esp, [])
                    if s not in found[esp]:
                        found[esp].append(s)
    return found


# ---------- critérios e exames ----------
def _num(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _criterio(texto: str, rule: str, chart: ChartView, segments: list[Segment]) -> CriterioEnc:
    kind, _, spec = rule.partition(":")
    if rule.startswith("tfg"):
        egfr = chart.egfr
        limit = _num(rule[4:])
        if egfr is None or limit is None:
            return CriterioEnc(texto=texto, status="confirmar", evidencia="Sem creatinina recente para estimar a TFG.")
        value, ev = egfr
        ok = value < limit
        return CriterioEnc(
            texto=texto,
            status="atendido" if ok else "nao_atendido",
            evidencia=f"TFG estimada {str(value).replace('.', ',')} mL/min/1,73 m² em {fmt_date(ev.timestamp)}",
            refs=[ev.source_ref],
        )
    if kind == "lab":
        m = re.fullmatch(r"(\w+)([<>])([\d.]+)", spec)
        if not m:
            return CriterioEnc(texto=texto, status="confirmar")
        key, op, limit = m.group(1), m.group(2), float(m.group(3))
        last = chart.latest_lab(key, max_days=3650)
        measure = codes.LABS.get(key)
        if last is None or measure is None:
            label = measure.label if measure else key
            return CriterioEnc(texto=texto, status="confirmar", evidencia=f"Sem {label} no prontuário.")
        value = float(last.value["valor"])
        ok = value > limit if op == ">" else value < limit
        shown = str(value).replace(".", ",")
        return CriterioEnc(
            texto=texto,
            status="atendido" if ok else "nao_atendido",
            evidencia=f"{measure.label} {shown} {measure.unit} em {fmt_date(last.timestamp)}".replace("  ", " "),
            refs=[last.source_ref],
        )
    if kind == "fala":
        words = tuple(w for w in spec.split("~") if w)
        falas = protocolos.falas_com(segments, words)
        if falas:
            refs = protocolos.refs_das_falas(falas)
            return CriterioEnc(
                texto=texto, status="atendido", evidencia="Dito na consulta: " + ", ".join(refs), refs=refs
            )
        return CriterioEnc(texto=texto, status="nao_atendido", evidencia="Não citado na conversa até agora.")
    return CriterioEnc(texto=texto, status="confirmar", evidencia="Avaliação clínica do profissional.")


def _exame(label: str, source: str, days: int, chart: ChartView) -> ExameEnc:
    """Mesma leitura do prontuário dos protocolos de APS (`chart._protocol_record`): FHIR, nota ou procedimento."""
    kind, _, spec = source.partition(":")
    if kind == "texto":
        spec = "|".join(w for w in spec.split("~") if w)
    probe = knowledge.ProtocolDef(
        protocol="", condition="", item=label, source=f"{kind}:{spec}", interval_days=days, guidance=""
    )
    status, ultimo, refs = chart._protocol_record(probe)
    return ExameEnc(
        exame=label,
        status={"em_dia": "feito", "pendente": "desatualizado"}.get(status, "faltando"),  # type: ignore[arg-type]
        ultimo=ultimo or "",
        refs=refs,
    )


def _contexto(
    row: knowledge.ReferralDef, chart: ChartView, hyps: list[Hypothesis], segments: list[Segment]
) -> list[str] | None:
    """A condição da linha vale para este paciente? Diagnóstico ativo, hipótese ou tema dito na conversa. Devolve as
    refs que motivam (lista vazia = vale, sem ref) ou None."""
    for prefix in row.icd:
        cond = chart.has_condition(prefix)
        if cond is not None:
            return [f"Condition/{cond['id']}"]
    for h in hyps:
        if h.decisao != "descartada" and h.cid.startswith(row.icd):
            return h.refs[:2]
    falas = protocolos.falas_com(segments, row.condition_terms) if row.condition_terms else []
    return protocolos.refs_das_falas(falas, 2) if falas else None


def _justificativa(enc: Encaminhamento) -> str:
    atendidos = [c for c in enc.criterios if c.status == "atendido"]
    feitos = [e for e in enc.exames if e.status == "feito"]
    linhas = [
        f"Encaminhamento para {enc.especialidade}. Condição: {enc.condicao}.",
        "Critérios atendidos: "
        + (
            "; ".join(f"{c.texto} ({c.evidencia})" if c.evidencia else c.texto for c in atendidos)
            or "nenhum registrado"
        )
        + ".",
        "Exames mínimos realizados: " + ("; ".join(f"{e.exame} ({e.ultimo})" for e in feitos) or "nenhum") + ".",
        "Exames mínimos pendentes: " + ("; ".join(enc.exames_faltantes) or "nenhum") + ".",
        f"Manejo já possível na APS: {enc.manejo_aps}",
    ]
    return "\n".join(linhas)


def _rascunho(enc: Encaminhamento, patient_id: str, when) -> dict:
    """ServiceRequest em RASCUNHO (status=draft, intent=proposal): nada vai ao prontuário sem o envio revisado."""
    fhir_refs = list(dict.fromkeys(r for r in enc.refs if not r.startswith("Fala/")))
    return {
        "resourceType": "ServiceRequest",
        "status": "draft",
        "intent": "proposal",
        "identifier": [{"system": SYSTEM, "value": enc.id}],
        "category": [{"coding": [{"system": codes.SNOMED, "code": SNOMED_REFERRAL[0], "display": SNOMED_REFERRAL[1]}]}],
        "code": {"text": f"Encaminhamento para {enc.especialidade}"},
        "performerType": {"text": enc.especialidade},
        "subject": {"reference": f"Patient/{patient_id}"},
        "authoredOn": when.isoformat(),
        "reasonCode": [
            {"text": f"{enc.condicao}: " + "; ".join(c.texto for c in enc.criterios if c.status == "atendido")}
        ],
        "supportingInfo": [{"reference": r} for r in fhir_refs],
        "note": [{"text": _justificativa(enc)}, {"text": "Fonte: " + enc.fonte}],
        "extension": [
            {"url": f"{SYSTEM}/refs", "valueString": ", ".join(enc.refs)},
        ],
    }


def _montar(
    row: knowledge.ReferralDef,
    gatilho: str,
    gatilho_texto: str,
    gatilho_refs: list[str],
    chart: ChartView,
    segments: list[Segment],
    decisions: dict,
) -> Encaminhamento:
    criterios = [_criterio(t, rule, chart, segments) for t, rule in row.criteria]
    exames = [_exame(label, src, days, chart) for label, src, days in row.exams]
    faltantes = [
        f"{e.exame} (desatualizado: {e.ultimo})" if e.status == "desatualizado" else e.exame
        for e in exames
        if e.status != "feito"
    ]
    atendidos = [c for c in criterios if c.status == "atendido"]
    if faltantes:
        rec = f"Faltam exames mínimos: {', '.join(faltantes)}. Peça antes de encaminhar, salvo sinal de urgência."
    elif not atendidos:
        rec = "Nenhum critério de encaminhamento atendido até agora: considere manejar na APS ou pedir teleconsultoria."
    else:
        rec = "Critérios atendidos e exames mínimos completos: encaminhe com a justificativa estruturada."
    refs = list(
        dict.fromkeys([*gatilho_refs, *(r for c in atendidos for r in c.refs), *(r for e in exames for r in e.refs)])
    )
    saved = decisions.get(row.id) or {}
    enc = Encaminhamento(
        id=row.id,
        especialidade=row.specialty,
        condicao=row.condition,
        gatilho=gatilho,  # type: ignore[arg-type]
        gatilho_texto=gatilho_texto,
        criterios=criterios,
        exames=exames,
        exames_faltantes=faltantes,
        manejo_aps=row.management,
        sinais_urgencia=row.urgency,
        teleconsultoria=row.teleconsulting,
        fonte=row.source,
        recomendacao=rec,
        refs=refs,
        decisao=DecisaoEnc(
            escolha=saved.get("escolha", "pendente"),
            motivo=saved.get("motivo", ""),
            por=saved.get("por"),
            em=saved.get("em"),
        ),
    )
    enc.rascunho = _rascunho(enc, chart.r.id, chart.now)
    return enc


def build(
    chart: ChartView, segments: list[Segment], hyps: list[Hypothesis], extra: dict | None = None
) -> list[Encaminhamento]:
    """Cards "Antes de encaminhar" da consulta: os da fala primeiro, depois os de hipótese com critério atendido."""
    decisions = (extra or {}).get("encaminhamentos") or {}
    out: list[Encaminhamento] = []
    spoken = falas_que_encaminham(segments)
    for row in _rows():
        falas = spoken.get(row.specialty)
        if not falas:
            continue
        motive = _contexto(row, chart, hyps, segments)
        if motive is None:
            continue  # a especialidade foi dita, mas a condição desta linha não vale para o paciente
        refs = protocolos.refs_das_falas(falas, 2) + motive
        out.append(
            _montar(
                row,
                "fala",
                f"Encaminhar para {row.specialty.lower()} dito em {refs[0]}",
                refs,
                chart,
                segments,
                decisions,
            )
        )
    taken = {e.id for e in out}
    for row in _rows():
        if row.id in taken:
            continue
        hit = next((h for h in hyps if h.decisao != "descartada" and h.cid.startswith(row.icd)), None)
        if hit is None:
            continue
        enc = _montar(
            row, "hipotese", f"Hipótese: {hit.hipotese} ({hit.cid})", hit.refs[:3], chart, segments, decisions
        )
        if any(c.status == "atendido" for c in enc.criterios):
            out.append(enc)
    return out


def resource_for_export(enc: dict, *, consultation_id: int, encounter_id: str | None, author: str) -> dict:
    """ServiceRequest do card (guardado em `insights_json`) pronto para exportar, ligado ao encontro e ao autor."""
    res = dict(enc["rascunho"])
    res["identifier"] = [{"system": SYSTEM, "value": f"{consultation_id}/{enc['id']}"}]
    if encounter_id:
        res["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    if author:
        res["requester"] = {"display": author}
    return res
