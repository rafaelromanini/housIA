"""Painel Impacto (#36): adoção, tempo e qualidade da consulta com IA, calculados só do audit_log e do feedback.

Cada métrica devolve o valor, a fórmula escrita e os ids das linhas do audit_log que a compõem (drill-down em
`GET /audit/metrics/{metrica}/linhas`): nenhum número sai sem a conta que o explica. Horários no fuso do hospital.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime
from statistics import mean
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.consult import knowledge
from app.db.models import AIFeedback, AuditLog, Consultation, ImpactBaseline, User

EXPEDIENTE = (8, 18)  # seg-sex, das 8h às 18h; fora disso a documentação conta como "fora do expediente"
USO_REGULAR_MIN = 3  # consultas por semana para contar como uso regular
NPS_PROMOTOR, NPS_DETRATOR = 9, 6
SANDBOX_ENTITIES = {"consultation", "consulta", "consulta_nota"}  # entidades do audit_log que apontam para uma consulta

GRUPOS = [
    ("volume", "Volume e adoção"),
    ("tempo", "Tempo"),
    ("qualidade", "Qualidade"),
    ("satisfacao", "Satisfação e retorno de tempo"),
]


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def _local(moment: datetime) -> datetime:
    return moment.astimezone(_tz())


def setor_label(key: str | None) -> str:
    return knowledge.SPECIALTIES.get(key or "", key or "—")


def _pct(num: int | float, den: int | float) -> float | None:
    return round(100 * num / den, 1) if den else None


def _avg(values: list[float], digits: int = 1) -> float | None:
    return round(mean(values), digits) if values else None


def _metric(grupo: str, rotulo: str, valor, unidade: str, formula: str, linhas: list[AuditLog], **extra) -> dict:
    ids = sorted({a.id for a in linhas})
    return {
        "grupo": grupo,
        "rotulo": rotulo,
        "valor": valor,
        "unidade": unidade,
        "formula": formula,
        "n_linhas": len(ids),
        "_ids": ids,
        **extra,
    }


def _p(a: AuditLog) -> dict:
    return a.payload_json or {}


def _last_by_entity(rows: list[AuditLog]) -> list[AuditLog]:
    last: dict[str, AuditLog] = {}
    for a in rows:
        last[a.entity_id or str(a.id)] = a
    return list(last.values())


def _first_by_entity(rows: list[AuditLog]) -> list[AuditLog]:
    first: dict[str, AuditLog] = {}
    for a in rows:
        first.setdefault(a.entity_id or str(a.id), a)
    return list(first.values())


def _layer_acceptance(finalizadas: list[AuditLog], key: str) -> tuple[dict, int, int]:
    layers = {"regra": {"sugeridas": 0, "aceitas": 0}, "ia": {"sugeridas": 0, "aceitas": 0}}
    for a in finalizadas:
        for layer, counts in (_p(a).get(key) or {}).items():
            if layer in layers:
                layers[layer]["sugeridas"] += int(counts.get("sugeridas", 0))
                layers[layer]["aceitas"] += int(counts.get("aceitas", 0))
    for counts in layers.values():
        counts["pct"] = _pct(counts["aceitas"], counts["sugeridas"])
    total = sum(c["sugeridas"] for c in layers.values())
    accepted = sum(c["aceitas"] for c in layers.values())
    return layers, accepted, total


def baselines(db: Session, until: datetime | None = None) -> dict[str, ImpactBaseline]:
    """A linha de base mais recente de cada setor (até `until`)."""
    q = select(ImpactBaseline).order_by(ImpactBaseline.id)
    if until:
        q = q.where(ImpactBaseline.created_at <= until)
    return {b.sector: b for b in db.scalars(q)}


def compute(db: Session, start: datetime, end: datetime, uso_regular_min: int = USO_REGULAR_MIN) -> dict:
    rows = list(
        db.scalars(
            select(AuditLog)
            .where(
                AuditLog.created_at >= start,
                AuditLog.created_at <= end,
                or_(
                    AuditLog.action.startswith("consulta."),
                    AuditLog.action == "cirurgia.prontidao",
                    AuditLog.action == "ia.feedback",
                ),
            )
            .order_by(AuditLog.id)
        )
    )
    # consulta de sandbox (#34) é teste com cenário sintético: não entra em adoção, tempo nem qualidade
    sandbox = {str(i) for i in db.scalars(select(Consultation.id).where(Consultation.sandbox.is_(True)))}
    rows = [a for a in rows if not (a.entity in SANDBOX_ENTITIES and a.entity_id in sandbox)]
    by_action: dict[str, list[AuditLog]] = defaultdict(list)
    for a in rows:
        by_action[a.action].append(a)
    users = {u.id: u for u in db.scalars(select(User))}
    metricas: dict[str, dict] = {}

    # ---------- volume ----------
    iniciadas = by_action["consulta.iniciada"]
    enviadas = _first_by_entity(
        sorted(
            by_action["consulta.enviada"]
            + [a for a in by_action["consulta.exportada"] if _p(a).get("tempo_ate_envio_s") is not None],
            key=lambda a: a.id,
        )
    )
    metricas["atendimentos_apoiados"] = _metric(
        "volume",
        "Atendimentos apoiados pela IA",
        len(iniciadas),
        "atendimentos",
        "Número de consultas iniciadas (ação consulta.iniciada) no período.",
        iniciadas,
    )
    metricas["notas_enviadas"] = _metric(
        "volume",
        "Notas enviadas ao prontuário",
        len(enviadas),
        "notas",
        "Consultas com nota enviada (consulta.enviada) ou exportada com o envio automático desligado "
        "(consulta.exportada), contando a primeira vez de cada consulta.",
        enviadas,
    )
    per_month: dict[tuple[int | None, str], list[AuditLog]] = defaultdict(list)
    for a in iniciadas:
        per_month[(a.user_id, _local(a.created_at).strftime("%Y-%m"))].append(a)
    tabela = [
        {
            "profissional": users[uid].name if uid in users else "—",
            "papel": users[uid].role if uid in users else None,
            "mes": month,
            "consultas": len(items),
        }
        for (uid, month), items in sorted(per_month.items(), key=lambda kv: (kv[0][1], kv[0][0] or 0))
    ]
    metricas["consultas_por_profissional_mes"] = _metric(
        "volume",
        "Consultas por profissional por mês",
        _avg([len(v) for v in per_month.values()]),
        "consultas",
        "Média, entre os pares (profissional, mês), do número de consultas iniciadas.",
        iniciadas,
        tabela=tabela,
    )
    weeks = max(1, math.ceil((end - start).total_seconds() / (7 * 86400)))
    per_user: dict[int | None, list[AuditLog]] = defaultdict(list)
    for a in iniciadas:
        per_user[a.user_id].append(a)
    regular = {uid: v for uid, v in per_user.items() if len(v) / weeks >= uso_regular_min}
    metricas["uso_regular"] = _metric(
        "volume",
        "Uso regular",
        _pct(len(regular), len(per_user)),
        "%",
        f"Profissionais com pelo menos {uso_regular_min} consultas por semana (consultas iniciadas ÷ {weeks} "
        f"semana(s) do período) ÷ profissionais que iniciaram alguma consulta.",
        [a for v in per_user.values() for a in v],
        numerador=len(regular),
        denominador=len(per_user),
    )

    # ---------- tempo ----------
    finalizadas = by_action["consulta.finalizada"]
    with_duration = [a for a in finalizadas if _p(a).get("duracao_s") is not None]
    metricas["duracao_media_min"] = _metric(
        "tempo",
        "Duração média da consulta",
        _avg([_p(a)["duracao_s"] / 60 for a in with_duration]),
        "min",
        "Média de (consulta finalizada − consulta iniciada), em minutos.",
        with_duration,
    )
    with_send = [a for a in enviadas if _p(a).get("tempo_ate_envio_s") is not None]
    metricas["tempo_ate_envio_min"] = _metric(
        "tempo",
        "Tempo até o envio da nota",
        _avg([_p(a)["tempo_ate_envio_s"] / 60 for a in with_send]),
        "min",
        "Média de (nota enviada − consulta finalizada), em minutos.",
        with_send,
    )
    same_day = [
        a
        for a in with_send
        if _p(a).get("finalizada_em")
        and _local(datetime.fromisoformat(_p(a)["finalizada_em"])).date() == _local(a.created_at).date()
    ]
    metricas["enviadas_mesmo_dia"] = _metric(
        "tempo",
        "Notas enviadas no mesmo dia",
        _pct(len(same_day), len(with_send)),
        "%",
        "Notas enviadas no mesmo dia (fuso do hospital) em que a consulta foi finalizada ÷ notas enviadas.",
        with_send,
        numerador=len(same_day),
        denominador=len(with_send),
    )
    docs = by_action["consulta.nota_revisada"] + enviadas
    off = [a for a in docs if _outside_hours(a.created_at)]
    metricas["documentacao_fora_expediente"] = _metric(
        "tempo",
        "Documentação fora do expediente",
        _pct(len(off), len(docs)),
        "%",
        f"Revisões e envios de nota feitos fora de seg-sex, {EXPEDIENTE[0]}h às {EXPEDIENTE[1]}h (fuso do hospital) "
        "÷ todas as revisões e envios.",
        docs,
        numerador=len(off),
        denominador=len(docs),
    )

    # ---------- qualidade ----------
    revisadas = [a for a in _last_by_entity(by_action["consulta.nota_revisada"]) if "editado_pct" in _p(a)]
    editadas = [a for a in revisadas if _p(a)["editado_pct"] > 0]
    base_ids = {a.entity_id for a in finalizadas} | {a.entity_id for a in revisadas}
    metricas["notas_editadas"] = _metric(
        "qualidade",
        "Notas editadas pelo profissional",
        _pct(len(editadas), len(base_ids)),
        "%",
        "Consultas cuja última revisão mudou o texto da nota gerada (consulta.nota_revisada com editado_pct > 0) "
        "÷ consultas finalizadas ou revisadas.",
        revisadas,
        numerador=len(editadas),
        denominador=len(base_ids),
    )
    metricas["edicao_media"] = _metric(
        "qualidade",
        "Quanto da nota é editado",
        _avg([_p(a)["editado_pct"] for a in revisadas]),
        "%",
        "Média de editado_pct (palavras trocadas, acrescentadas ou removidas ÷ tamanho da nota) na última revisão "
        "de cada consulta.",
        revisadas,
    )
    for key, rotulo, quem in (
        ("aceite_perguntas", "Aceite das perguntas sugeridas", "perguntas sugeridas que foram feitas na consulta"),
        ("aceite_hipoteses", "Aceite das hipóteses", "hipóteses confirmadas ou provisórias pelo profissional"),
    ):
        layers, accepted, total = _layer_acceptance(finalizadas, key)
        metricas[key] = _metric(
            "qualidade",
            rotulo,
            _pct(accepted, total),
            "%",
            f"{quem[0].upper() + quem[1:]} ÷ sugeridas, na finalização da consulta; por camada: regra "
            "(determinística) e IA.",
            [a for a in finalizadas if key in _p(a)],
            numerador=accepted,
            denominador=total,
            por_camada=layers,
        )
    answers, inserted = by_action["consulta.copiloto"], by_action["consulta.copiloto_inserido"]
    metricas["aceite_copiloto"] = _metric(
        "qualidade",
        "Respostas do copiloto levadas à nota",
        _pct(len(inserted), len(answers)),
        "%",
        "Inserções na nota (consulta.copiloto_inserido) ÷ respostas do copiloto (consulta.copiloto).",
        answers + inserted,
        numerador=len(inserted),
        denominador=len(answers),
    )
    per_surgery: dict[str, list[AuditLog]] = defaultdict(list)
    for a in by_action["cirurgia.prontidao"]:
        per_surgery[a.entity_id or str(a.id)].append(a)
    before = [s for v in per_surgery.values() if (s := (_p(v[0]).get("antes") or {}).get("score")) is not None]
    after = [s for v in per_surgery.values() if (s := (_p(v[-1]).get("depois") or {}).get("score")) is not None]
    metricas["conformidade_cirurgica"] = _metric(
        "qualidade",
        "Conformidade cirúrgica (antes → depois)",
        _avg(after),
        "pontos",
        "Pontuação de prontidão do relatório cirúrgico: antes = no início da cirurgia (primeiro registro), "
        "depois = na última verificação (cirurgia.prontidao), média por relatório.",
        [a for v in per_surgery.values() for a in v],
        antes=_avg(before),
        depois=_avg(after),
        glosas_evitadas=sum(len(_p(a).get("resolvidas") or []) for v in per_surgery.values() for a in v),
    )

    # ---------- satisfação ----------
    nps_rows = list(
        db.scalars(
            select(AIFeedback).where(
                AIFeedback.kind == "nps", AIFeedback.created_at >= start, AIFeedback.created_at <= end
            )
        )
    )
    latest_nps: dict[tuple[int, str], AIFeedback] = {}
    for f in sorted(nps_rows, key=lambda f: f.id):
        latest_nps[(f.user_id, _local(f.created_at).strftime("%Y-%m"))] = f
    scores = [f.score for f in latest_nps.values() if f.score is not None]
    promoters = sum(s >= NPS_PROMOTOR for s in scores)
    detractors = sum(s <= NPS_DETRATOR for s in scores)
    used = {f.id for f in latest_nps.values()}
    nps_audit = _last_by_feedback(
        [a for a in by_action["ia.feedback"] if a.entity == "nps" and _p(a).get("feedback_id") in used]
    )
    metricas["nps"] = _metric(
        "satisfacao",
        "NPS dos profissionais",
        round(100 * (promoters - detractors) / len(scores)) if scores else None,
        "pontos",
        f"% de promotores (nota ≥ {NPS_PROMOTOR}) − % de detratores (nota ≤ {NPS_DETRATOR}); uma resposta por "
        "profissional por mês (a mais recente).",
        nps_audit,
        distribuicao={
            "promotores": promoters,
            "neutros": len(scores) - promoters - detractors,
            "detratores": detractors,
        },
        respostas=len(scores),
    )
    metricas["horas_devolvidas"] = _hours_returned(db, end, enviadas, metricas)
    return {
        "periodo": {"inicio": start.isoformat(), "fim": end.isoformat()},
        "grupos": [
            {"id": g, "rotulo": r, "metricas": [k for k, m in metricas.items() if m["grupo"] == g]} for g, r in GRUPOS
        ],
        "metricas": metricas,
    }


def _last_by_feedback(rows: list[AuditLog]) -> list[AuditLog]:
    last: dict[int, AuditLog] = {}
    for a in rows:
        last[_p(a)["feedback_id"]] = a
    return list(last.values())


def _outside_hours(moment: datetime) -> bool:
    local = _local(moment)
    return local.weekday() >= 5 or not EXPEDIENTE[0] <= local.hour < EXPEDIENTE[1]


def _hours_returned(db: Session, end: datetime, enviadas: list[AuditLog], metricas: dict) -> dict:
    base = baselines(db, end)
    per_sector: dict[str, list[AuditLog]] = defaultdict(list)
    for a in enviadas:
        if _p(a).get("tempo_ate_envio_s") is not None:
            per_sector[_p(a).get("especialidade") or "—"].append(a)
    setores, lines, total_min = [], [], 0.0
    for sector, items in sorted(per_sector.items()):
        atual = mean(_p(a)["tempo_ate_envio_s"] / 60 for a in items)
        b = base.get(sector)
        entry = {
            "setor": sector,
            "rotulo": setor_label(sector),
            "atendimentos": len(items),
            "tempo_atual_min": round(atual, 1),
            "linha_de_base_min": b.minutes_before if b else None,
            "minutos_devolvidos": None,
        }
        if b:
            entry["minutos_devolvidos"] = round(len(items) * max(0.0, b.minutes_before - atual), 1)
            total_min += entry["minutos_devolvidos"]
            lines += items
        setores.append(entry)
    for sector in sorted(set(base) - set(per_sector)):
        setores.append(
            {
                "setor": sector,
                "rotulo": setor_label(sector),
                "atendimentos": 0,
                "tempo_atual_min": None,
                "linha_de_base_min": base[sector].minutes_before,
                "minutos_devolvidos": 0.0,
            }
        )
    audit_baselines = []
    if base:
        ids = [str(b.id) for b in base.values()]
        audit_baselines = list(
            db.scalars(select(AuditLog).where(AuditLog.action == "impacto.linha_de_base", AuditLog.entity_id.in_(ids)))
        )
    com_base = [s for s in setores if s["linha_de_base_min"] is not None]
    return _metric(
        "satisfacao",
        "Horas devolvidas à assistência",
        round(total_min / 60, 1) if com_base else None,
        "h",
        "Para cada setor com linha de base: atendimentos com nota enviada × (minutos de documentação antes do "
        "HousIA − tempo médio até o envio hoje), nunca negativo; somado e dividido por 60. Setor sem linha de base "
        "não entra.",
        lines + audit_baselines,
        minutos=round(total_min, 1),
        por_setor=setores,
        sem_linha_de_base=sorted({s["setor"] for s in setores if s["linha_de_base_min"] is None}),
    )


def public(report: dict) -> dict:
    """Sem os ids internos das linhas."""
    return {
        **report,
        "metricas": {k: {f: v for f, v in m.items() if f != "_ids"} for k, m in report["metricas"].items()},
    }


def lines(db: Session, report: dict, key: str) -> list[tuple[AuditLog, User | None]]:
    ids = report["metricas"][key]["_ids"]
    if not ids:
        return []
    return list(
        db.execute(
            select(AuditLog, User)
            .outerjoin(User, User.id == AuditLog.user_id)
            .where(AuditLog.id.in_(ids))
            .order_by(AuditLog.id)
        ).all()
    )


def flat_rows(report: dict) -> list[dict]:
    """Uma linha por métrica (e por camada/setor) para o CSV."""
    out = []
    for key, m in report["metricas"].items():
        out.append(
            {
                "metrica": key,
                "rotulo": m["rotulo"],
                "valor": m["valor"],
                "unidade": m["unidade"],
                "numerador": m.get("numerador"),
                "denominador": m.get("denominador"),
                "linhas_audit": m["n_linhas"],
                "formula": m["formula"],
            }
        )
        for layer, c in (m.get("por_camada") or {}).items():
            out.append(
                {
                    "metrica": f"{key}.{layer}",
                    "rotulo": f"{m['rotulo']} — camada {layer}",
                    "valor": c["pct"],
                    "unidade": "%",
                    "numerador": c["aceitas"],
                    "denominador": c["sugeridas"],
                    "linhas_audit": None,
                    "formula": "aceitas ÷ sugeridas na camada",
                }
            )
        for s in m.get("por_setor") or []:
            out.append(
                {
                    "metrica": f"{key}.{s['setor']}",
                    "rotulo": f"{m['rotulo']} — {s['rotulo']}",
                    "valor": s["minutos_devolvidos"],
                    "unidade": "min",
                    "numerador": s["atendimentos"],
                    "denominador": None,
                    "linhas_audit": None,
                    "formula": "atendimentos × (linha de base − tempo atual até o envio)",
                }
            )
    return out


def measure_report(report: dict, generated: datetime, measure: str | None = None) -> dict:
    """FHIR R4 MeasureReport (tipo summary) com uma group por métrica: valor em measureScore e, quando é razão,
    as populações numerador e denominador."""
    groups = []
    for key, m in report["metricas"].items():
        if m["valor"] is None:
            continue
        group: dict = {
            "id": key.replace("_", "-"),
            "code": {
                "coding": [{"system": MEASURE_CODES, "code": key, "display": m["rotulo"]}],
                "text": m["formula"],
            },
            "measureScore": {"value": m["valor"], "unit": m["unidade"]},
        }
        if m.get("denominador") is not None:
            group["population"] = [
                _population("numerator", "Numerador", m["numerador"]),
                _population("denominator", "Denominador", m["denominador"]),
            ]
        groups.append(group)
    return {
        "resourceType": "MeasureReport",
        "status": "complete",
        "type": "summary",
        "measure": measure or MEASURE_URL,
        "date": generated.astimezone(UTC).isoformat(),
        "period": {"start": report["periodo"]["inicio"], "end": report["periodo"]["fim"]},
        "group": groups,
    }


MEASURE_URL = "https://housia.dev/fhir/Measure/impacto-consulta"
MEASURE_CODES = "https://housia.dev/fhir/CodeSystem/metricas-impacto"


def _population(code: str, display: str, count: int) -> dict:
    return {
        "code": {
            "coding": [
                {"system": "http://terminology.hl7.org/CodeSystem/measure-population", "code": code, "display": display}
            ]
        },
        "count": count,
    }
