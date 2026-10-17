"""Copiloto do gestor (#37): indicadores de alta, transição, centro cirúrgico e ambulatório, cada um com sua fonte.

Os números são conferidos contra as fixtures (cenários em data/scenarios) e contra o que as próprias APIs devolveram
ao serem usadas (avaliações de alta, reconciliações, conferências, consultas e relatórios cirúrgicos).
"""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fhir.resources.R4B.measurereport import MeasureReport

from app import clock
from app.consult import service as consult_service
from app.consult.knowledge import demo_scripts
from app.gestao import service as gestao
from app.seed.builder import parse_offset
from tests.conftest import ANCHOR, login
from tests.test_consult import say, start
from tests.test_discharge import GOOD, RISKY
from tests.test_surgical import dictate
from tests.test_surgical import start as start_surgery

TZ = ZoneInfo("America/Sao_Paulo")
SCRIPT = demo_scripts()["cen6_consulta"]["falas"]
DATA = __import__("pathlib").Path(__file__).resolve().parents[3] / "data" / "scenarios"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    original = clock.now()
    consult_service.clear_chart_cache()
    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    yield
    clock.freeze(original)
    consult_service.clear_chart_cache()


@pytest.fixture
def painel(client):
    """Estado do dia: alta travada (cen3), alta confirmada às 9h (cen4), reconciliação, conferência, cirurgia e nota."""
    medico, enf, gestor = (login(client, r) for r in ("medico", "enfermagem", "gestor"))
    clock.freeze(ANCHOR.astimezone(TZ).replace(hour=9, minute=0).astimezone(UTC))
    assert (
        client.post("/patients/cen3/discharge-assessment", json={"social_checklist": RISKY}, headers=medico).status_code
        == 200
    )
    a4 = client.post("/patients/cen4/discharge-assessment", json={"social_checklist": GOOD}, headers=medico).json()
    assert client.post(f"/discharge-assessments/{a4['id']}/confirm", headers=medico).status_code == 200
    run = client.post("/patients/cen2/medrec", json={}, headers=medico).json()
    texto = "PA subindo até 184/102, comunicado plantonista. Risco de queda alto."
    conf = client.post(
        "/patients/cen1/handoff/check", json={"audience": "enfermagem", "texto": texto}, headers=enf
    ).json()
    cirurgia = start_surgery(client, medico)
    dictate(client, medico, cirurgia["id"], demo_scripts()["cen4b_artroplastia"]["falas"])
    client.post(f"/consultations/{cirurgia['id']}/finish", headers=medico)
    cid = start(client, medico)["id"]
    say(client, medico, cid, SCRIPT)
    note = client.post(f"/consultations/{cid}/finish", headers=medico).json()["nota"]
    client.put(f"/consultations/{cid}/note", json=note | {"atestacao": True}, headers=medico)
    sent = client.post(
        f"/consultations/{cid}/send",
        json={"justificativa": "Risco avaliado e discutido com a paciente."},
        headers=medico,
    ).json()
    assert sent["enviado"] is True
    clock.freeze(ANCHOR.astimezone(TZ).replace(hour=15, minute=1).astimezone(UTC))
    return client, gestor, {"run": run, "conf": conf, "cirurgia": cirurgia["id"], "nota": note, "cid": cid}


def get(client, gestor, path="", **params):
    res = client.get(f"/gestao{path}", params=params, headers=gestor)
    assert res.status_code == 200, res.text
    return res.json()


def refs(lines):
    return {r for line in lines for r in line["fhir_refs"]}


# ---------- acesso ----------
@pytest.mark.parametrize("role", ["medico", "enfermagem"])
@pytest.mark.parametrize(
    "path,method",
    [
        ("", "get"),
        ("/indicadores/altas_travadas/linhas", "get"),
        ("/export.csv", "get"),
        ("/measure-report", "get"),
        ("/resumo-semanal", "post"),
    ],
)
def test_only_the_manager_profile_reaches_gestao(client, role, path, method):
    res = getattr(client, method)(f"/gestao{path}", headers=login(client, role))
    assert res.status_code == 403


def test_no_token_is_401(client):
    assert client.get("/gestao").status_code == 401


# ---------- (1) alta ----------
def test_discharge_numbers_match_the_fixtures(painel):
    client, gestor, _ = painel
    r = get(client, gestor)["indicadores"]
    # previsão de alta vem do próprio cenário (internacao.alta_prevista, deslocamento sobre a âncora de 15h)
    expected = {0: set(), 1: set()}
    today = ANCHOR.astimezone(TZ).date()
    for path in DATA.glob("cenario*.json"):
        s = json.loads(path.read_text(encoding="utf-8"))
        if "internacao" in s and s["internacao"].get("alta_prevista"):
            day = (ANCHOR + parse_offset(s["internacao"]["alta_prevista"])).astimezone(TZ).date()
            expected.setdefault((day - today).days, set()).add(s["id"])
    assert expected[0] == {"cen4", "cen4c"} and expected[1] == {"cen3", "cen4b"}
    for key, day in (("altas_previstas_hoje", 0), ("altas_previstas_amanha", 1)):
        lines = get(client, gestor, f"/indicadores/{key}/linhas")["items"]
        assert {line["paciente"] for line in lines} == expected[day], key
        assert r[key]["valor"] == len(expected[day])
        assert all(any(x.startswith("Encounter/") for x in line["fhir_refs"]) for line in lines)
    # alta travada: só a cen3 (risco alto sem override); a cen4 foi confirmada
    travadas = get(client, gestor, "/indicadores/altas_travadas/linhas")
    assert [line["paciente"] for line in travadas["items"]] == ["cen3"] and r["altas_travadas"]["valor"] == 1
    assert "risco ALTO" in travadas["items"][0]["descricao"] and "motivo da trava" in travadas["items"][0]["descricao"]
    # alta antes das 11h: a cen4 foi confirmada às 9h
    cedo = r["altas_antes_11h"]
    assert (cedo["valor"], cedo["numerador"], cedo["denominador"]) == (1, 1, 1)
    # tempo de internação por setor: média de dias dos internados do setor (cen3: 6d3h → 6,1 em Enfermaria 3B)
    tempo = get(client, gestor, "/indicadores/tempo_internacao_setor/linhas")["items"]
    cen3 = next(line for line in tempo if line["paciente"] == "cen3")
    assert cen3["peso"] == 6.1 and cen3["setor"] == "Enfermaria 3B"
    por_setor = r["tempo_internacao_setor"]["por_setor"]
    in_ward = [line["peso"] for line in tempo if line["setor"] == "Enfermaria 3B"]
    assert por_setor["Enfermaria 3B"] == round(sum(in_ward) / len(in_ward), 1)


def test_override_unlocks_the_blocked_discharge(painel):
    client, gestor, _ = painel
    medico = login(client)
    a = client.get("/patients/cen3/discharge-assessment/latest", headers=medico).json()
    ok = client.post(f"/discharge-assessments/{a['id']}/override", json={"justification": "x" * 40}, headers=medico)
    assert ok.status_code == 200
    assert get(client, gestor)["indicadores"]["altas_travadas"]["valor"] == 0


# ---------- (2) transição ----------
def test_transition_numbers_come_from_medrec_and_the_verbal_check(painel):
    client, gestor, made = painel
    r = get(client, gestor)["indicadores"]
    critical = [a for a in made["run"]["achados"] if a["severidade"] == "critico"]
    assert critical and r["discrepancias_medrec"]["valor"] == len(critical)
    assert r["discrepancias_medrec"]["por_transicao"] == {"UTI Adulto → Enfermaria 3B": len(critical)}
    lines = get(client, gestor, "/indicadores/discrepancias_medrec/linhas")["items"]
    assert {line["paciente"] for line in lines} == {"cen2"}
    assert any(x.startswith(("MedicationRequest/", "MedicationStatement/")) for x in refs(lines))
    omitted = made["conf"]["contagem"]
    expected = sum(i["status"] == "omitido" and i["severidade"] == "critico" for i in made["conf"]["itens"])
    assert r["omissoes_conferencia"]["valor"] == expected
    assert omitted["omitido"] >= expected
    if expected:
        line = get(client, gestor, "/indicadores/omissoes_conferencia/linhas")["items"][0]
        assert line["paciente"] == "cen1" and "Patient/cen1" in line["fhir_refs"]


# ---------- (3) centro cirúrgico ----------
def test_surgical_pending_reports_and_opme_without_authorization(painel):
    client, gestor, made = painel
    r = get(client, gestor)["indicadores"]
    consulta = client.get(f"/consultations/{made['cirurgia']}", headers=login(client)).json()
    report = (consulta.get("nota") or {}).get("relatorio_cirurgico") or consulta["insights"]["relatorio_cirurgico"]
    pend = len(report["prontidao"]["pendencias"])
    assert (r["relatorios_pendentes"]["valor"] == 1) == (pend > 0 and not report["prontidao"]["pronto"])
    unauthorized = [o for o in report["opme"] if o["status"] == "nao_autorizado"]
    assert r["opme_sem_autorizacao"]["valor"] == len(unauthorized)
    lines = get(client, gestor, "/indicadores/relatorios_pendentes/linhas")["items"]
    assert all(line["paciente"] == "cen4b" for line in lines)


# ---------- (4) ambulatório ----------
def test_ambulatory_icd_ranking_and_protocol_adherence(painel):
    client, gestor, made = painel
    r = get(client, gestor)["indicadores"]
    codes = [c["codigo"] for c in made["nota"]["cids"] if c.get("tipo") != "hipotese"]
    assert codes
    ranking = {x["cid"]: x["n"] for x in r["cids_frequentes"]["ranking"]}
    assert ranking == {c: 1 for c in codes}
    adesao = r["adesao_protocolos"]
    assert adesao["denominador"] > 0 and 0 <= adesao["valor"] <= 100
    lines = get(client, gestor, "/indicadores/adesao_protocolos/linhas")["items"]
    assert len(lines) == adesao["denominador"] and sum(line["peso"] for line in lines) == adesao["numerador"]
    assert all(line["fhir_refs"] for line in lines)


# ---------- (5) série semanal e comparação ----------
def test_weekly_series_and_sector_comparison(painel):
    client, gestor, _ = painel
    body = get(client, gestor)
    serie = body["serie_semanal"]["altas_antes_11h"]
    assert len(serie) == 4 and serie[-1]["valor"] == 1 and [p["valor"] for p in serie[:-1]] == [0, 0, 0]
    assert serie[0]["semana"] < serie[-1]["semana"]
    setores = {row["setor"] for row in body["comparacao_setores"]}
    assert {"Enfermaria 3B", "Clínica Cirúrgica 4A"} <= setores
    row = next(r for r in body["comparacao_setores"] if r["setor"] == "Enfermaria 3B")
    assert row["valores"]["altas_travadas"] == 1


# ---------- (6) drill-down ----------
def test_every_number_leads_to_its_lines_and_fhir_resources(painel):
    client, gestor, _ = painel
    body = get(client, gestor)
    for key, ind in body["indicadores"].items():
        drill = get(client, gestor, f"/indicadores/{key}/linhas")
        assert drill["total"] == ind["n_linhas"] == len(drill["items"]), key
        assert drill["formula"] == ind["formula"]
        for line in drill["items"]:
            assert line["fhir_refs"] and all("/" in x for x in line["fhir_refs"]), (key, line)
        if key in ("altas_travadas", "altas_previstas_hoje", "tempo_internacao_setor"):
            assert drill["total"] > 0
    assert "linhas" not in body["indicadores"]["altas_travadas"]  # o painel só traz a contagem
    assert client.get("/gestao/indicadores/nao_existe/linhas", headers=gestor).status_code == 404


# ---------- (7) CSV e MeasureReport ----------
def test_csv_and_measure_report(painel):
    client, gestor, _ = painel
    csv_res = client.get("/gestao/export.csv", headers=gestor)
    assert csv_res.status_code == 200 and csv_res.headers["content-type"].startswith("text/csv")
    lines = csv_res.text.lstrip("﻿").splitlines()
    assert lines[0] == "indicador;rotulo;setor;valor;unidade;linhas;formula"
    assert any(line.startswith("altas_travadas;Altas travadas;;1;altas;1;") for line in lines)
    mr = MeasureReport.model_validate(client.get("/gestao/measure-report", headers=gestor).json())
    assert mr.type == "summary" and mr.measure.endswith("gestao-transicao-cuidado")
    groups = {g.code.coding[0].code: g for g in mr.group}
    assert groups["altas_travadas"].measureScore.value == 1
    cedo = groups["altas_antes_11h"]
    assert {p.code.coding[0].code: p.count for p in cedo.population} == {"numerator": 1, "denominator": 1}
    evaluated = {e.reference for e in mr.evaluatedResource}
    assert "Patient/cen3" in evaluated and any(x.startswith("Encounter/") for x in evaluated)


# ---------- (8) resumo semanal ----------
def test_weekly_summary_only_cites_numbers_that_are_in_the_panel(painel):
    client, gestor, _ = painel
    body = get(client, gestor)
    resumo = client.post("/gestao/resumo-semanal", headers=gestor).json()
    assert resumo["frases"] and resumo["meta"]["modo"] == "mock" and resumo["descartados"] == []
    allowed = gestao.panel_numbers({"indicadores": body["indicadores"], "serie_semanal": body["serie_semanal"]})
    for frase in resumo["frases"]:
        assert frase["refs"] and all(ref in body["indicadores"] for ref in frase["refs"])
        assert gestao._numbers_of(frase["texto"]) <= allowed, frase
    texts = " ".join(f["texto"] for f in resumo["frases"])
    assert "1 alta(s) travada(s)" in texts


def test_summary_validator_drops_invented_numbers_and_missing_sources(painel):
    client, gestor, _ = painel
    body = get(client, gestor)
    frases = [
        gestao.ResumoFrase(texto="1 alta(s) travada(s) por risco alto.", refs=["altas_travadas"]),
        gestao.ResumoFrase(texto="47 altas travadas, o dobro da semana passada.", refs=["altas_travadas"]),
        gestao.ResumoFrase(texto="O setor está bem.", refs=[]),
        gestao.ResumoFrase(texto="1 alta travada.", refs=["indicador_inventado"]),
        gestao.ResumoFrase(texto="Relatório de 12,5% de glosa.", refs=["opme_sem_autorizacao"]),
    ]
    keep, dropped = gestao.validate_summary(frases, body)
    assert [k["texto"] for k in keep] == ["1 alta(s) travada(s) por risco alto."]
    motivos = " | ".join(d["motivo"] for d in dropped)
    assert len(dropped) == 4 and "número fora do painel: 47" in motivos and "sem indicador" in motivos


def test_summary_validator_catches_numbers_written_in_words(painel):
    client, gestor, _ = painel
    body = get(client, gestor)
    frases = [gestao.ResumoFrase(texto="Cem altas travadas na semana.", refs=["altas_travadas"])]
    keep, dropped = gestao.validate_summary(frases, body)
    assert not keep and "número fora do painel: 100" in dropped[0]["motivo"]


def test_summary_from_the_model_is_filtered_before_it_reaches_the_screen(painel, monkeypatch):
    from app.llm.client import LLMResult

    client, gestor, _ = painel

    def fake(prompt, variables, schema, mock, effort=None):
        out = schema.model_validate(
            {
                "frases": [
                    {"texto": "1 alta(s) travada(s).", "refs": ["altas_travadas"]},
                    {"texto": "Foram 99 altas travadas.", "refs": ["altas_travadas"]},
                ]
            }
        )
        return LLMResult(output=out, model="teste", prompt_version="x", mode="real")

    monkeypatch.setattr(gestao, "generate_structured", fake)
    resumo = client.post("/gestao/resumo-semanal", headers=gestor).json()
    assert [f["texto"] for f in resumo["frases"]] == ["1 alta(s) travada(s)."]
    assert len(resumo["descartados"]) == 1 and "99" in resumo["descartados"][0]["motivo"]


def test_period_filter_limits_event_indicators(painel):
    client, gestor, _ = painel
    today = ANCHOR.astimezone(TZ).date()
    old = get(client, gestor, since=str(today - timedelta(days=30)), until=str(today - timedelta(days=20)))
    assert old["indicadores"]["altas_antes_11h"]["valor"] == 0
    assert old["indicadores"]["discrepancias_medrec"]["valor"] == 0
    assert old["indicadores"]["altas_travadas"]["valor"] == 1  # estado atual, não depende do período
    assert datetime.fromisoformat(old["periodo"]["fim"]).date() <= today
