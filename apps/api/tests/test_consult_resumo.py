"""#8 — Sumarizador com fonte por frase, lembretes ao vivo e alertas (atenção recolhida + sinal vital crítico)."""

import pytest
from sqlalchemy import select

from app import clock
from app.consult import summary
from app.consult.engine import Engine
from app.consult.schemas import Insights, Segment
from app.db import session as db_session
from app.db.models import MedrecRun, Organization
from app.llm import client as llm_client
from app.timeline.context import build_context
from app.timeline.events import ClinicalEvent, build_timeline
from tests.test_consult import SCRIPT, FakeClient, say, start


def engine_for(record, falas, pid="cen6", extra=None, events=None) -> Engine:
    rec = record(pid)
    segs = [Segment(n=i, t=float(i), falante=who, texto=t) for i, (who, t) in enumerate(falas, 1)]  # type: ignore[arg-type]
    return Engine(
        rec,
        build_timeline(rec) + (events or []),
        build_context(rec),
        segs,
        "clinica_medica",
        "consulta",
        clock.now(),
        extra,
    )


def vital(pid, key, value, ref="Observation/x-vital", minutes_ago=20, **more):
    from datetime import timedelta

    return ClinicalEvent(
        id=ref,
        patient_id=pid,
        timestamp=clock.now() - timedelta(minutes=minutes_ago),
        kind="vital",
        summary=f"{key} {value}",
        value={"codigo": "x", "chave": key, "valor": value, "unidade": ""} | more,
        source_ref=ref,
    )


def kinds(ins: Insights, tipo: str):
    return [r for r in ins.lembretes if r.tipo == tipo]


# ---------- resumo com fonte por frase ----------
def test_cen6_mock_summary_has_the_fasting_sentence_and_the_recent_diuretic(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    frases = ins["resumo"]["frases"]
    fasting = next(f for f in frases if "glibenclamida" in f["texto"].lower() and "jejum" in f["texto"].lower())
    assert {"Fala/9", "Fala/11"} <= set(fasting["refs"])
    assert "64 mg/dL" in fasting["texto"]  # a glicemia da triagem, com a Observation
    assert any(r.startswith("Observation/") for r in fasting["refs"])
    diuretic = next(f for f in frases if "hidroclorotiazida" in f["texto"].lower())
    assert "Fala/26" in diuretic["refs"] and any(r.startswith("MedicationRequest/") for r in diuretic["refs"])
    assert "Mudança recente" in diuretic["texto"]
    by_key = {f["chave"] for f in frases}
    assert {"comorbidades", "plano"} <= by_key and any(k.startswith("emocao:") for k in by_key)
    # o resumo antigo continua disponível como texto único
    assert (
        ins["resumo"]["texto"].startswith("[PACIENTE], 60 anos") and "glibenclamida" in ins["resumo"]["texto"].lower()
    )


def test_every_summary_sentence_has_a_valid_source(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    chart = client.get(f"/consultations/{cid}", headers=h).json()
    assert len(chart["segmentos"]) == len(SCRIPT)
    for f in ins["resumo"]["frases"]:
        assert f["refs"], f["texto"]
        for ref in f["refs"]:
            if ref.startswith("Fala/"):
                assert 1 <= int(ref.split("/")[1]) <= len(SCRIPT)
    # pelo menos o plano dito pela médica cita as falas dela
    plan = next(f for f in ins["resumo"]["frases"] if f["chave"] == "plano")
    assert set(plan["refs"]) <= {f"Fala/{n}" for n, s in enumerate(SCRIPT, 1) if s["falante"] == "medico"}


def test_new_sentences_are_marked_against_the_previous_summary(client, auth):
    h = auth("medico")
    created = start(client, h)
    assert not any(f["nova"] for f in created["insights"]["resumo"]["frases"])  # linha de base: nada é novo
    cid = created["id"]
    first = say(client, h, cid, SCRIPT[:4])
    new_first = {f["chave"] for f in first["resumo"]["frases"] if f["nova"]}
    assert any(k.startswith("queixa:") for k in new_first) and "comorbidades" not in new_first
    second = say(client, h, cid, SCRIPT[4:12])
    flags = {f["chave"]: f["nova"] for f in second["resumo"]["frases"]}
    assert flags["comorbidades"] is False
    assert any(f["nova"] for f in second["resumo"]["frases"] if f["chave"].startswith(("jejum:", "queixa:")))
    # a mesma análise de novo (sem falas novas) não apaga o que era novo
    again = client.post(f"/consultations/{cid}/insights", headers=h).json()["insights"]
    assert {f["chave"]: f["nova"] for f in again["resumo"]["frases"]} == flags


def test_ai_summary_sentences_only_enter_with_valid_refs(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    anamnese = {
        "queixa_principal": "Tontura",
        "hda": "x",
        "antecedentes": [],
        "medicacoes_em_uso": [],
        "alergias": [],
        "habitos_e_contexto": [],
        "revisao_de_sistemas": [],
        "refs": ["Fala/4"],
    }
    payload = {
        "resumo": "Tontura postural com hipoglicemia.",
        "resumo_frases": [
            {"texto": "A filha acompanha e vai ajudar com os horários dos remédios.", "refs": ["Fala/29"]},
            {"texto": "Frase inventada sem fonte válida.", "refs": ["Fala/999"]},
            {"texto": "Frase sem fonte nenhuma.", "refs": []},
        ],
        "destaques": [],
        "queixas": [],
        "emocoes": [],
        "perguntas": [],
        "hipoteses": [],
        "sugestoes_medicamentos": [],
        "cirurgia": [],
        "historico_relatado": [],
        "anamnese": anamnese,
        "falantes": [],
    }
    monkeypatch.setattr(llm_client.get_settings(), "llm_mode", "real")
    monkeypatch.setattr(llm_client, "_client", FakeClient(payload))
    res = client.post(f"/consultations/{cid}/insights", headers=h)
    assert res.status_code == 200, res.text
    frases = res.json()["insights"]["resumo"]["frases"]
    ia = [f for f in frases if f["origem"] == "ia"]
    assert [f["refs"] for f in ia] == [["Fala/29"]]
    assert not any("inventada" in f["texto"] or "sem fonte nenhuma" in f["texto"] for f in frases)
    assert any(d["onde"] == "resumo" for d in res.json()["ia"]["descartados"])
    assert any(f["origem"] == "regra" for f in frases)  # as frases das regras ficam
    monkeypatch.setattr(llm_client, "_client", None)


def test_summary_sentences_for_free_form_speech(record):
    e = engine_for(
        record,
        [
            ("paciente", "Eu tomo a glibenclamida em jejum, sem comer nada."),
            ("paciente", "A losartana eu tomo de manhã e a noite."),
            ("paciente", "Faz duas semanas que mandaram trocar o remédio da pressão, comecei um novo."),
            ("medico", "Vou pedir exames e vamos ajustar a dose."),
            ("medico", "A senhora tem alergia?"),
        ],
    )
    frases = {f.chave: f for f in e.run().resumo.frases}
    assert "jejum:glibenclamida" in frases and frases["jejum:glibenclamida"].refs[0] == "Fala/1"
    assert "uso:losartana" in frases and "Fala/2" in frases["uso:losartana"].refs
    assert frases["plano"].refs == ["Fala/4"]  # a pergunta da fala 5 não é plano
    assert all(f.refs for f in frases.values())


def test_summary_without_conditions_still_cites_the_patient(record):
    ins = engine_for(record, [], pid="cen1").run()
    assert ins.resumo.frases[0].refs and ins.resumo.frases[0].chave == "comorbidades"
    assert summary.text_of(ins.resumo.frases).startswith("[PACIENTE]")


# ---------- lembretes ----------
def test_allergy_reminder_appears_without_record_and_goes_away_after_the_answer(client, auth):
    h = auth("medico")
    created = start(client, h, pid="cen7e")  # nenhuma AllergyIntolerance (nem "nega alergias conhecidas")
    assert [r["tipo"] for r in created["insights"]["lembretes"] if r["tipo"] == "alergia"] == ["alergia"]
    cid = created["id"]
    still = say(client, h, cid, [{"falante": "paciente", "texto": "Estou com dor de cabeça.", "t": 1}])
    assert any(r["tipo"] == "alergia" for r in still["lembretes"])
    after = say(
        client,
        h,
        cid,
        [
            {"falante": "medico", "texto": "A senhora tem alergia a algum remédio?", "t": 2},
            {"falante": "paciente", "texto": "Não tenho alergia, doutora.", "t": 3},
        ],
    )
    assert not any(r["tipo"] == "alergia" for r in after["lembretes"])


def test_allergy_reminder_not_shown_when_the_chart_records_allergies(client, auth):
    h = auth("medico")
    created = start(client, h)  # cen6 tem alergia à penicilina registrada
    assert not any(r["tipo"] == "alergia" for r in created["insights"]["lembretes"])
    # "nega alergias conhecidas" registrado também é um registro (cen1)
    assert not any(r["tipo"] == "alergia" for r in start(client, h, pid="cen1")["insights"]["lembretes"])


def test_overdue_protocol_reminder_goes_away_when_the_topic_comes_up(client, auth):
    h = auth("medico")
    created = start(client, h, pid="cen7e")  # HbA1c de 250 dias, intervalo de 180
    hba1c = [r for r in created["insights"]["lembretes"] if r["tipo"] == "protocolo" and "HbA1c" in r["texto"]]
    assert hba1c and "vencido" in hba1c[0]["texto"] and hba1c[0]["severidade"] == "atencao"
    assert hba1c[0]["refs"] and hba1c[0]["fonte"]
    after = say(
        client, h, created["id"], [{"falante": "medico", "texto": "Vamos olhar a hemoglobina glicada.", "t": 1}]
    )
    assert not any("HbA1c" in r["texto"] for r in after["lembretes"])
    assert any(r["tipo"] == "protocolo" for r in after["lembretes"])  # os outros continuam


def test_vital_sign_not_taken_at_triage_reminder(client, auth):
    h = auth("medico")
    created = start(client, h)  # a triagem do cen6 não mediu SpO2 nem temperatura
    reminder = next(r for r in created["insights"]["lembretes"] if r["tipo"] == "sinais_vitais")
    assert "saturação" in reminder["texto"] and "temperatura" in reminder["texto"] and reminder["refs"]
    ins = say(client, h, created["id"], [{"falante": "medico", "texto": "A saturação está em 97.", "t": 1}])
    texto = next(r for r in ins["lembretes"] if r["tipo"] == "sinais_vitais")["texto"]
    assert "saturação" not in texto and "temperatura" in texto
    done = say(client, h, created["id"], [{"falante": "medico", "texto": "Medi a temperatura: 36,5.", "t": 2}])
    assert not any(r["tipo"] == "sinais_vitais" for r in done["lembretes"])


def test_post_discharge_visit_without_reconciliation(client, auth, record):
    h = auth("medico")
    created = start(client, h, pid="cen7d")  # alta há 6 dias, losartana no lugar do enalapril
    reminder = next(r for r in created["insights"]["lembretes"] if r["tipo"] == "pos_alta")
    assert "sem reconciliação" in reminder["texto"] and reminder["refs"][0].startswith("Encounter/")
    # com uma reconciliação feita depois da internação o lembrete some
    with db_session.SessionLocal() as db:
        db.add(
            MedrecRun(
                patient_id="cen7d",
                from_encounter_or_location="Enfermaria",
                to_encounter_or_location="Casa",
                findings_json={},
                created_at=clock.now(),
                organization_id=db.scalar(select(Organization.id)),  # a reconciliação é da organização do usuário
            )
        )
        db.commit()
    again = say(client, h, created["id"], [{"falante": "paciente", "texto": "Estou melhor.", "t": 1}])
    assert not any(r["tipo"] == "pos_alta" for r in again["lembretes"])


def test_reminder_not_shown_when_the_patient_was_not_discharged_recently(record):
    assert not kinds(engine_for(record, []).run(), "pos_alta")


# ---------- alertas ----------
def test_alerts_card_lists_critical_first_then_attention(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    order = [a["severidade"] for a in ins["alertas"]]
    assert order == sorted(order, key=lambda s: {"critico": 0, "atencao": 1, "info": 2}[s])
    assert "critico" in order and "atencao" in order
    metformin = next(a for a in ins["alertas"] if a["texto"].startswith("Metformina"))
    assert metformin["severidade"] == "atencao"  # TFG 30–44 (cen6 ≈ 40)
    assert any(r.startswith("Observation/") for r in metformin["refs"])


def test_critical_triage_blood_pressure_makes_a_critical_alert(client, auth):
    h = auth("medico")
    created = start(client, h, pid="cen1")  # triagem: PA 184/102 há 25 min
    pa = [
        a
        for a in created["insights"]["alertas"]
        if a["categoria"] == "sinal_vital" and "Pressão arterial" in a["texto"]
    ]
    assert pa and pa[0]["severidade"] == "critico" and "184/102" in pa[0]["texto"]
    assert pa[0]["refs"] and pa[0]["refs"][0].startswith("Observation/")
    assert any(d["texto"] == pa[0]["texto"] for d in created["insights"]["resumo"]["destaques"])


@pytest.mark.parametrize(
    "key, value, extra, expected",
    [
        ("spo2", 91, {}, True),
        ("spo2", 92, {}, False),
        ("fc", 121, {}, True),
        ("fc", 120, {}, False),
        ("fc", 49, {}, True),
        ("fc", 50, {}, False),
        ("glicemia", 69, {}, True),
        ("glicemia", 70, {}, False),
        ("glicemia", 301, {}, True),
        ("glicemia", 300, {}, False),
        ("pa", 0, {"sistolica": 180, "diastolica": 90}, True),
        ("pa", 0, {"sistolica": 150, "diastolica": 110}, True),
        ("pa", 0, {"sistolica": 179, "diastolica": 109}, False),
    ],
)
def test_critical_vital_thresholds(record, key, value, extra, expected):
    ev = vital("cen2", key, value, ref=f"Observation/t-{key}", **extra)
    base = engine_for(record, [], pid="cen2").run()
    got = engine_for(record, [], pid="cen2", events=[ev]).run()
    new = [a for a in got.alertas if a.categoria == "sinal_vital" and a not in base.alertas]
    assert bool(new) is expected, [a.texto for a in new]
    if expected:
        assert new[0].severidade == "critico" and new[0].refs == [f"Observation/t-{key}"]


def test_old_vitals_do_not_make_triage_alerts(record):
    ev = vital("cen2", "spo2", 85, ref="Observation/old", minutes_ago=60 * 30)
    assert not [a for a in engine_for(record, [], pid="cen2", events=[ev]).run().alertas if "SpO2" in a.texto]


@pytest.mark.parametrize(
    "who, text, key",
    [
        ("paciente", "Tomei a metformina depois do almoço e vim direto pra cá.", "jejum:metformina"),
        ("medico", "A senhora tomou a glibenclamida em jejum?", "jejum:glibenclamida"),
        ("paciente", "Não tomei café hoje.", "jejum:glibenclamida"),
        ("paciente", "Tomo a losartana todo dia de manhã.", "jejum:losartana"),
    ],
)
def test_fasting_sentence_needs_a_patient_cue_with_a_medicine(record, who, text, key):
    frases = {f.chave for f in engine_for(record, [(who, text)]).run().resumo.frases}
    assert key not in frases


def test_fasting_sentence_for_the_generic_diabetes_pill(record):
    e = engine_for(record, [("paciente", "Tomo o remédio do açúcar de manhã cedo, antes de comer nada.")])
    assert "jejum:glibenclamida" in {f.chave for f in e.run().resumo.frases}  # a sulfonilureia, que causa hipoglicemia
