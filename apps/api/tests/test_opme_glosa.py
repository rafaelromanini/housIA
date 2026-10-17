"""OPME em três vias com guia autorizada e rastreio do implante (backlog 25); motor anti-glosa em dados e prontidão
para faturamento (backlog 26).

Tudo pelo caminho real (API → Engine → opme.py/antiglosa.py) sobre os cenários sintéticos, em LLM_MODE=mock.
"""

import csv
import shutil
from datetime import date

import pytest
from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.claim import Claim
from sqlalchemy import select

from app.config import get_settings
from app.consult import antiglosa, gs1, opme
from app.consult import service as consult_service
from app.consult.knowledge import demo_scripts
from app.db import session as db_session
from app.db.models import AuditLog
from tests.conftest import CONSENT
from tests.test_surgical import dictate, report_of, start

LABEL = "(01)07891234567895(17)271231(10)LOTE123(21)SN1"
FEMUR = "(01)02990000000125(17)301231(10)FEM123(21)F001"  # DEMO-OPME-012
TIBIA = "(01)02990000000132(17)301231(10)TIB456(21)T002"  # DEMO-OPME-013
POLY = "(01)02990000000149(17)301231(10)POL789(21)P003"  # DEMO-OPME-014
CEMENT = "(01)02990000000156(17)301231(10)CIM001"  # DEMO-OPME-015 (sem série)


@pytest.fixture(autouse=True)
def _fresh_state():
    consult_service.clear_chart_cache()
    antiglosa.clear_cache()
    yield
    consult_service.clear_chart_cache()
    antiglosa.clear_cache()


def line(rep: dict, code: str) -> dict:
    return next(o for o in rep["opme"] if o["codigo"] == code)


def rules(rep: dict) -> dict[str, dict]:
    return {p["regra"]: p for p in rep["prontidao"]["pendencias"]}


def put_guide(client, h, cid, **body):
    return client.put(f"/consultations/{cid}/opme-autorizado", json=body, headers=h)


def scan(client, h, cid, label, **body):
    return client.post(f"/consultations/{cid}/opme-consumo", json={"etiqueta": label} | body, headers=h)


def audit_rows(action: str) -> list[dict]:
    with db_session.SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)).all()
        return [r.payload_json for r in rows]


def new_line(code: str) -> dict:
    return {"codigo": code, "descricao": code, "autorizado": 0, "citado": 1, "status": "nao_autorizado", "refs": []}


def cleanup(store, refs):
    for ref in refs:
        rtype, rid = ref.split("/")
        store.resources[rtype].pop(rid, None)


# ======================= 25: etiqueta GS1 =======================
def test_gs1_parser_reads_human_and_raw_labels():
    today = date(2026, 10, 16)
    et = gs1.parse(LABEL, today)
    assert (et.gtin, et.lote, et.validade, et.serie) == ("07891234567895", "LOTE123", "2027-12-31", "SN1")
    assert et.gtin_valido is True and et.avisos == []
    # leitura bruta de DataMatrix: símbolo de simbologia na frente e GS (FNC1) depois do campo variável
    raw = "]d2" + "0107891234567895" + "17271231" + "10LOTE123" + "\x1d" + "21SN1"
    assert gs1.parse(raw, today).as_dict() == et.as_dict()
    assert gs1.parse("01078912345678951727123110LOTE123\x1d21SN1", today).serie == "SN1"
    # dia 00 = último dia do mês; GTIN com dígito errado é avisado, não recusado
    last = gs1.parse("(01)07891234567896(17)260200(10)A", today)
    assert last.validade == "2026-02-28" and last.gtin_valido is False and "Dígito verificador" in last.avisos[0]
    assert gs1.parse("(17)261301(10)A", today).avisos and gs1.parse("(17)261301(10)A", today).validade is None
    for bad in ("", "texto qualquer", "(99)abc"):
        with pytest.raises(gs1.Gs1Error):
            gs1.parse(bad, today)


# ======================= 25: guia + ditado fora do roteiro =======================
def test_manual_guide_plus_dictation_gives_correct_reconciliation_outside_the_script(client, auth):
    h = auth("medico")
    c = start(client, h, pid="cen1", especialidade="cirurgia_geral")
    assert c["roteiro_demo"] is None  # nenhum roteiro: antes, toda OPME citada virava "sem autorização"
    cid = c["id"]
    # autocomplete do catálogo
    found = client.get("/consult/opme-catalog?q=trocarte", headers=h).json()
    assert {f["codigo"] for f in found} == {"DEMO-OPME-001", "DEMO-OPME-002"}
    res = put_guide(
        client,
        h,
        cid,
        numero="GUIA-123",
        itens=[
            {"codigo": "DEMO-OPME-001", "quantidade": 1},
            {"codigo": "DEMO-OPME-002", "quantidade": 2},
            {"codigo": "DEMO-OPME-005", "quantidade": 2},
            {
                "codigo": "FORN-FIO-01",
                "quantidade": 1,
                "descricao": "Fio especial do fornecedor",
                "palavras": ["fio especial"],
            },
        ],
    )
    assert res.status_code == 200, res.text
    assert res.json()["guia"]["origem"] == "manual" and res.json()["guia"]["numero"] == "GUIA-123"
    rep = report_of(
        dictate(
            client,
            h,
            cid,
            [
                ("medico", "Procedimento realizado: colecistectomia videolaparoscópica."),
                ("medico", "Colocado um trocarte de 10 e dois trocartes de 5. Ligadura com dois endoloops."),
                ("medico", "Usado fio especial no fechamento. Usado também bisturi ultrassônico."),
            ],
        )["insights"]
    )
    assert [(o["codigo"], o["autorizado"], o["citado"], o["status"]) for o in rep["opme"]] == [
        ("DEMO-OPME-001", 1, 1, "conferido"),
        ("DEMO-OPME-002", 2, 2, "conferido"),
        ("DEMO-OPME-005", 2, 2, "conferido"),
        ("FORN-FIO-01", 1, 1, "conferido"),
        ("DEMO-OPME-009", 0, 1, "nao_autorizado"),
    ]
    assert line(rep, "FORN-FIO-01")["descricao"] == "Fio especial do fornecedor"
    assert (
        line(rep, "DEMO-OPME-009")["status_guia"] == "ausente"
        and line(rep, "DEMO-OPME-009")["status_fala"] == "divergente"
    )
    assert any("sem autorização na guia" in p["texto"] for p in rep["pendencias"] if p["severidade"] == "critico")
    # a guia fica na consulta e as regras de faturamento enxergam o número dela
    got = client.get(f"/consultations/{cid}", headers=h).json()
    assert got["opme_guia"]["numero"] == "GUIA-123" and rep["prontidao"]["numero_guia"] == "GUIA-123"
    # entrada inválida
    assert put_guide(client, h, cid, itens=[{"codigo": "FORN-X", "quantidade": 1}]).status_code == 422  # sem descrição
    assert put_guide(client, h, cid, itens=[], tiss_xml="<a/>").status_code == 422  # duas origens
    assert client.put(f"/consultations/{cid}/opme-autorizado", json={}, headers=h).status_code == 422
    # só vale em cirurgia em andamento
    consulta = client.post(
        "/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=h
    ).json()
    assert put_guide(client, h, consulta["id"], itens=[{"codigo": "DEMO-OPME-001"}]).status_code == 409


def test_quantity_divergence_is_flagged(client, auth):
    h = auth("medico")
    cid = start(client, h, pid="cen1", especialidade="cirurgia_geral")["id"]
    put_guide(
        client,
        h,
        cid,
        itens=[{"codigo": "DEMO-OPME-002", "quantidade": 2}, {"codigo": "DEMO-OPME-005", "quantidade": 2}],
    )
    rep = report_of(
        dictate(client, h, cid, [("medico", "Colocados três trocartes de 5. Ligadura com dois endoloops.")])["insights"]
    )
    div = line(rep, "DEMO-OPME-002")
    assert (div["citado"], div["autorizado"], div["status"], div["status_fala"], div["status_guia"]) == (
        3,
        2,
        "quantidade_divergente",
        "divergente",
        "divergente",
    )
    assert any("3 citado(s) × 2 autorizado(s)" in p["texto"] for p in rep["pendencias"])
    assert line(rep, "DEMO-OPME-005")["status"] == "conferido"


def test_guide_import_from_claim_response_and_tiss_xml(client, auth):
    h = auth("medico")
    cid = start(client, h, pid="cen1", especialidade="cirurgia_geral")["id"]
    claim_response = {
        "resourceType": "ClaimResponse",
        "status": "active",
        "outcome": "complete",
        "preAuthRef": "AUT-9",
        "addItem": [
            {
                "productOrService": {"coding": [{"code": "DEMO-OPME-002", "display": "Trocarte"}]},
                "quantity": {"value": 2},
            },
            {
                "productOrService": {"coding": [{"code": "FORN-CLIP", "display": "Clipe especial"}]},
                "quantity": {"value": 3},
            },
            {
                "productOrService": {"coding": [{"code": "DEMO-OPME-009"}]},
                "quantity": {"value": 1},
                "adjudication": [{"category": {"coding": [{"code": "denied"}]}}],
            },
        ],
    }
    res = put_guide(client, h, cid, claim_response=claim_response)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["guia"]["origem"] == "claimresponse" and body["guia"]["numero"] == "AUT-9"
    assert {a["codigo"]: a["quantidade"] for a in body["autorizado"]} == {"DEMO-OPME-002": 2, "FORN-CLIP": 3}
    assert {o["codigo"]: o["autorizado"] for o in report_of(body["insights"])["opme"]} == {
        "DEMO-OPME-002": 2,
        "FORN-CLIP": 3,
    }
    assert (
        put_guide(client, h, cid, claim_response={"resourceType": "ClaimResponse", "outcome": "error"}).status_code
        == 422
    )
    assert put_guide(client, h, cid, claim_response={"resourceType": "Patient"}).status_code == 422

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <ans:mensagemTISS xmlns:ans="http://www.ans.gov.br/padroes/tiss/schemas">
      <ans:autorizacao>
        <ans:numeroGuiaOperadora>778899</ans:numeroGuiaOperadora><ans:senha>S1</ans:senha>
        <ans:opmeAutorizadas>
          <ans:opme><ans:codigoProduto>DEMO-OPME-001</ans:codigoProduto>
            <ans:descricaoProduto>Trocarte 10</ans:descricaoProduto>
            <ans:quantidadeAutorizada>1</ans:quantidadeAutorizada></ans:opme>
          <ans:opme><ans:codigoProduto>DEMO-OPME-006</ans:codigoProduto>
            <ans:quantidadeSolicitada>1</ans:quantidadeSolicitada></ans:opme>
        </ans:opmeAutorizadas>
      </ans:autorizacao>
    </ans:mensagemTISS>"""
    res = put_guide(client, h, cid, tiss_xml=xml)
    assert res.status_code == 200, res.text
    assert res.json()["guia"]["origem"] == "tiss" and res.json()["guia"]["numero"] == "778899"
    assert {a["codigo"]: a["quantidade"] for a in res.json()["autorizado"]} == {"DEMO-OPME-001": 1, "DEMO-OPME-006": 1}
    # XML com DOCTYPE/ENTITY e XML sem item são recusados
    assert put_guide(client, h, cid, tiss_xml='<!DOCTYPE a [<!ENTITY x "y">]><a/>').status_code == 422
    assert put_guide(client, h, cid, tiss_xml="<a><b>1</b></a>").status_code == 422
    assert put_guide(client, h, cid, tiss_xml="<a>").status_code == 422


# ======================= 25: consumo por etiqueta =======================
def test_label_scan_three_way_expired_quantity_and_duplicates(client, auth):
    h = auth("medico")
    c = start(client, h)  # cen4b: o roteiro traz a guia (012, 013, 014 × 1 e 015 × 2)
    cid = c["id"]
    ok = scan(client, h, cid, FEMUR)
    assert ok.status_code == 200, ok.text
    leitura = ok.json()["leitura"]
    assert (leitura["codigo"], leitura["lote"], leitura["serie"], leitura["validade"]) == (
        "DEMO-OPME-012",
        "FEM123",
        "F001",
        "2030-12-31",
    )
    assert leitura["vencido"] is False and leitura["registro_anvisa"] == "DEMO-ANVISA-012"
    # série repetida (mesma peça lida duas vezes)
    assert scan(client, h, cid, FEMUR).status_code == 409
    # validade vencida: crítica, entra nos alertas, mesmo com a peça autorizada
    expired = scan(client, h, cid, "(01)02990000000132(17)240131(10)TIB9(21)T7")
    assert expired.status_code == 200 and expired.json()["leitura"]["vencido"] is True
    rep = report_of(expired.json()["insights"])
    assert line(rep, "DEMO-OPME-013")["status"] == "validade_vencida"
    critical = [p for p in rep["pendencias"] if p["severidade"] == "critico"]
    assert any(
        "Validade vencida" in p["texto"] and "31/01/2024" in p["texto"] and "TIB9" in p["texto"] for p in critical
    )
    assert any("Validade vencida" in a["texto"] for a in expired.json()["insights"]["alertas"])
    assert "opme_vencida" in rules(rep) and rules(rep)["opme_vencida"]["severidade"] == "critico"

    # cimento lido em 3 unidades × 2 autorizadas; etiqueta ilegível; quantidade com série
    qty = scan(client, h, cid, CEMENT, quantidade=3)
    assert line(report_of(qty.json()["insights"]), "DEMO-OPME-015")["status"] == "quantidade_divergente"
    assert any(
        "3 lido(s) na etiqueta × 2 autorizado(s)" in p["texto"] for p in report_of(qty.json()["insights"])["pendencias"]
    )
    assert scan(client, h, cid, "isto não é uma etiqueta").status_code == 422
    assert scan(client, h, cid, POLY, quantidade=2).status_code == 422
    # produto fora do catálogo e da guia: lido na etiqueta sem autorização
    other = scan(client, h, cid, LABEL)
    assert other.status_code == 200
    unknown = line(report_of(other.json()["insights"]), "GTIN-07891234567895")
    assert unknown["status"] == "nao_autorizado" and unknown["lotes"][0]["lote"] == "LOTE123"

    # remover leitura errada
    first_id = ok.json()["consumo"][0]["id"]
    gone = client.delete(f"/consultations/{cid}/opme-consumo/{first_id}", headers=h)
    assert gone.status_code == 200 and all(x["id"] != first_id for x in gone.json()["consumo"])
    assert client.delete(f"/consultations/{cid}/opme-consumo/zzz", headers=h).status_code == 404

    # três vias: a fala do roteiro confere com a guia e com as etiquetas lidas
    cid2 = start(client, h)["id"]
    for label in (FEMUR, TIBIA, POLY):
        assert scan(client, h, cid2, label).status_code == 200
    rep2 = report_of(dictate(client, h, cid2, demo_scripts()["cen4b_artroplastia"]["falas"])["insights"])
    for code in ("DEMO-OPME-012", "DEMO-OPME-013", "DEMO-OPME-014"):
        three = line(rep2, code)
        assert three["status"] == "conferido" and (
            three["status_fala"],
            three["status_guia"],
            three["status_etiqueta"],
        ) == (
            "ok",
            "ok",
            "ok",
        )
        assert three["consumido"] == three["citado"] == three["autorizado"] == 1 and three["lotes"]
    # implante narrado e autorizado, mas sem etiqueta lida (rastreio ligado): atenção
    cid3 = start(client, h)["id"]
    scan(client, h, cid3, FEMUR)
    rep3 = report_of(dictate(client, h, cid3, demo_scripts()["cen4b_artroplastia"]["falas"])["insights"])
    assert (
        line(rep3, "DEMO-OPME-013")["status"] == "sem_etiqueta"
        and line(rep3, "DEMO-OPME-013")["status_etiqueta"] == "ausente"
    )
    assert any("sem etiqueta lida" in p["texto"] for p in rep3["pendencias"])
    # etiqueta lida e não citada no ditado
    cid4 = start(client, h)["id"]
    scan(client, h, cid4, FEMUR)
    rep4 = report_of(
        dictate(client, h, cid4, [("medico", "Procedimento realizado: artroplastia total de joelho direito.")])[
            "insights"
        ]
    )
    assert line(rep4, "DEMO-OPME-012")["status"] == "sem_fala"
    # a leitura é auditada (com o lote) e a manual exige código e lote ou série
    assert any(a["codigo"] == "DEMO-OPME-013" and a["vencido"] is True for a in audit_rows("consulta.opme_consumo"))
    manual = client.post(
        f"/consultations/{cid4}/opme-consumo",
        json={"codigo": "DEMO-OPME-013", "lote": "M1", "validade": "2031-01-31"},
        headers=h,
    )
    assert manual.status_code == 200 and manual.json()["leitura"]["origem"] == "manual"
    assert (
        client.post(f"/consultations/{cid4}/opme-consumo", json={"codigo": "DEMO-OPME-013"}, headers=h).status_code
        == 422
    )


def test_size_is_extracted_from_the_dictation(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    put_guide(client, h, cid, itens=[{"codigo": "DEMO-OPME-016", "quantidade": 3}])
    rep = report_of(
        dictate(
            client,
            h,
            cid,
            [
                ("medico", "Fixação com dois parafusos corticais de 3,5 por 40, e um parafuso de 3,5 x 36."),
                ("medico", "Haste femoral tamanho 12 e placa bloqueada de 6 mm."),
            ],
        )["insights"]
    )
    screws = line(rep, "DEMO-OPME-016")
    assert screws["citado"] == 3 and screws["status"] == "conferido"
    assert screws["tamanhos"] == ["3,5 × 40 mm", "3,5 × 36 mm"]
    assert line(rep, "DEMO-OPME-019")["tamanhos"] == ["tamanho 12"]
    assert line(rep, "DEMO-OPME-017")["tamanhos"] == ["6 mm"]
    knee = report_of(
        dictate(client, h, start(client, h)["id"], demo_scripts()["cen4b_artroplastia"]["falas"])["insights"]
    )
    assert line(knee, "DEMO-OPME-012")["tamanhos"] == ["tamanho 5"]
    assert line(knee, "DEMO-OPME-014")["tamanhos"] == ["10 mm"]
    assert opme.extract_size(" 3,5 por 40, dois") == "3,5 × 40 mm" and opme.extract_size(" sem tamanho") is None


def test_adversarial_dictation_history_size_numbers_and_questions(client, auth):
    """Antecedente, número de tamanho e pergunta não viram material usado nem quantidade."""
    h = auth("medico")
    cid = start(client, h)["id"]
    rep = report_of(
        dictate(
            client,
            h,
            cid,
            [
                ("medico", "Antecedente de stent coronariano há 3 anos."),
                ("medico", "Paciente idosa com prótese prévia de joelho, componente femoral antigo bem fixo."),
                ("medico", "Vamos usar tela? O senhor quer que eu pegue o grampeador?"),
                ("medico", "nao usamos dreno tubular e sem tela"),
                ("medico", "Colocado componente femoral tamanho 5 e polietileno de 10 mm"),
                ("anestesista", "Colocados dois trocartes de 5 e mais um clipador"),
            ],
        )["insights"]
    )
    got = {o["codigo"]: o["citado"] for o in rep["opme"] if o["citado"]}
    assert got == {"DEMO-OPME-012": 1, "DEMO-OPME-014": 1, "DEMO-OPME-002": 2, "DEMO-OPME-003": 1}
    assert opme._quantity_before("colocados tres parafusos".split("parafusos")[0], 20) == 3
    assert opme._quantity_before("tamanho 5 ", 10) == 1 and opme._quantity_before("usado 2 ", 8) == 2


def test_malformed_guide_imports_are_422_not_500(client, auth):
    h = auth("medico")
    cid = start(client, h, pid="cen1", especialidade="cirurgia_geral")["id"]
    bad_claims = [
        {
            "resourceType": "ClaimResponse",
            "addItem": [{"productOrService": {"coding": [{"code": "X"}]}, "quantity": {"value": "abc"}}],
        },
        {"resourceType": "ClaimResponse", "addItem": ["x"]},
        {"resourceType": "ClaimResponse", "addItem": [{"productOrService": "x"}]},
        {"resourceType": "Bundle", "entry": [None]},
    ]
    for body in bad_claims:
        assert put_guide(client, h, cid, claim_response=body).status_code == 422, body
    inf = "<a><i><codigoProduto>X1</codigoProduto><descricao>d</descricao><quantidade>inf</quantidade></i></a>"
    assert put_guide(client, h, cid, tiss_xml=inf).status_code == 422
    # enfermagem registra etiqueta na sala; gestor não mexe no relatório
    assert put_guide(client, auth("gestor"), cid, itens=[{"codigo": "DEMO-OPME-001"}]).status_code == 403


def test_review_cannot_drop_a_line_with_a_label_read(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    assert scan(client, h, cid, FEMUR).status_code == 200
    dictate(client, h, cid, [(f["falante"], f["texto"]) for f in demo_scripts()["cen4b_artroplastia"]["falas"]])
    report = client.post(f"/consultations/{cid}/finish", headers=h).json()["nota"]["relatorio_cirurgico"]
    assert line(report, "DEMO-OPME-012")["lotes"]
    kept = [o for o in report["opme"] if o["codigo"] != "DEMO-OPME-012"]
    res = client.put(f"/consultations/{cid}/note", json={"relatorio_cirurgico": report | {"opme": kept}}, headers=h)
    after = res.json()["nota"]["relatorio_cirurgico"]
    assert line(after, "DEMO-OPME-012")["lotes"][0]["lote"] == "FEM123"  # a etiqueta lida não some na revisão


def test_negated_material_does_not_count(client, auth):
    h = auth("medico")
    cid = start(client, h, pid="cen1", especialidade="cirurgia_geral")["id"]
    rep = report_of(dictate(client, h, cid, [("medico", "Sem dreno tubular. Não foi usado grampeador.")])["insights"])
    assert rep["opme"] == []


# ======================= 25: edição na revisão =======================
def test_opme_is_editable_in_review_and_server_keeps_authority(client, auth):
    h = auth("medico")
    c = client.post(
        "/patients/cen4/consultations",
        json={"tipo": "cirurgia", "especialidade": "cirurgia_geral", "consentimento": CONSENT},
        headers=h,
    ).json()
    cid = c["id"]
    dictate(client, h, cid, [(f["falante"], f["texto"]) for f in demo_scripts()["cen4_cirurgia"]["falas"]])
    done = client.post(f"/consultations/{cid}/finish", headers=h).json()
    note = done["nota"]
    report = note["relatorio_cirurgico"]
    assert line(report, "DEMO-OPME-009")["status"] == "nao_autorizado"
    # o cirurgião corrige a quantidade de endoloop, justifica o bisturi ultrassônico e tenta forjar a guia
    edited = [dict(o) for o in report["opme"]]
    for o in edited:
        if o["codigo"] == "DEMO-OPME-005":
            o["citado"] = 3
        if o["codigo"] == "DEMO-OPME-009":
            o["justificativa"] = "Uso necessário por aderências; autorização complementar solicitada."
            o["autorizado"] = 5  # o servidor ignora: a guia é do servidor
            o["status"] = "conferido"
    edited = [o for o in edited if o["codigo"] != "DEMO-OPME-006"]  # retirada
    res = client.put(
        f"/consultations/{cid}/note",
        json={"relatorio_cirurgico": report | {"opme": edited}},
        headers=h,
    )
    assert res.status_code == 200, res.text
    after = res.json()["nota"]["relatorio_cirurgico"]
    assert "DEMO-OPME-006" not in {o["codigo"] for o in after["opme"]}
    endo = line(after, "DEMO-OPME-005")
    assert (endo["citado"], endo["autorizado"], endo["status"], endo["editado"]) == (
        3,
        2,
        "quantidade_divergente",
        True,
    )
    bisturi = line(after, "DEMO-OPME-009")
    assert bisturi["autorizado"] == 0 and bisturi["status"] == "nao_autorizado" and bisturi["justificativa"]
    texts = [p["texto"] for p in after["pendencias"]]
    assert any("3 citado(s) × 2 autorizado(s)" in t for t in texts)
    assert not any("Pinça de energia ultrassônica citado no ditado sem autorização" in t for t in texts)
    assert any("divergência justificada pelo cirurgião" in t for t in texts)
    changes = audit_rows("consulta.nota_revisada")[-1]["opme"]
    assert {c["codigo"]: c["acao"] for c in changes} == {
        "DEMO-OPME-005": "editado",
        "DEMO-OPME-009": "editado",
        "DEMO-OPME-006": "retirado",
    }
    # o contorno fica auditado: texto da justificativa por linha e id da regra anti-glosa que ela esconde
    bis = next(c for c in changes if c["codigo"] == "DEMO-OPME-009")
    assert bis["justificativa"] == "Uso necessário por aderências; autorização complementar solicitada."
    assert bis["regras"] == ["opme_sem_guia"]
    assert "justificativa" not in next(c for c in changes if c["codigo"] == "DEMO-OPME-005")
    # linha nova só com código do catálogo
    again = client.put(
        f"/consultations/{cid}/note",
        json={"relatorio_cirurgico": after | {"opme": [*after["opme"], new_line("DEMO-OPME-007"), new_line("XXX")]}},
        headers=h,
    ).json()["nota"]["relatorio_cirurgico"]
    assert line(again, "DEMO-OPME-007")["status"] == "nao_autorizado" and "XXX" not in {
        o["codigo"] for o in again["opme"]
    }


# ======================= 25: rastreio (Device/Procedure/Bundle, alta e plantão) =======================
def test_implant_traceability_bundle_discharge_card_and_handoff(client, auth, store, monkeypatch):
    h = auth("medico")
    c = start(client, h)
    cid = c["id"]
    for label in (FEMUR, TIBIA, POLY):
        assert scan(client, h, cid, label).status_code == 200
    dictate(client, h, cid, [(f["falante"], f["texto"]) for f in demo_scripts()["cen4b_artroplastia"]["falas"]])
    client.put(f"/consultations/{cid}/faturamento", json={"numero_guia": "AUT-55"}, headers=h)
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200, done.text

    # exportar (sem enviar): o Bundle de transação traz Device, Procedure.usedReference e o Claim, todos válidos
    export = client.get(f"/consultations/{cid}/export", headers=h).json()
    bundle = export["bundle"]
    Bundle.model_validate(bundle)
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert types.count("Device") == 3 and types.count("Procedure") == 1 and types.count("Claim") == 1
    devices = {e["fullUrl"]: e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Device"}
    procedure = next(e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Procedure")
    assert {r["reference"] for r in procedure["usedReference"]} == set(devices)
    femur = next(d for d in devices.values() if d["lotNumber"] == "FEM123")
    assert femur["serialNumber"] == "F001" and femur["expirationDate"] == "2030-12-31"
    assert femur["udiCarrier"][0]["deviceIdentifier"] == "02990000000125"
    assert femur["patient"] == {"reference": "Patient/cen4b"}
    assert "Implantes (rastreio)" in export["texto"] and "lote FEM123" in export["texto"]

    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    created: list[str] = []
    try:
        sent = client.post(f"/consultations/{cid}/send", headers=h)
        assert sent.status_code == 200, sent.text
        sent = sent.json()
        created = [sent["fhir_ref"], *sent["pedidos"], *sent["dispositivos"], sent["procedimento"], sent["claim"]]
        assert len(sent["dispositivos"]) == 3
        stored_proc = store.get(*sent["procedimento"].split("/"))
        assert [r["reference"] for r in stored_proc["usedReference"]] == sent["dispositivos"]
        assert stored_proc["report"] == [{"reference": sent["fhir_ref"]}]
        assert store.get(*sent["claim"].split("/"))["status"] == "draft"

        # Waze da alta: cartão do implante com o lote
        docs = client.post("/patients/cen4b/discharge-documents", json={"idioma": "pt-BR"}, headers=h)
        assert docs.status_code == 200, docs.text
        card = docs.json()["cartao_implante"]
        assert {i["lote"] for i in card} == {"FEM123", "TIB456", "POL789"}
        femur_card = next(i for i in card if i["lote"] == "FEM123")
        assert femur_card["serie"] == "F001" and femur_card["registro_anvisa"] == "DEMO-ANVISA-012"
        assert femur_card["refs"][0].startswith("Device/") and femur_card["procedimento"]
        # passagem de plantão: o dispositivo é fato
        handoff = client.post("/patients/cen4b/handoff", json={"audience": "enfermagem"}, headers=h).json()
        implants = [m for m in handoff["content"]["mudancas_no_periodo"] if m["texto"].startswith("Implante:")]
        assert len(implants) == 3 and any("lote FEM123" in m["texto"] for m in implants)
        assert all(m["refs"] and m["refs"][0].startswith("Device/") for m in implants)
    finally:
        cleanup(store, created)


# ======================= 26: regras em dados =======================
def test_glosa_rules_live_in_data_with_source():
    folder = get_settings().data_dir / "reference" / "glosa_rules"
    assert antiglosa.problems() == []
    assert set(antiglosa.convenios()) == {"generico", "DEMO-CONV", "SUS"}
    for conv in antiglosa.convenios().values():
        with open(folder / conv.arquivo, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert {"regra", "tipo", "condicao", "severidade", "fonte"} <= set(rows[0])
        assert rows and all(r["fonte"] and r["mensagem"] for r in rows)
    kinds = {r.tipo_fonte for c in antiglosa.convenios() for r in antiglosa.rules_for(c)}
    assert kinds == {"manual", "contrato", "motivo_glosa_tiss"}
    # códigos de procedimento são de demonstração (TUSS/SIGTAP não são licenciados aqui)
    assert all(
        p.tuss.startswith("DEMO-TUSS-") and p.sigtap.startswith("DEMO-SIGTAP-") for p in antiglosa.procedures().values()
    )
    # o convênio herda o genérico e sobrescreve pelo id
    generic = {r.regra: r for r in antiglosa.rules_for("generico")}
    demo = {r.regra: r for r in antiglosa.rules_for("DEMO-CONV")}
    assert set(generic) <= set(demo) and demo["auxiliares"].severidade == "critico" != generic["auxiliares"].severidade
    assert "profilaxia_ditada" in demo and "profilaxia_ditada" not in generic


def test_rules_are_not_hard_coded(client, auth, tmp_path, monkeypatch):
    """Muda o CSV (cópia) e o resultado muda: gravidade, mensagem e fonte vêm dos dados."""
    h = auth("medico")
    cid = start(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    script = demo_scripts()["cen4_cirurgia"]["falas"]
    before = report_of(dictate(client, h, cid, [(f["falante"], f["texto"]) for f in script])["insights"])
    assert rules(before)["equipe_ditada"]["severidade"] == "critico"

    ref = tmp_path / "reference"
    shutil.copytree(get_settings().data_dir / "reference", ref)
    path = ref / "glosa_rules" / "generico.csv"
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        if r["regra"] == "equipe_ditada":
            r.update(
                severidade="info", peso="1", mensagem="Equipe ainda não ditada (regra editada).", fonte="Fonte editada"
            )
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    monkeypatch.setattr("app.reference._ref_dir", lambda: ref)
    antiglosa.clear_cache()
    cid2 = start(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    after = report_of(dictate(client, h, cid2, [(f["falante"], f["texto"]) for f in script])["insights"])
    edited = rules(after)["equipe_ditada"]
    assert (edited["severidade"], edited["texto"], edited["fonte"]) == (
        "info",
        "Equipe ainda não ditada (regra editada).",
        "Fonte editada",
    )


# ======================= 26: score ao vivo =======================
def test_score_rises_as_fields_are_dictated_in_cen4(client, auth):
    h = auth("medico")
    cid = start(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    start_score = report_of(client.get(f"/consultations/{cid}", headers=h).json()["insights"])["prontidao"]["score"]
    assert start_score <= 10  # só o procedimento agendado (cen4) conta; nada ditado ainda
    scores = []
    for f in demo_scripts()["cen4_cirurgia"]["falas"]:
        scores.append(
            report_of(dictate(client, h, cid, [(f["falante"], f["texto"])])["insights"])["prontidao"]["score"]
        )
    assert scores[0] > 0 and scores[-1] > scores[0] + 20 and max(scores) >= scores[-1]
    assert scores[1] > scores[0] and scores[3] > scores[2]  # procedimento/anestesia e técnica entram no score
    base = scores[-1]

    # os campos que faltavam: horário, equipe e contagem. Cada um some das pendências e o score sobe até o teto
    # (com pendência crítica aberta, o bisturi sem guia, o score não passa de 89).
    first = base
    last = None
    for step, (text, rule, campo) in enumerate(
        (
            ("Início da cirurgia às 8h, término às 9h10.", "horario_ditado", "horario"),
            (
                "Equipe: cirurgião Dr. Paulo Mota, auxiliar Dr. Caio Lima, "
                "anestesista Dra. Rita Alves e instrumentador Pedro.",
                "equipe_ditada",
                "equipe",
            ),
            ("Contagem de compressas e instrumentais confere.", "completude_modelo", "contagem"),
        )
    ):
        last = report_of(dictate(client, h, cid, [("medico", text)])["insights"])
        now = last["prontidao"]
        assert (rule, campo) not in {(p["regra"], p["campo"]) for p in now["pendencias"]}
        assert now["score"] >= base and (step > 0 or now["score"] > base)
        base = now["score"]
    assert base > first and base <= 89
    pend = {p["regra"] for p in last["prontidao"]["pendencias"]}
    assert "equipe_ditada" not in pend and "horario_ditado" not in pend and "anestesista_identificado" not in pend
    assert last["prontidao"]["regras_ok"] < last["prontidao"]["regras_avaliadas"]  # o bisturi sem guia segue pendente
    assert last["prontidao"]["criticas"] == 1 and last["prontidao"]["pronto"] is False  # opme_sem_guia (crítica)
    put_guide(
        client,
        h,
        cid,
        itens=[
            {"codigo": "DEMO-OPME-001"},
            {"codigo": "DEMO-OPME-002", "quantidade": 2},
            {"codigo": "DEMO-OPME-005", "quantidade": 2},
            {"codigo": "DEMO-OPME-006"},
            {"codigo": "DEMO-OPME-009"},
        ],
    )
    ready = report_of(client.get(f"/consultations/{cid}", headers=h).json()["insights"])["prontidao"]
    assert ready["criticas"] == 0 and ready["pronto"] is True and ready["score"] >= 90


# ======================= 26: regras específicas =======================
def test_cid_incompatible_with_procedure_has_source(client, auth):
    h = auth("medico")
    # cen4b tem gonartrose (M17.1); apendicectomia ditada para essa paciente é incompatível com o CID
    cid = start(client, h)["id"]
    rep = report_of(
        dictate(
            client,
            h,
            cid,
            [
                (
                    "medico",
                    "Diagnóstico pré-operatório: gonartrose primária do joelho direito. "
                    "Procedimento realizado: apendicectomia.",
                )
            ],
        )["insights"]
    )
    assert rep["cids"][0]["codigo"] == "M17.1" and rep["modelo"]["id"] == "apendicectomia"
    item = rules(rep)["cid_procedimento"]
    assert item["severidade"] == "critico" and "M17.1" in item["texto"] and "Apendicectomia" in item["texto"]
    assert "K35" in item["texto"] and "SIGTAP" in item["fonte"] and "Motivo de glosa" in item["fonte"]
    # CID compatível: sem pendência
    good = report_of(
        dictate(
            client,
            h,
            start(client, h)["id"],
            [
                (
                    "medico",
                    "Diagnóstico pré-operatório: gonartrose primária do joelho direito. "
                    "Procedimento realizado: artroplastia total de joelho direito.",
                )
            ],
        )["insights"]
    )
    assert "cid_procedimento" not in rules(good)


def test_opme_procedure_team_time_and_anesthesia_rules(client, auth):
    h = auth("medico")
    cid = start(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    put_guide(client, h, cid, itens=[{"codigo": "DEMO-OPME-012"}])
    rep = report_of(
        dictate(
            client,
            h,
            cid,
            [
                ("medico", "Procedimento realizado: apendicectomia videolaparoscópica sob anestesia local."),
                ("medico", "Usado componente femoral. Início da cirurgia às 8h, término às 8h10."),
                (
                    "medico",
                    "Equipe: cirurgião Dr. A, auxiliar Dr. B, primeiro auxiliar Dra. C e "
                    "segundo auxiliar Dr. D, anestesista Dra. E.",
                ),
            ],
        )["insights"]
    )
    found = rules(rep)
    assert (
        "componente femoral" in found["opme_incompativel"]["texto"].lower()
        or "Componente femoral" in found["opme_incompativel"]["texto"]
    )
    assert found["opme_incompativel"]["severidade"] == "critico" and "Curadoria" in found["opme_incompativel"]["fonte"]
    assert "Duração de 10 min fora da faixa usual" in found["duracao_faixa"]["texto"]
    assert "Anestesia (anestesia local) não é usual em Apendicectomia" in found["anestesia_procedimento"]["texto"]
    assert "máximo de 2" in found["auxiliares"]["texto"]
    # coerência: início = término; duração dita diferente do calculado
    c2 = start(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    same = rules(
        report_of(
            dictate(
                client,
                h,
                c2,
                [("medico", "Procedimento realizado: apendicectomia. Início da cirurgia às 9h, término às 9h.")],
            )["insights"]
        )
    )
    assert "iguais" in same["horario_coerencia"]["texto"] and same["horario_coerencia"]["severidade"] == "critico"
    c3 = start(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    off = rules(
        report_of(
            dictate(
                client,
                h,
                c3,
                [
                    (
                        "medico",
                        "Procedimento realizado: apendicectomia. Início da cirurgia às 9h, "
                        "término às 10h. Duração de 3 horas.",
                    )
                ],
            )["insights"]
        )
    )
    assert "não bate com início e término" in off["horario_coerencia"]["texto"]
    c4 = start(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    half = rules(
        report_of(
            dictate(client, h, c4, [("medico", "Procedimento realizado: apendicectomia. Início da cirurgia às 9h.")])[
                "insights"
            ]
        )
    )
    assert "término não ditado" in half["horario_completo"]["texto"] and "horario_coerencia" not in half


def test_auxiliary_counting():
    count = antiglosa.count_auxiliares
    assert count("Equipe: cirurgião Dr. A, anestesista Dra. B") == 0
    assert count("Dois auxiliares e anestesista Dra. B") == 2
    assert count("Primeiro auxiliar Dr. C e segundo auxiliar Dra. D") == 2
    assert count("Auxiliares: Dr. X e Dra. Y; anestesista Dra. B") == 2
    assert count("cirurgião Dr. Henrique, auxiliar Dr. Caio Lima, anestesista Dra. Marina e instrumentador Felipe") == 1
    # negação: "sem auxiliar" não é um auxiliar identificado
    assert count("Equipe: cirurgião Dr. A, sem auxiliar, anestesista Dra. B") == 0
    assert count("Não houve auxiliar") == 0 and count("cirurgia sem auxiliares") == 0
    assert count("auxiliares: nenhum") == 0


def test_convenio_changes_rules_and_codes(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    dictate(client, h, cid, [(f["falante"], f["texto"]) for f in demo_scripts()["cen4b_artroplastia"]["falas"]])
    got = client.get("/consult/convenios", headers=h).json()
    assert {c["value"] for c in got} == {"generico", "DEMO-CONV", "SUS"}
    base = report_of(client.get(f"/consultations/{cid}", headers=h).json()["insights"])["prontidao"]
    assert base["convenio"] == "generico" and base["codigo_demo"] == "DEMO-TUSS-013"
    aux_base = next(p for p in base["pendencias"] if p["regra"] == "auxiliares")
    assert aux_base["severidade"] == "atencao"

    res = client.put(f"/consultations/{cid}/faturamento", json={"convenio": "DEMO-CONV"}, headers=h)
    assert res.status_code == 200, res.text
    demo = report_of(res.json()["insights"])["prontidao"]
    rules_demo = {p["regra"]: p for p in demo["pendencias"]}
    assert demo["convenio"] == "DEMO-CONV" and rules_demo["auxiliares"]["severidade"] == "critico"
    assert "Contrato DEMO-CONV" in rules_demo["auxiliares"]["fonte"] or "Curadoria" in rules_demo["auxiliares"]["fonte"]
    assert rules_demo["opme_rastreio"]["severidade"] == "critico" and demo["pronto"] is False and demo["score"] <= 89

    sus = report_of(
        client.put(f"/consultations/{cid}/faturamento", json={"convenio": "SUS"}, headers=h).json()["insights"]
    )
    assert sus["prontidao"]["codigo_demo"] == "DEMO-SIGTAP-013"
    assert client.put(f"/consultations/{cid}/faturamento", json={"convenio": "XYZ"}, headers=h).status_code == 422


# ======================= 26: trava de envio, Claim e audit =======================
def test_send_with_critical_pending_needs_justification_and_audit_records_it(client, auth, store, monkeypatch):
    h = auth("medico")
    cid = start(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    dictate(client, h, cid, [(f["falante"], f["texto"]) for f in demo_scripts()["cen4_cirurgia"]["falas"]])
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200, done.text
    ready = done.json()["nota"]["relatorio_cirurgico"]["prontidao"]
    assert ready["criticas"] >= 1 and not ready["pronto"]

    blocked = client.post(f"/consultations/{cid}/send", headers=h)
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["codigo"] == "prontidao_critica" and detail["pendencias"] and detail["pendencias"][0]["fonte"]
    short = client.post(f"/consultations/{cid}/send", json={"justificativa": "urgente"}, headers=h)
    assert short.status_code == 409

    text = "Cirurgia de urgência: autorização complementar do bisturi ultrassônico solicitada ao convênio."
    ok = client.post(f"/consultations/{cid}/send", json={"justificativa": text}, headers=h)
    assert ok.status_code == 200, ok.text
    assert ok.json()["bundle"]["resourceType"] == "Bundle"  # EHR_WRITEBACK desligado: exporta
    saved = client.get(f"/consultations/{cid}", headers=h).json()
    assert saved["justificativa_envio"]["texto"] == text and saved["justificativa_envio"]["regras_justificadas"]

    # audit 'cirurgia.prontidao': pendências antes e depois, no finish e no envio
    rows = [r for r in audit_rows("cirurgia.prontidao") if r["patient_id"] == "cen4"]
    finish, send = next(r for r in rows if r["momento"] == "finish"), next(r for r in rows if r["momento"] == "envio")
    for r in (finish, send):
        assert {"antes", "depois", "resolvidas", "convenio"} <= set(r)
        assert r["depois"]["pendencias"] and {"regra", "severidade"} <= set(r["depois"]["pendencias"][0])
    assert finish["antes"]["score"] <= finish["depois"]["score"] and finish["depois"]["criticas"] == ready["criticas"]
    assert send["justificativa"] == text and send["regras_justificadas"] and send["antes"] == finish["depois"]
    assert "texto" not in str(finish["depois"])  # só ids de regra, nunca texto clínico

    # na revisão o cirurgião pode resolver a pendência (guia complementar): o envio passa sem justificativa
    cid2 = start(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    put_guide(
        client,
        h,
        cid2,
        itens=[
            {"codigo": "DEMO-OPME-001"},
            {"codigo": "DEMO-OPME-002", "quantidade": 2},
            {"codigo": "DEMO-OPME-005", "quantidade": 2},
            {"codigo": "DEMO-OPME-006"},
            {"codigo": "DEMO-OPME-009"},
        ],
    )
    falas = [(f["falante"], f["texto"]) for f in demo_scripts()["cen4_cirurgia"]["falas"]]
    falas.append(
        (
            "medico",
            "Equipe: cirurgião Dr. A, auxiliar Dr. B, anestesista Dra. C. "
            "Início da cirurgia às 8h, término às 9h. Contagem confere.",
        )
    )
    dictate(client, h, cid2, falas)
    assert client.post(f"/consultations/{cid2}/finish", headers=h).status_code == 200
    clean = client.post(f"/consultations/{cid2}/send", headers=h)
    assert clean.status_code == 200, clean.text
    envio = [r for r in audit_rows("cirurgia.prontidao") if r["momento"] == "envio"][-1]
    assert "justificativa" not in envio and envio["depois"]["criticas"] == 0


def test_line_justification_bypass_is_audited_with_text_rule_and_send_gate(client, auth, store):
    h = auth("medico")
    cid = start(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    dictate(client, h, cid, [(f["falante"], f["texto"]) for f in demo_scripts()["cen4_cirurgia"]["falas"]])
    report = client.post(f"/consultations/{cid}/finish", headers=h).json()["nota"]["relatorio_cirurgico"]
    assert "opme_sem_guia" in {p["regra"] for p in report["prontidao"]["pendencias"] if p["severidade"] == "critico"}
    text = "Aderências extensas exigiram o bisturi ultrassônico; autorização complementar em andamento."
    edited = [dict(o, justificativa=text) if o["codigo"] == "DEMO-OPME-009" else dict(o) for o in report["opme"]]
    res = client.put(f"/consultations/{cid}/note", json={"relatorio_cirurgico": report | {"opme": edited}}, headers=h)
    assert res.status_code == 200, res.text
    change = next(c for c in audit_rows("consulta.nota_revisada")[-1]["opme"] if c["codigo"] == "DEMO-OPME-009")
    assert change["justificativa"] == text and change["regras"] == ["opme_sem_guia"]

    # o contorno é real: a pendência crítica "sem guia" some; o que sobrar ainda trava o envio
    sent = client.post(f"/consultations/{cid}/send", headers=h)
    if sent.status_code == 409:
        assert "opme_sem_guia" not in {p["regra"] for p in sent.json()["detail"]["pendencias"]}
        sent = client.post(f"/consultations/{cid}/send", json={"justificativa": text}, headers=h)
    assert sent.status_code == 200, sent.text
    envio = [r for r in audit_rows("cirurgia.prontidao") if r["momento"] == "envio"][-1]
    assert {"codigo": "DEMO-OPME-009", "justificativa": text} in envio["opme_justificadas"]

    # retirar a justificativa também fica registrado, e a trava "sem guia" volta
    cleared = [dict(o, justificativa=None) if o["codigo"] == "DEMO-OPME-009" else dict(o) for o in edited]
    res = client.put(f"/consultations/{cid}/note", json={"relatorio_cirurgico": report | {"opme": cleared}}, headers=h)
    assert res.status_code == 200, res.text
    change = next(c for c in audit_rows("consulta.nota_revisada")[-1]["opme"] if c["codigo"] == "DEMO-OPME-009")
    assert change["justificativa"] is None and change["justificativa_anterior"] == text and "regras" not in change
    again = client.post(f"/consultations/{cid}/send", headers=h)
    assert again.status_code == 409
    assert "opme_sem_guia" in {p["regra"] for p in again.json()["detail"]["pendencias"]}


def test_claim_draft_validates_as_r4(client, auth, store, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    for label in (FEMUR, TIBIA, POLY):
        scan(client, h, cid, label)
    dictate(client, h, cid, [(f["falante"], f["texto"]) for f in demo_scripts()["cen4b_artroplastia"]["falas"]])
    client.put(f"/consultations/{cid}/faturamento", json={"numero_guia": "AUT-77"}, headers=h)
    client.post(f"/consultations/{cid}/finish", headers=h)
    export = client.get(f"/consultations/{cid}/export", headers=h).json()
    claim = next(e["resource"] for e in export["bundle"]["entry"] if e["resource"]["resourceType"] == "Claim")
    model = Claim.model_validate(claim)
    assert model.status == "draft" and model.use == "claim" and model.insurance[0].preAuthRef == ["AUT-77"]
    assert claim["diagnosis"][0]["diagnosisCodeableConcept"]["coding"][0]["code"] == "M17.1"
    assert claim["procedure"][0]["procedureCodeableConcept"]["coding"][0]["code"] == "DEMO-TUSS-013"
    items = {i["productOrService"]["coding"][0]["code"]: i for i in claim["item"]}
    assert items["DEMO-TUSS-013"]["procedureSequence"] == [1] and items["DEMO-OPME-012"]["quantity"]["value"] == 1
    assert items["DEMO-OPME-012"]["udi"][0]["reference"].startswith("urn:uuid:")  # aponta para o Device do Bundle
    Bundle.model_validate(export["bundle"])
    # sem guia: pré-autorização; SUS: código SIGTAP (demonstração)
    cid2 = start(client, h)["id"]
    dictate(client, h, cid2, [(f["falante"], f["texto"]) for f in demo_scripts()["cen4b_artroplastia"]["falas"]])
    client.put(f"/consultations/{cid2}/faturamento", json={"convenio": "SUS"}, headers=h)
    client.post(f"/consultations/{cid2}/finish", headers=h)
    sus = next(
        e["resource"]
        for e in client.get(f"/consultations/{cid2}/export", headers=h).json()["bundle"]["entry"]
        if e["resource"]["resourceType"] == "Claim"
    )
    Claim.model_validate(sus)
    assert (
        sus["use"] == "preauthorization"
        and sus["procedure"][0]["procedureCodeableConcept"]["coding"][0]["code"] == "DEMO-SIGTAP-013"
    )
    assert sus["procedure"][0]["procedureCodeableConcept"]["coding"][0]["system"].endswith("sigtap-demo")


def test_claim_is_created_on_send_and_not_for_clinic_notes(client, auth, store, monkeypatch):
    h = auth("medico")
    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    cid = start(client, h)["id"]
    dictate(client, h, cid, [(f["falante"], f["texto"]) for f in demo_scripts()["cen4b_artroplastia"]["falas"]])
    client.post(f"/consultations/{cid}/finish", headers=h)
    created: list[str] = []
    try:
        sent = client.post(f"/consultations/{cid}/send", headers=h).json()
        created = [sent["fhir_ref"], *sent["pedidos"], sent["claim"]]
        assert "dispositivos" not in sent and "procedimento" not in sent  # nenhuma etiqueta lida: sem Device
        Claim.model_validate(store.get(*sent["claim"].split("/")))
    finally:
        cleanup(store, created)
    consulta = client.post(
        "/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=h
    ).json()
    from tests.test_consult import SCRIPT, say

    say(client, h, consulta["id"], SCRIPT)
    client.post(f"/consultations/{consulta['id']}/finish", headers=h)
    exp = client.get(f"/consultations/{consulta['id']}/export", headers=h).json()
    assert "bundle" not in exp
