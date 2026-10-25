"""Painel Impacto (#36): adoção, tempo e qualidade da consulta com IA, cada número com fórmula e linhas do audit_log.

Três consultas são feitas pelo caminho real da API (relógio controlado, LLM_MODE=mock) e as métricas são conferidas com
a conta manual feita aqui, linha a linha.
"""

import re
from datetime import UTC, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fhir.resources.R4B.measurereport import MeasureReport
from sqlalchemy import select

from app import clock
from app.audit import impact
from app.consult import service as consult_service
from app.consult.edicao import edit_percent
from app.consult.knowledge import demo_scripts
from app.db import session as db_session
from app.db.models import AuditLog
from tests.conftest import ANCHOR, login
from tests.test_consult import say, start
from tests.test_surgical import dictate
from tests.test_surgical import start as start_surgery

SCRIPT = demo_scripts()["cen6_consulta"]["falas"]
TZ = ZoneInfo("America/Sao_Paulo")
_dia = ANCHOR.astimezone(TZ).date()
# o indicador de expediente é seg-sex: ancora em terça a sexta para que ontem e hoje sejam dias úteis em qualquer data
TODAY = _dia - timedelta(days=(_dia.weekday() - 4) % 7 if _dia.weekday() in (5, 6, 0) else 0)
YESTERDAY = TODAY - timedelta(days=1)


def at(day, hh, mm=0):
    from datetime import datetime

    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ).astimezone(UTC)  # o SQLite guarda sem fuso


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    original = clock.now()
    consult_service.clear_chart_cache()
    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    yield
    clock.freeze(original)
    consult_service.clear_chart_cache()


def consulta(client, h, t_start, t_finish, t_revise, t_send, edit: str | None):
    """Uma consulta completa nos horários dados; `edit` troca o texto do plano (None = nota igual à gerada)."""
    clock.freeze(t_start)
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    clock.freeze(t_finish)
    note = client.post(f"/consultations/{cid}/finish", headers=h).json()["nota"]
    clock.freeze(t_revise)
    if edit:
        note["plano"]["texto"] = edit
    assert client.put(f"/consultations/{cid}/note", json=note | {"atestacao": True}, headers=h).status_code == 200
    clock.freeze(t_send)
    sent = client.post(
        f"/consultations/{cid}/send", json={"justificativa": "Risco avaliado e discutido com a paciente."}, headers=h
    ).json()
    assert sent["enviado"] is True
    return cid


@pytest.fixture
def tres(client):
    """Três consultas, duas pelo médico e uma pela enfermagem, ontem; a terceira é enviada hoje de manhã cedo."""
    medico, enf = login(client, "medico"), login(client, "enfermagem")
    ids = [
        consulta(
            client, medico, at(YESTERDAY, 9), at(YESTERDAY, 9, 20), at(YESTERDAY, 9, 40), at(YESTERDAY, 9, 50),
            "Suspender ibuprofeno e glibenclamida; retorno em 2 semanas.",
        ),
        consulta(
            client, enf, at(YESTERDAY, 10), at(YESTERDAY, 10, 10), at(YESTERDAY, 10, 20), at(YESTERDAY, 10, 30), None
        ),
        consulta(
            client, medico, at(YESTERDAY, 17), at(YESTERDAY, 17, 30), at(TODAY, 7, 20), at(TODAY, 7, 30),
            "Plano totalmente reescrito pelo médico, com novas orientações de retorno e exames de controle.",
        ),
    ]  # fmt: skip
    clock.freeze(at(TODAY, 12))
    return client, ids


def report(client, role="gestor", **params):
    q = {"since": str(YESTERDAY - timedelta(days=1)), "until": str(TODAY)} | params
    res = client.get("/audit/metrics", params=q, headers=login(client, role))
    assert res.status_code == 200, res.text
    return res.json()["consulta_ia"]["metricas"]


def rows_of(action: str) -> list[AuditLog]:
    with db_session.SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


# ---------- (1) volume ----------
def test_volume_matches_the_manual_count(tres):
    client, ids = tres
    m = report(client)
    assert m["atendimentos_apoiados"]["valor"] == 3
    assert m["notas_enviadas"]["valor"] == 3
    # pares (profissional, mês): médico 2 consultas, enfermagem 1 → média 1,5
    assert m["consultas_por_profissional_mes"]["valor"] == 1.5
    tabela = {(t["papel"], t["consultas"]) for t in m["consultas_por_profissional_mes"]["tabela"]}
    assert tabela == {("medico", 2), ("enfermagem", 1)}
    # 3 consultas na janela de 1 semana: nenhum profissional chega a 3/semana; com N=2 só o médico (1 de 2)
    assert m["uso_regular"]["valor"] == 0.0
    regular = report(client, uso_regular_min=2)["uso_regular"]
    assert (regular["valor"], regular["numerador"], regular["denominador"]) == (50.0, 1, 2)


# ---------- (2) tempo ----------
def test_time_metrics_match_the_manual_calculation(tres):
    client, ids = tres
    m = report(client)
    assert m["duracao_media_min"]["valor"] == 20.0  # (20 + 10 + 30) / 3
    assert m["tempo_ate_envio_min"]["valor"] == 296.7  # (30 + 20 + 840) / 3: o terceiro enviou no dia seguinte
    mesmo_dia = m["enviadas_mesmo_dia"]
    assert (mesmo_dia["numerador"], mesmo_dia["denominador"], mesmo_dia["valor"]) == (2, 3, 66.7)
    fora = m["documentacao_fora_expediente"]  # 3 revisões + 3 envios; só a revisão e o envio das 7h20 e 7h30 saem
    assert (fora["numerador"], fora["denominador"], fora["valor"]) == (2, 6, 33.3)


# ---------- (3) qualidade ----------
def test_quality_metrics_use_the_edit_percentage_of_the_review(tres):
    client, ids = tres
    m = report(client)
    pcts = [a.payload_json["editado_pct"] for a in rows_of("consulta.nota_revisada")]
    assert len(pcts) == 3 and pcts[1] == 0.0 and pcts[0] > 0 and pcts[2] > 0
    notas = m["notas_editadas"]
    assert (notas["numerador"], notas["denominador"], notas["valor"]) == (2, 3, 66.7)
    assert m["edicao_media"]["valor"] == round(sum(pcts) / 3, 1)
    # aceite por camada: o roteiro do cenário 6 aborda perguntas sugeridas pelas regras
    perguntas = m["aceite_perguntas"]
    assert perguntas["por_camada"]["regra"]["sugeridas"] > 0
    assert perguntas["por_camada"]["regra"]["aceitas"] > 0
    assert set(perguntas["por_camada"]) == {"regra", "ia"}
    total = sum(c["sugeridas"] for c in perguntas["por_camada"].values())
    assert perguntas["denominador"] == total
    assert set(m["aceite_hipoteses"]["por_camada"]) == {"regra", "ia"}


def test_edit_percentage_by_hand():
    a = {"plano": {"texto": "a b c d", "refs": ["Fala/1"]}}
    b = {"plano": {"texto": "a b x d e", "refs": ["Fala/9"]}}  # c→x (1) e +e (1) sobre 5 palavras
    assert edit_percent(a, b) == (40.0, 2)
    assert edit_percent(a, a) == (0.0, 0)
    assert edit_percent(None, None) == (0.0, 0)


def test_surgical_conformity_before_and_after(client):
    h = login(client)
    clock.freeze(at(YESTERDAY, 8))
    c = start_surgery(client, h)
    dictate(client, h, c["id"], demo_scripts()["cen4b_artroplastia"]["falas"])
    assert client.post(f"/consultations/{c['id']}/finish", headers=h).status_code == 200
    clock.freeze(at(TODAY, 12))
    conf = report(client)["conformidade_cirurgica"]
    row = rows_of("cirurgia.prontidao")[0].payload_json
    assert conf["antes"] == row["antes"]["score"] and conf["depois"] == row["depois"]["score"]
    assert conf["depois"] >= conf["antes"]
    assert conf["n_linhas"] == 1


def test_copilot_acceptance(client):
    h = login(client)
    clock.freeze(at(YESTERDAY, 9))
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    msg = client.post(f"/consultations/{cid}/ask", json={"pergunta": "qual o último potássio?"}, headers=h).json()
    client.post(f"/consultations/{cid}/ask", json={"pergunta": "posso usar AINE?"}, headers=h)
    ins = client.post(
        f"/consultations/{cid}/copiloto/inserir",
        json={"mensagem_id": msg["mensagem"]["id"], "destino": "plano"},
        headers=h,
    )
    assert ins.status_code == 200, ins.text
    clock.freeze(at(TODAY, 12))
    c = report(client)["aceite_copiloto"]
    assert (c["numerador"], c["denominador"], c["valor"]) == (1, 2, 50.0)


# ---------- (4) NPS ----------
def test_nps_is_recorded_aggregated_and_replaced_within_the_month(client):
    clock.freeze(at(TODAY, 12))
    medico, enf, gestor = (login(client, r) for r in ("medico", "enfermagem", "gestor"))
    assert client.get("/feedback/nps", headers=medico).json()["respondido"] is False
    bad = client.post("/feedback", json={"kind": "nps"}, headers=medico)
    assert bad.status_code == 422
    assert client.post("/feedback", json={"kind": "nps", "score": 11}, headers=medico).status_code == 422
    for h, score in ((medico, 10), (enf, 9), (gestor, 3)):
        assert client.post("/feedback", json={"kind": "nps", "score": score}, headers=h).status_code == 200
    assert client.get("/feedback/nps", headers=medico).json() == {
        "mes": TODAY.strftime("%Y-%m"),
        "respondido": True,
        "nota": 10,
    }
    nps = report(client)["nps"]
    assert nps["distribuicao"] == {"promotores": 2, "neutros": 0, "detratores": 1}
    assert nps["valor"] == 33  # (2 − 1) / 3
    # responder de novo no mês troca a nota; a anterior continua na auditoria
    client.post("/feedback", json={"kind": "nps", "score": 4, "comment": "lento"}, headers=medico)
    nps = report(client)["nps"]
    assert nps["distribuicao"] == {"promotores": 1, "neutros": 0, "detratores": 2} and nps["valor"] == -33
    assert len(rows_of("ia.feedback")) == 4
    assert nps["n_linhas"] == 3  # só a resposta vigente de cada profissional compõe o número
    raw = client.get("/audit/metrics", headers=gestor).json()
    assert "nps" not in raw["feedback_ia"]  # o NPS não entra na taxa de utilidade da IA


# ---------- (5) linha de base e horas devolvidas ----------
def test_baseline_form_generates_returned_hours(tres):
    client, ids = tres
    gestor = login(client, "gestor")
    assert report(client)["horas_devolvidas"]["valor"] is None  # sem linha de base não há número
    for role in ("medico", "enfermagem"):
        res = client.post(
            "/audit/impact/baseline",
            json={"setor": "clinica_medica", "minutos_antes": 600},
            headers=login(client, role),
        )
        assert res.status_code == 403
    assert (
        client.post(
            "/audit/impact/baseline", json={"setor": "inexistente", "minutos_antes": 10}, headers=gestor
        ).status_code
        == 422
    )
    ok = client.post("/audit/impact/baseline", json={"setor": "clinica_medica", "minutos_antes": 600}, headers=gestor)
    assert ok.status_code == 200, ok.text
    horas = report(client)["horas_devolvidas"]
    setor = horas["por_setor"][0]
    # 3 notas × (600 − 296,67 min) = 910 min = 15,2 h
    assert setor["atendimentos"] == 3 and setor["minutos_devolvidos"] == 910.0
    assert horas["valor"] == 15.2 and horas["minutos"] == 910.0
    assert client.get("/audit/impact/baseline", headers=gestor).json()[0]["minutos_antes"] == 600
    # a linha de base nova substitui a anterior; a antiga fica no audit_log
    client.post("/audit/impact/baseline", json={"setor": "clinica_medica", "minutos_antes": 300}, headers=gestor)
    assert report(client)["horas_devolvidas"]["minutos"] == 10.0  # 3 × (300 − 296,67)
    assert len(rows_of("impacto.linha_de_base")) == 2


# ---------- (6) drill-down ----------
def test_drill_down_returns_the_audit_rows_that_make_up_each_number(tres):
    client, ids = tres
    gestor = login(client, "gestor")
    params = {"since": str(YESTERDAY - timedelta(days=1)), "until": str(TODAY)}
    m = report(client)
    for key, m_ in m.items():
        res = client.get(f"/audit/metrics/{key}/linhas", params=params, headers=gestor)
        assert res.status_code == 200, (key, res.text)
        body = res.json()
        assert body["total"] == m_["n_linhas"] == len(body["items"]), key
        assert body["formula"] == m_["formula"] and body["valor"] == m_["valor"]
    lines = client.get("/audit/metrics/duracao_media_min/linhas", params=params, headers=gestor).json()["items"]
    assert [i["id"] for i in lines] == [a.id for a in rows_of("consulta.finalizada")]
    assert {i["action"] for i in lines} == {"consulta.finalizada"}
    # a conta fecha com as linhas: média de duracao_s das linhas = valor
    assert round(sum(i["payload"]["duracao_s"] for i in lines) / len(lines) / 60, 1) == m["duracao_media_min"]["valor"]
    fora = client.get("/audit/metrics/documentacao_fora_expediente/linhas", params=params, headers=gestor).json()
    assert {i["action"] for i in fora["items"]} == {"consulta.nota_revisada", "consulta.enviada"}
    assert len(fora["items"]) == 6
    assert client.get("/audit/metrics/nao_existe/linhas", headers=gestor).status_code == 404
    assert client.get("/audit/metrics/nps/linhas", headers=login(client, "medico")).status_code == 403


# ---------- (7) CSV e MeasureReport ----------
def test_csv_export_has_formula_and_line_count(tres):
    client, ids = tres
    res = client.get(
        "/audit/metrics/export.csv",
        params={"since": str(YESTERDAY - timedelta(days=1)), "until": str(TODAY)},
        headers=login(client, "gestor"),
    )
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    lines = res.text.lstrip("﻿").splitlines()
    assert lines[0] == "metrica;rotulo;valor;unidade;numerador;denominador;linhas_audit;formula"
    row = next(r for r in lines if r.startswith("duracao_media_min;"))
    assert ";20.0;min;" in row and "consulta finalizada" in row
    assert any(r.startswith("aceite_perguntas.regra;") for r in lines)


def test_measure_report_validates_as_fhir_r4(tres):
    client, ids = tres
    res = client.get(
        "/audit/metrics/measure-report",
        params={"since": str(YESTERDAY - timedelta(days=1)), "until": str(TODAY)},
        headers=login(client, "gestor"),
    )
    assert res.status_code == 200, res.text
    data = res.json()
    mr = MeasureReport.model_validate(data)  # modelos R4B, idênticos para este recurso
    assert mr.type == "summary" and mr.status == "complete"
    groups = {g.code.coding[0].code: g for g in mr.group}
    assert groups["duracao_media_min"].measureScore.value == 20.0
    uso = groups["enviadas_mesmo_dia"]
    assert {p.code.coding[0].code: p.count for p in uso.population} == {"numerator": 2, "denominator": 3}
    assert client.get("/audit/metrics/measure-report", headers=login(client, "medico")).status_code == 403


# ---------- (8) rótulos e filtros ----------
def test_consulta_actions_are_filterable_and_labelled_in_the_screen(tres):
    client, ids = tres
    gestor = login(client, "gestor")
    only = client.get("/audit", params={"action": "consulta.", "limit": 500}, headers=gestor).json()
    assert only["items"] and all(i["action"].startswith("consulta.") for i in only["items"])
    web = Path(__file__).resolve().parents[2] / "web" / "components" / "audit" / "audit-view.tsx"
    source = web.read_text(encoding="utf-8")
    emitted = {i["action"] for i in only["items"]}
    code = "\n".join(
        f.read_text(encoding="utf-8") for f in (Path(impact.__file__).resolve().parents[1] / "consult").glob("*.py")
    )
    emitted |= set(re.findall(r'"(consulta\.[a-z_]+)"', code)) | {
        f"consulta.pedido_{a}" for a in ("assinado", "recusado", "revertido")
    }
    missing = sorted(a for a in emitted if f'"{a}"' not in source)
    assert missing == [], f"ações sem rótulo em audit-view.tsx: {missing}"
    assert '"consulta."' in source  # filtro por prefixo na tela
