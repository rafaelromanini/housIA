import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.auth.deps import require_roles
from app.db.models import User
from app.db.session import get_db
from app.fhir_client.client import FhirSource, get_fhir
from app.gestao import service
from app.llm.client import LLMError

router = APIRouter(prefix="/gestao", tags=["gestão"])
gestor = require_roles("gestor")


def _report(db: Session, fhir: FhirSource, since: date | None, until: date | None) -> dict:
    start, end = service.period(since, until)
    return service.painel(db, fhir, start, end)


@router.get("")
def get_painel(
    since: date | None = None,
    until: date | None = None,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    _: User = Depends(gestor),
) -> dict:
    """Indicadores de alta, transição, centro cirúrgico e ambulatório, com a fórmula de cada um, série semanal e
    comparação entre setores. As linhas de cada número vêm em /gestao/indicadores/{id}/linhas."""
    return service.public(_report(db, fhir, since, until))


@router.get("/indicadores/{indicador}/linhas")
def get_lines(
    indicador: str,
    since: date | None = None,
    until: date | None = None,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    _: User = Depends(gestor),
) -> dict:
    """Drill-down: as linhas (paciente, setor e recursos FHIR) que compõem o número de um indicador."""
    report = _report(db, fhir, since, until)
    i = report["indicadores"].get(indicador)
    if i is None:
        raise HTTPException(404, f"Indicador desconhecido. Use um de: {', '.join(report['indicadores'])}")
    return {
        "indicador": indicador,
        "rotulo": i["rotulo"],
        "valor": i["valor"],
        "unidade": i["unidade"],
        "formula": i["formula"],
        "total": len(i["linhas"]),
        "items": i["linhas"],
    }


@router.get("/export.csv")
def export_csv(
    since: date | None = None,
    until: date | None = None,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    _: User = Depends(gestor),
) -> Response:
    out = io.StringIO()
    fields = ["indicador", "rotulo", "setor", "valor", "unidade", "linhas", "formula"]
    writer = csv.DictWriter(out, fieldnames=fields, delimiter=";")
    writer.writeheader()
    writer.writerows(service.flat_rows(_report(db, fhir, since, until)))
    return Response(
        "﻿" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="gestao-housia.csv"'},
    )


@router.get("/measure-report")
def measure_report(
    since: date | None = None,
    until: date | None = None,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    _: User = Depends(gestor),
) -> dict:
    """O painel como FHIR R4 MeasureReport; evaluatedResource lista os Patient/Encounter que compõem os números."""
    return service.measure_report(_report(db, fhir, since, until), clock.now())


@router.post("/resumo-semanal")
def weekly_summary(
    since: date | None = None,
    until: date | None = None,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(gestor),
) -> dict:
    """Resumo semanal opcional da IA (no mock, um modelo de texto). Só sai frase que cita um indicador do painel e
    só com números que estão no painel."""
    report = _report(db, fhir, since, until)
    try:
        resumo = service.weekly_summary(report)
    except LLMError as exc:
        log_action(db, user, "gestao.erro_llm", "gestao", None, {"erro": str(exc)})
        raise HTTPException(502, str(exc)) from exc
    log_action(
        db,
        user,
        "gestao.resumo_semanal",
        "gestao",
        None,
        {"frases": len(resumo["frases"]), "descartadas": len(resumo["descartados"]), **resumo["meta"]},
    )
    return resumo
