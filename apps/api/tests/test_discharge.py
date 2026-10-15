import pytest
from sqlalchemy import select

from app.db import session as db_session
from app.db.models import AuditLog, DischargeOverride
from app.discharge.scoring import SocialChecklist, _l_points, final_risk, lace, prefill, social_score

RISKY = dict(
    mora_sozinho=True,
    tem_cuidador=False,
    escadas_em_casa=True,
    mobilidade_reduzida=True,
    acesso_medicamentos=False,
    precisa_dispositivos=True,
    dispositivos=["Glicosímetro e insumos"],
    ja_tem_dispositivos=False,
    consulta_retorno_agendada=False,
    teach_back_realizado=False,
)
MITIGATED = dict(
    mora_sozinho=True,
    tem_cuidador=True,
    cuidador_capacitado=True,
    escadas_em_casa=False,
    mobilidade_reduzida=True,
    acesso_medicamentos=True,
    precisa_dispositivos=True,
    dispositivos=["Glicosímetro e insumos"],
    ja_tem_dispositivos=True,
    consulta_retorno_agendada=True,
    teach_back_realizado=True,
)
GOOD = dict(
    mora_sozinho=False,
    tem_cuidador=True,
    cuidador_capacitado=True,
    escadas_em_casa=False,
    mobilidade_reduzida=False,
    acesso_medicamentos=True,
    precisa_dispositivos=False,
    consulta_retorno_agendada=True,
    teach_back_realizado=True,
)


# ---------- LACE ----------
@pytest.mark.parametrize(("days", "points"), [(0, 0), (1, 1), (3, 3), (4, 4), (6, 4), (7, 5), (13, 5), (14, 7)])
def test_lace_length_of_stay_points(days, points):
    assert _l_points(days) == points


def test_lace_scenario3_high(record):
    score, comp = lace(record("cen3"))
    assert comp["L"]["pontos"] == 4  # 6 dias
    assert comp["A"]["pontos"] == 3  # urgência
    assert comp["C"]["pontos"] == 5  # Charlson 6 (ICC 1 + DM com lesão 2 + DRC 2 + DPOC 1)
    assert comp["E"]["pontos"] == 3  # 3 idas ao PS em 6 meses
    assert score == 15
    assert all(comp[k]["refs"] for k in "LACE")


def test_lace_scenario4_low(record):
    score, comp = lace(record("cen4"))
    assert (comp["L"]["pontos"], comp["A"]["pontos"], comp["C"]["pontos"], comp["E"]["pontos"]) == (1, 3, 0, 0)
    assert score == 4


def test_charlson_takes_highest_weight_in_group(record):
    _, comp = lace(record("cen3"))
    diabetes = [c for c in comp["C"]["categorias"] if "Diabetes" in c["categoria"]]
    assert len(diabetes) == 1 and diabetes[0]["peso"] == 2


# ---------- score social e combinação ----------
def test_social_score_weights_and_conditionals():
    score, items = social_score(SocialChecklist(**RISKY))
    fields = {i["campo"] for i in items}
    assert {"mora_sozinho", "tem_cuidador", "ja_tem_dispositivos", "teach_back_realizado"} <= fields
    assert "cuidador_capacitado" not in fields  # só conta se houver cuidador
    assert score == 16
    assert social_score(SocialChecklist(**GOOD)) == (0, [])
    mitigated, items = social_score(SocialChecklist(**MITIGATED))
    assert mitigated == 2 and [i["campo"] for i in items] == ["mobilidade_reduzida"]


@pytest.mark.parametrize(
    ("lace_score", "social", "risk"),
    [
        (15, 13, "alto"),
        (15, 4, "alto"),
        (15, 2, "moderado"),
        (7, 0, "moderado"),
        (2, 4, "moderado"),
        (2, 7, "alto"),
        (4, 0, "baixo"),
    ],
)
def test_final_risk_combination(lace_score, social, risk):
    assert final_risk(lace_score, social)[0] == risk


def test_prefill_from_fhir(record):
    pre = prefill(record("cen3"))
    assert pre["valores"] == {"mora_sozinho": True, "tem_cuidador": False, "mobilidade_reduzida": True}
    assert pre["fontes"]["mora_sozinho"]["ref"] == "Observation/cen3-soc-moradores"
    assert prefill(record("cen4"))["valores"]["tem_cuidador"] is True


# ---------- golden: cenários 3 e 4 ----------
def test_scenario3_high_risk_locks_until_override(client, auth):
    h = auth("medico")
    a = client.post("/patients/cen3/discharge-assessment", json={"social_checklist": RISKY}, headers=h).json()
    assert a["final_risk"] == "alto" and a["trava"]["ativa"] is True
    assert a["sugestoes"] and all(s["refs"] for s in a["sugestoes"])
    assert any("insulina" in s["texto"].lower() for s in a["sugestoes"])

    blocked = client.post(f"/discharge-assessments/{a['id']}/confirm", headers=h)
    assert blocked.status_code == 409

    short = client.post(f"/discharge-assessments/{a['id']}/override", json={"justification": "curta"}, headers=h)
    assert short.status_code == 422
    nurse = client.post(
        f"/discharge-assessments/{a['id']}/override", json={"justification": "x" * 30}, headers=auth("enfermagem")
    )
    assert nurse.status_code == 403

    text = "Filha virá morar com o paciente por 2 semanas; visita do Melhor em Casa agendada para amanhã."
    ok = client.post(f"/discharge-assessments/{a['id']}/override", json={"justification": text}, headers=h).json()
    assert ok["trava"]["ativa"] is False and ok["overrides"][0]["justificativa"] == text
    confirmed = client.post(f"/discharge-assessments/{a['id']}/confirm", headers=h).json()
    assert confirmed["confirmada"]["por"] == "Dra. Helena Prado"

    with db_session.SessionLocal() as db:
        assert (
            db.scalar(select(DischargeOverride).where(DischargeOverride.assessment_id == a["id"])).justification == text
        )
        actions = [x.action for x in db.scalars(select(AuditLog))]
        assert {
            "discharge.avaliacao",
            "discharge.confirmacao_bloqueada",
            "discharge.override",
            "discharge.confirmada",
        } <= set(actions)

    # o override aparece na auditoria do gestor
    audit = client.get("/audit?action=discharge.override", headers=auth("gestor")).json()
    assert any(e["payload"]["justificativa"] == text for e in audit["items"])


def test_scenario3_reassessment_after_suggestions_unlocks(client, auth):
    h = auth("medico")
    client.post("/patients/cen3/discharge-assessment", json={"social_checklist": RISKY}, headers=h)
    again = client.post("/patients/cen3/discharge-assessment", json={"social_checklist": MITIGATED}, headers=h).json()
    assert again["final_risk"] == "moderado" and again["trava"]["ativa"] is False
    assert client.post(f"/discharge-assessments/{again['id']}/confirm", headers=h).status_code == 200


def test_scenario4_low_risk_no_lock(client, auth):
    h = auth("medico")
    a = client.post("/patients/cen4/discharge-assessment", json={"social_checklist": GOOD}, headers=h).json()
    assert a["final_risk"] == "baixo" and a["trava"]["ativa"] is False
    assert client.post(f"/discharge-assessments/{a['id']}/confirm", headers=h).status_code == 200
    panel = client.get("/patients", headers=h).json()
    assert next(r for r in panel if r["id"] == "cen4")["risco_alta"] == "baixo"
