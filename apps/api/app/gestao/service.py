"""Copiloto do gestor (#37): indicadores de alta, transição, centro cirúrgico e ambulatório, cada um com a conta e as
linhas (e recursos FHIR) que o compõem.

Tudo é recomputável: o valor de um indicador é sempre função das suas `linhas`, e cada linha cita o paciente, o
Encounter ou o recurso de origem. O resumo semanal (IA opcional) só pode citar números que estão no painel.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.consult import knowledge
from app.consult.chart import ChartView
from app.db.models import AuditLog, Consultation, DischargeAssessment, DischargeOverride, MedrecRun
from app.db.tenancy import current_org
from app.fhir_client import codes
from app.fhir_client.client import FhirSource
from app.llm.client import generate_structured
from app.patients import avulso
from app.patients.service import bed_panel
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from app.timeline.record import PatientRecord, parse_dt, ref_id

MEASURE_URL = "https://housia.dev/fhir/Measure/gestao-transicao-cuidado"
HORA_ALTA_CEDO = 11  # "alta antes das 11h"
SEMANAS_DA_SERIE = 4
GRUPOS = [
    ("alta", "Alta"),
    ("transicao", "Transição de cuidado"),
    ("cirurgico", "Centro cirúrgico"),
    ("ambulatorio", "Ambulatório"),
]
NAO_ADERE = {"pendente", "sem_registro", "fora_do_alvo"}


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def period(since: date | None, until: date | None) -> tuple[datetime, datetime]:
    """Dias no fuso do hospital; padrão: últimos 7 dias até agora."""
    from datetime import UTC, time

    tz = _tz()
    end = datetime.combine(until, time.max, tzinfo=tz).astimezone(UTC) if until else clock.now()
    start = datetime.combine(since, time.min, tzinfo=tz).astimezone(UTC) if since else end - timedelta(days=7)
    return start, end


@dataclass
class Ctx:
    db: Session
    fhir: FhirSource
    now: datetime
    start: datetime
    end: datetime
    beds: dict[str, dict]  # paciente internado → linha do painel de leitos
    hosp: dict[str, dict]  # paciente internado → Encounter hospitalar
    cache: dict = field(default_factory=dict)

    def local(self, moment: datetime) -> datetime:
        return moment.astimezone(_tz())

    def where(self, pid: str | None) -> tuple[str, str]:
        """(nome · leito, setor) do paciente internado, ou o id e "—"."""
        bed = self.beds.get(pid or "")
        if bed is None:
            return (pid or "—"), "—"
        return f"{bed['nome']} · {bed['setor']} leito {bed['leito']}", bed["setor"]


def _line(
    id_: str, texto: str, *, pid: str | None = None, setor: str = "—", refs: list[str] | None = None, peso: float = 1
) -> dict:
    refs = list(dict.fromkeys(([f"Patient/{pid}"] if pid else []) + (refs or [])))
    return {"id": id_, "descricao": texto, "paciente": pid, "setor": setor, "fhir_refs": refs, "peso": peso}


def _count(linhas: list[dict]) -> float:
    return sum(item["peso"] for item in linhas)


def _by_sector(linhas: list[dict], mean_: bool = False) -> dict[str, float]:
    groups: dict[str, list[float]] = defaultdict(list)
    for item in linhas:
        groups[item["setor"]].append(item["peso"])
    return {k: round(mean(v), 1) if mean_ else round(sum(v), 1) for k, v in sorted(groups.items())}


def _ind(
    grupo: str,
    rotulo: str,
    unidade: str,
    formula: str,
    linhas: list[dict],
    valor: float | None = None,
    *,
    media: bool = False,
    **extra,
) -> dict:
    if valor is None and linhas:
        valor = round(mean(i["peso"] for i in linhas), 1) if media else round(_count(linhas), 1)
    if valor is None and not media:
        valor = 0
    if isinstance(valor, float) and valor.is_integer() and not media:
        valor = int(valor)
    return {
        "grupo": grupo,
        "rotulo": rotulo,
        "valor": valor,
        "unidade": unidade,
        "formula": formula,
        "n_linhas": len(linhas),
        "por_setor": _by_sector(linhas, media),
        "linhas": linhas,
        **extra,
    }


# ---------- alta ----------
def _ext_alta(enc: dict) -> datetime | None:
    for e in enc.get("extension", []):
        if e.get("url") == codes.ALTA_PREVISTA:
            return parse_dt(e.get("valueDateTime"))
    return None


def _previstas(c: Ctx, day_offset: int, rotulo: str) -> dict:
    target = c.local(c.now).date() + timedelta(days=day_offset)
    linhas = []
    for pid, enc in c.hosp.items():
        when = _ext_alta(enc)
        if pid in c.beds and when and c.local(when).date() == target:
            nome, setor = c.where(pid)
            linhas.append(
                _line(
                    f"Encounter/{enc['id']}",
                    f"{nome}: alta prevista às {c.local(when):%H:%M}",
                    pid=pid,
                    setor=setor,
                    refs=[f"Encounter/{enc['id']}"],
                )
            )
    dia = "hoje" if day_offset == 0 else "amanhã"
    return _ind(
        "alta",
        rotulo,
        "altas",
        f"Internados cuja previsão de alta (extensão alta-prevista do Encounter) cai {dia} no fuso do hospital.",
        linhas,
    )


def _latest_assessments(c: Ctx) -> dict[str, DischargeAssessment]:
    if "assessments" not in c.cache:
        latest: dict[str, DischargeAssessment] = {}
        for a in c.db.scalars(select(DischargeAssessment).order_by(DischargeAssessment.id)):
            latest[a.patient_id] = a
        c.cache["assessments"] = latest
    return c.cache["assessments"]


def altas_travadas(c: Ctx) -> dict:
    overridden = {o for (o,) in c.db.execute(select(DischargeOverride.assessment_id))}
    linhas = []
    for pid, a in _latest_assessments(c).items():
        if pid in c.beds and a.final_risk == "alto" and a.confirmed_at is None and a.id not in overridden:
            nome, setor = c.where(pid)
            enc = (c.hosp.get(pid) or {}).get("id")
            linhas.append(
                _line(
                    f"DischargeAssessment/{a.id}",
                    f"{nome}: risco ALTO (LACE {a.lace_score}, social {a.social_score}); motivo da trava: reavaliar "
                    "após as sugestões ou registrar override justificado",
                    pid=pid,
                    setor=setor,
                    refs=[f"Encounter/{enc}"] if enc else [],
                )
            )
    return _ind(
        "alta",
        "Altas travadas",
        "altas",
        "Internados cuja avaliação de alta mais recente é de risco ALTO, ainda sem override justificado e sem alta "
        "confirmada.",
        linhas,
    )


def altas_antes_11h(c: Ctx) -> dict:
    confirmed = [
        a
        for a in c.db.scalars(
            select(DischargeAssessment).where(
                DischargeAssessment.confirmed_at.is_not(None),
                DischargeAssessment.confirmed_at >= c.start,
                DischargeAssessment.confirmed_at <= c.end,
            )
        )
    ]
    early = [a for a in confirmed if c.local(a.confirmed_at).hour < HORA_ALTA_CEDO]
    linhas = []
    for a in early:
        nome, setor = c.where(a.patient_id)
        linhas.append(
            _line(
                f"DischargeAssessment/{a.id}",
                f"{nome}: alta confirmada às {c.local(a.confirmed_at):%H:%M}",
                pid=a.patient_id,
                setor=setor,
                refs=[f"Encounter/{a.encounter_id}"] if a.encounter_id else [],
            )
        )
    return _ind(
        "alta",
        "Altas confirmadas antes das 11h",
        "altas",
        f"Altas confirmadas no período, antes das {HORA_ALTA_CEDO}h (fuso do hospital). Denominador: todas as altas "
        "confirmadas no período.",
        linhas,
        numerador=len(early),
        denominador=len(confirmed),
    )


def tempo_internacao(c: Ctx) -> dict:
    linhas = []
    for pid, bed in c.beds.items():
        start = parse_dt(bed["internado_em"]) if bed.get("internado_em") else None
        if start is None:
            continue
        dias = round((c.now - start).total_seconds() / 86400, 1)
        enc = (c.hosp.get(pid) or {}).get("id")
        linhas.append(
            _line(
                f"Encounter/{enc or pid}",
                f"{bed['nome']} · {bed['setor']} leito {bed['leito']}: {dias} dia(s) de internação",
                pid=pid,
                setor=bed["setor"],
                refs=[f"Encounter/{enc}"] if enc else [],
                peso=dias,
            )
        )
    return _ind(
        "alta",
        "Tempo de internação por setor",
        "dias",
        "Média, por setor, dos dias entre o início da internação (Encounter hospitalar) e agora, dos pacientes "
        "internados.",
        linhas,
        media=True,
    )


# ---------- transição ----------
def discrepancias_medrec(c: Ctx) -> dict:
    runs = c.db.scalars(
        select(MedrecRun).where(MedrecRun.created_at >= c.start, MedrecRun.created_at <= c.end).order_by(MedrecRun.id)
    ).all()
    linhas, por_transicao = [], defaultdict(int)
    for run in runs:
        findings = run.findings_json or {}
        origem = (findings.get("origem") or {}).get("setor") or "—"
        destino = (findings.get("destino") or {}).get("setor") or "—"
        for i, f in enumerate(findings.get("achados", [])):
            if f.get("severidade") != "critico":
                continue
            por_transicao[f"{origem} → {destino}"] += 1
            nome, _ = c.where(run.patient_id)
            linhas.append(
                _line(
                    f"MedrecRun/{run.id}#{i}",
                    f"{nome} ({origem} → {destino}): {f.get('titulo')}",
                    pid=run.patient_id,
                    setor=destino,
                    refs=[r for r in f.get("refs", []) if "/" in r and not r.startswith("Fala/")],
                )
            )
    return _ind(
        "transicao",
        "Discrepâncias críticas de medicação",
        "achados",
        "Achados de severidade crítica das reconciliações do período, por transição (setor de origem → destino).",
        linhas,
        por_transicao=dict(sorted(por_transicao.items())),
    )


def omissoes_conferencia(c: Ctx) -> dict:
    rows = c.db.scalars(
        select(AuditLog)
        .where(AuditLog.action == "handoff.conferencia", AuditLog.created_at >= c.start, AuditLog.created_at <= c.end)
        .order_by(AuditLog.id)
    ).all()
    linhas = []
    for a in rows:
        n = int((a.payload_json or {}).get("omitidos_criticos", 0))
        pid = (a.payload_json or {}).get("patient_id") or a.entity_id
        if n:
            nome, setor = c.where(pid)
            linhas.append(
                _line(
                    f"AuditLog/{a.id}",
                    f"{nome}: {n} omissão(ões) crítica(s) na conferência verbal da passagem",
                    pid=pid,
                    setor=setor,
                    peso=n,
                )
            )
    return _ind(
        "transicao",
        "Omissões críticas na conferência verbal",
        "omissões",
        "Soma de omitidos_criticos do payload de cada conferência (handoff.conferencia) do período.",
        linhas,
    )


# ---------- centro cirúrgico ----------
def _surgeries(c: Ctx) -> list[tuple[Consultation, dict]]:
    if "cirurgias" not in c.cache:
        out = []
        for cons in c.db.scalars(
            select(Consultation)
            .where(
                Consultation.kind == "cirurgia",
                Consultation.sandbox.is_(False),
                Consultation.created_at >= c.start,
                Consultation.created_at <= c.end,
            )
            .order_by(Consultation.id)
        ):
            report = (cons.note_json or {}).get("relatorio_cirurgico") or (cons.insights_json or {}).get(
                "relatorio_cirurgico"
            )
            if report:
                out.append((cons, report))
        c.cache["cirurgias"] = out
    return c.cache["cirurgias"]


def _sector_of(cons: Consultation) -> str:
    return knowledge.SPECIALTIES.get(cons.specialty, cons.specialty)


def relatorios_pendentes(c: Ctx) -> dict:
    linhas = []
    for cons, report in _surgeries(c):
        pront = report.get("prontidao") or {}
        n = len(pront.get("pendencias") or [])
        if n and not pront.get("pronto"):
            linhas.append(
                _line(
                    f"Consultation/{cons.id}",
                    f"Cirurgia nº {cons.id} ({_sector_of(cons)}): {n} pendência(s) de faturamento, "
                    f"{pront.get('criticas', 0)} crítica(s), prontidão {pront.get('score')}",
                    pid=cons.patient_id,
                    setor=_sector_of(cons),
                    refs=[cons.fhir_ref] if cons.fhir_ref else [],
                )
            )
    return _ind(
        "cirurgico",
        "Relatórios cirúrgicos com pendência",
        "relatórios",
        "Relatórios cirúrgicos do período cuja prontidão para faturamento ainda tem pendências.",
        linhas,
    )


def opme_sem_autorizacao(c: Ctx) -> dict:
    linhas = []
    for cons, report in _surgeries(c):
        for item in report.get("opme") or []:
            if item.get("status") == "nao_autorizado":
                linhas.append(
                    _line(
                        f"Consultation/{cons.id}#{item.get('codigo')}",
                        f"Cirurgia nº {cons.id}: {item.get('descricao')} (citado {item.get('citado')}, consumido "
                        f"{item.get('consumido')}) sem autorização na guia",
                        pid=cons.patient_id,
                        setor=_sector_of(cons),
                        refs=[cons.fhir_ref] if cons.fhir_ref else [],
                    )
                )
    return _ind(
        "cirurgico",
        "OPME sem autorização",
        "itens",
        "Itens de OPME dos relatórios do período com status nao_autorizado (usado ou citado e ausente da guia).",
        linhas,
    )


# ---------- ambulatório ----------
def cids_frequentes(c: Ctx) -> dict:
    rows = c.db.scalars(
        select(AuditLog)
        .where(
            AuditLog.action.in_(("consulta.enviada", "consulta.exportada")),
            AuditLog.created_at >= c.start,
            AuditLog.created_at <= c.end,
        )
        .order_by(AuditLog.id)
    ).all()
    sent = {
        int(a.entity_id): a for a in rows if (a.payload_json or {}).get("tempo_ate_envio_s") is not None and a.entity_id
    }
    linhas, contagem, nomes = [], defaultdict(int), {}
    for cid, _ in sorted(sent.items()):
        cons = c.db.get(Consultation, cid)
        if cons is None or cons.kind == "cirurgia":
            continue
        for item in (cons.note_json or {}).get("cids") or []:
            if item.get("tipo") == "hipotese":
                continue
            code = item["codigo"]
            contagem[code] += 1
            nomes[code] = item.get("descricao") or code
            linhas.append(
                _line(
                    f"Consultation/{cons.id}#{code}",
                    f"Consulta nº {cons.id}: {code} — {nomes[code]}",
                    pid=cons.patient_id,
                    setor=_sector_of(cons),
                    refs=[cons.fhir_ref] if cons.fhir_ref else [],
                )
                | {"cid": code}
            )
    ranking = [
        {"cid": code, "descricao": nomes[code], "n": n}
        for code, n in sorted(contagem.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    ]
    return _ind(
        "ambulatorio",
        "CID mais frequentes nas notas enviadas",
        "diagnósticos",
        "CID principais e secundários (hipóteses ficam de fora) das notas enviadas (ou exportadas) no período; "
        "o ranking mostra os 10 mais frequentes.",
        linhas,
        ranking=ranking,
    )


def adesao_protocolos(c: Ctx) -> dict:
    linhas = []
    for patient in c.fhir.list_patients():
        if not avulso.visible(patient["id"], current_org(c.db)):
            continue
        record = PatientRecord.load(c.fhir, patient["id"])
        if record is None:
            continue
        record.now = c.now
        chart = ChartView(record, build_timeline(record), build_context(record), c.now)
        for item in chart.protocols():
            adere = item.status not in NAO_ADERE
            nome = (record.patient.get("name") or [{}])[0].get("text") or patient["id"]
            linhas.append(
                _line(
                    f"{patient['id']}#{item.protocolo}/{item.item}",
                    f"{nome}: {item.protocolo} — {item.item} ({item.status.replace('_', ' ')}"
                    f"{', último ' + item.ultimo if item.ultimo else ''})",
                    pid=patient["id"],
                    setor="Ambulatório",
                    refs=item.refs,
                    peso=1 if adere else 0,
                )
                | {"adere": adere}
            )
    adherent = sum(1 for i in linhas if i["adere"])
    valor = round(100 * adherent / len(linhas), 1) if linhas else None
    return _ind(
        "ambulatorio",
        "Adesão aos protocolos de APS",
        "%",
        "Itens de protocolo (data/reference/care_protocols.csv) em dia ou no alvo ÷ itens de protocolo dos pacientes "
        "com a condição; pendente, sem registro e fora do alvo contam como não aderentes.",
        linhas,
        valor,
        numerador=adherent,
        denominador=len(linhas),
    )


INDICADORES: dict[str, Callable[[Ctx], dict]] = {
    "altas_previstas_hoje": lambda c: _previstas(c, 0, "Altas previstas para hoje"),
    "altas_previstas_amanha": lambda c: _previstas(c, 1, "Altas previstas para amanhã"),
    "altas_travadas": altas_travadas,
    "altas_antes_11h": altas_antes_11h,
    "tempo_internacao_setor": tempo_internacao,
    "discrepancias_medrec": discrepancias_medrec,
    "omissoes_conferencia": omissoes_conferencia,
    "relatorios_pendentes": relatorios_pendentes,
    "opme_sem_autorizacao": opme_sem_autorizacao,
    "cids_frequentes": cids_frequentes,
    "adesao_protocolos": adesao_protocolos,
}
# indicadores de evento (dependem do período): só eles têm série semanal
SERIE = (
    "altas_antes_11h",
    "discrepancias_medrec",
    "omissoes_conferencia",
    "relatorios_pendentes",
    "opme_sem_autorizacao",
)


def context(db: Session, fhir: FhirSource, start: datetime, end: datetime) -> Ctx:
    beds = {row["id"]: row for row in bed_panel(fhir, db)}
    hosp = {
        ref_id(e.get("subject")): e
        for e in fhir.active_encounters()
        if not e.get("partOf") and e.get("class", {}).get("code") == "IMP"
    }
    return Ctx(db, fhir, clock.now(), start, end, beds, {k: v for k, v in hosp.items() if k})


def painel(db: Session, fhir: FhirSource, start: datetime, end: datetime) -> dict:
    c = context(db, fhir, start, end)
    indicadores = {key: fn(c) for key, fn in INDICADORES.items()}
    serie: dict[str, list[dict]] = {}
    for key in SERIE:
        points = []
        for k in range(SEMANAS_DA_SERIE - 1, -1, -1):
            w_end = end - timedelta(days=7 * k)
            week = replace(c, start=w_end - timedelta(days=7), end=w_end, cache={})
            points.append(
                {
                    "semana": c.local(w_end).strftime("%G-W%V"),
                    "inicio": week.start.isoformat(),
                    "fim": week.end.isoformat(),
                    "valor": INDICADORES[key](week)["valor"],
                }
            )
        serie[key] = points
    setores = sorted({s for i in indicadores.values() for s in i["por_setor"]})
    comparacao = [
        {"setor": s, "valores": {k: i["por_setor"].get(s) for k, i in indicadores.items() if s in i["por_setor"]}}
        for s in setores
    ]
    return {
        "periodo": {"inicio": start.isoformat(), "fim": end.isoformat()},
        "gerado_em": c.now.isoformat(),
        "grupos": [
            {"id": g, "rotulo": r, "indicadores": [k for k, i in indicadores.items() if i["grupo"] == g]}
            for g, r in GRUPOS
        ],
        "indicadores": indicadores,
        "serie_semanal": serie,
        "comparacao_setores": comparacao,
    }


def public(report: dict) -> dict:
    """O painel sem as linhas (elas vêm do drill-down de cada indicador)."""
    return {
        **report,
        "indicadores": {k: {f: v for f, v in i.items() if f != "linhas"} for k, i in report["indicadores"].items()},
    }


def flat_rows(report: dict) -> list[dict]:
    out = []
    for key, i in report["indicadores"].items():
        out.append(
            {
                "indicador": key,
                "rotulo": i["rotulo"],
                "setor": "",
                "valor": i["valor"],
                "unidade": i["unidade"],
                "linhas": i["n_linhas"],
                "formula": i["formula"],
            }
        )
        for setor, v in i["por_setor"].items():
            out.append(
                {
                    "indicador": key,
                    "rotulo": i["rotulo"],
                    "setor": setor,
                    "valor": v,
                    "unidade": i["unidade"],
                    "linhas": "",
                    "formula": "por setor",
                }
            )
    return out


def measure_report(report: dict, generated: datetime) -> dict:
    from app.audit import impact

    shaped = {
        "periodo": report["periodo"],
        "metricas": {
            k: {
                "rotulo": i["rotulo"],
                "valor": i["valor"],
                "unidade": i["unidade"],
                "formula": i["formula"],
                "numerador": i.get("numerador"),
                "denominador": i.get("denominador"),
            }
            for k, i in report["indicadores"].items()
        },
    }
    mr = impact.measure_report(shaped, generated, MEASURE_URL)
    refs = sorted({r for i in report["indicadores"].values() for line in i["linhas"] for r in line["fhir_refs"]})
    fhir_refs = [r for r in refs if r.split("/")[0] in ("Patient", "Encounter", "DocumentReference")]
    return mr | {"evaluatedResource": [{"reference": r} for r in fhir_refs]}


# ---------- resumo semanal (IA opcional; no mock, um modelo de texto) ----------
class ResumoFrase(BaseModel):
    texto: str = Field(description="uma frase curta; cada número vem do painel")
    refs: list[str] = Field(description="ids dos indicadores do painel que sustentam a frase")


class ResumoOutput(BaseModel):
    frases: list[ResumoFrase]


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _fmt(v) -> str:
    return str(v).replace(".", ",") if isinstance(v, float) else str(v)


def panel_numbers(report: dict) -> set[str]:
    """Todo número que aparece no painel (valores, numeradores, denominadores, setores, transições, ranking, série)."""
    found: set[float] = set()

    def add(v) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            found.add(round(float(v), 1))

    for i in report["indicadores"].values():
        for k in ("valor", "numerador", "denominador"):
            add(i.get(k))
        for v in i["por_setor"].values():
            add(v)
        for v in (i.get("por_transicao") or {}).values():
            add(v)
        for r in i.get("ranking") or []:
            add(r["n"])
    for points in report["serie_semanal"].values():
        for p in points:
            add(p["valor"])
    return {n for v in found for n in {f"{v:g}", f"{int(v)}" if v.is_integer() else f"{v:g}"}}


_EXTENSO = {
    "dois": 2, "duas": 2, "três": 3, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8,
    "nove": 9, "dez": 10, "onze": 11, "doze": 12, "treze": 13, "catorze": 14, "quatorze": 14, "quinze": 15,
    "dezesseis": 16, "dezessete": 17, "dezoito": 18, "dezenove": 19, "vinte": 20, "trinta": 30, "quarenta": 40,
    "cinquenta": 50, "sessenta": 60, "setenta": 70, "oitenta": 80, "noventa": 90, "cem": 100, "mil": 1000,
}  # fmt: skip
_LIMIAR_11H = re.compile(r"antes das onze horas", re.IGNORECASE)  # limiar fixo do indicador, não é número do painel


def _numbers_of(text: str) -> set[str]:
    """Algarismos e números por extenso ("dez altas" não pode passar por número fora do painel)."""
    text = _LIMIAR_11H.sub("", text)
    found = {f"{float(m.group(0).replace(',', '.')):g}" for m in _NUM.finditer(text)}
    for w in re.findall(r"[a-zçãéê]+", text.lower()):
        if w in _EXTENSO:
            found.add(f"{_EXTENSO[w]:g}")
    return found


def validate_summary(frases: list[ResumoFrase], report: dict) -> tuple[list[dict], list[dict]]:
    """Frase sem indicador citado, com indicador inexistente ou com número que não está no painel é descartada."""
    allowed = panel_numbers(report)
    keep, dropped = [], []
    for f in frases:
        bad_refs = [r for r in f.refs if r not in report["indicadores"]]
        extra = sorted(_numbers_of(f.texto) - allowed)
        if not f.refs or bad_refs:
            dropped.append({"texto": f.texto, "motivo": "sem indicador do painel como fonte"})
        elif extra:
            dropped.append({"texto": f.texto, "motivo": f"número fora do painel: {', '.join(extra)}"})
        else:
            keep.append({"texto": f.texto, "refs": f.refs})
    return keep, dropped


def mock_summary(variables: dict) -> dict:
    """Modelo de texto determinístico: uma frase por indicador relevante, com o número exatamente como no painel."""
    import json

    ind = variables["painel"] if isinstance(variables["painel"], dict) else json.loads(variables["painel"])
    frases = []

    def say(key: str, texto: str) -> None:
        frases.append({"texto": texto, "refs": [key]})

    if (i := ind.get("altas_previstas_hoje")) and i["valor"]:
        say("altas_previstas_hoje", f"{_fmt(i['valor'])} alta(s) prevista(s) para hoje.")
    if (i := ind.get("altas_previstas_amanha")) and i["valor"]:
        say("altas_previstas_amanha", f"{_fmt(i['valor'])} alta(s) prevista(s) para amanhã.")
    if (i := ind.get("altas_travadas")) and i["valor"]:
        say("altas_travadas", f"{_fmt(i['valor'])} alta(s) travada(s) por risco alto sem override.")
    if (i := ind.get("altas_antes_11h")) and i.get("denominador"):
        say(
            "altas_antes_11h",
            f"{_fmt(i['numerador'])} de {_fmt(i['denominador'])} altas confirmadas no período "
            "foram antes das onze horas.",
        )
    if (i := ind.get("discrepancias_medrec")) and i["valor"]:
        say("discrepancias_medrec", f"{_fmt(i['valor'])} discrepância(s) crítica(s) de medicação nas transições.")
    if (i := ind.get("omissoes_conferencia")) and i["valor"]:
        say("omissoes_conferencia", f"{_fmt(i['valor'])} omissão(ões) crítica(s) na conferência verbal da passagem.")
    if (i := ind.get("relatorios_pendentes")) and i["valor"]:
        say("relatorios_pendentes", f"{_fmt(i['valor'])} relatório(s) cirúrgico(s) com pendência de faturamento.")
    if (i := ind.get("opme_sem_autorizacao")) and i["valor"]:
        say("opme_sem_autorizacao", f"{_fmt(i['valor'])} item(ns) de OPME sem autorização na guia.")
    if (i := ind.get("adesao_protocolos")) and i["valor"] is not None:
        say("adesao_protocolos", f"Adesão aos protocolos de APS em {_fmt(i['valor'])}%.")
    return {"frases": frases}


def weekly_summary(report: dict) -> dict:
    compact = {
        k: {
            f: i.get(f)
            for f in ("rotulo", "valor", "unidade", "numerador", "denominador", "por_setor", "por_transicao", "ranking")
            if i.get(f) is not None
        }
        for k, i in report["indicadores"].items()
    }
    result = generate_structured("gestao_resumo", {"painel": compact}, ResumoOutput, mock_summary, "low")
    frases, dropped = validate_summary(result.output.frases, report)
    return {
        "frases": frases,
        "descartados": dropped,
        "meta": {"modo": result.mode, "modelo": result.model, "prompt_version": result.prompt_version},
        "aviso": "Resumo gerado a partir dos números do painel; cada frase cita o indicador de origem.",
    }
