"""Verificação cética de #5 e #6: entradas adversariais em PT-BR (fala corrida, erro de digitação, outro falante) e o
caminho "confiável" do CID, que não pode servir para a IA passar código inventado."""

import pytest

from app.consult import ai as consult_ai
from app.consult.chart import ChartView
from app.consult.cid import screen
from app.consult.schemas import CidSuggestion
from app.consult.text import planned_drugs
from app.llm.pseudonymize import Pseudonymizer
from app.reference import icd_entry
from tests.test_consult_cid_medicamentos import SCRIPT, _fake_structured, cen6_engine


def cids(*codes: str, tipo: str = "hipotese") -> list[CidSuggestion]:
    return [CidSuggestion(codigo=c, descricao="x", tipo=tipo, refs=["Fala/1"]) for c in codes]


# ---------- CID: o caminho "confiável" ----------
def test_trusted_path_only_lets_through_codes_the_professional_registered_in_the_chart():
    kept, dropped = screen(cids("N18.3", "n183", "J99.9", "K35.5"), "cids", trusted=frozenset({"N18.3"}))
    assert [c.codigo for c in kept] == ["N18.3", "N18.3"]  # normalizado, e só o que consta no prontuário
    assert {d["cid"] for d in dropped} == {"J99.9", "K35.5"}


def test_malformed_chart_code_does_not_open_the_trusted_path_to_garbage(record, monkeypatch):
    """Condition com código malformado ("E11.-") normalizava para "" e "" entrava no conjunto confiável: qualquer lixo
    da IA que também normalizasse para "" passava."""
    e = cen6_engine(record, [("paciente", "Doutora, estou com tontura.")])
    monkeypatch.setattr(
        ChartView,
        "conditions",
        property(lambda self: [("E11.-", "Diabetes", {"id": "a"}), ("I10", "HAS", {"id": "b"})]),
    )
    assert e.chart.icd_codes == frozenset({"I10"})
    kept, dropped = screen(cids("XYZ", "Z", "J06.9x"), "cids", trusted=e.chart.icd_codes)
    assert kept == [] and len(dropped) == 3
    assert {d["motivo"] for d in dropped} == {"CID inexistente"}
    # e a trava de ponta a ponta: a nota da IA com lixo não chega ao prontuário
    ins = e.run()
    note = consult_ai.mock_note(e, ins, "pt-BR")
    note["cids"] = [
        {"codigo": "XYZ", "descricao": "lixo", "tipo": "principal", "refs": ["Fala/1"]},
        {"codigo": "I10", "descricao": "HAS", "tipo": "secundario", "refs": ["Fala/1"]},
    ]
    monkeypatch.setattr(consult_ai, "generate_structured", _fake_structured(note))
    result, _, dropped = consult_ai.run_note(e, ins, Pseudonymizer(), {"Fala/1"}, "pt-BR")
    assert [c.codigo for c in result.cids] == ["I10"] and any(d.get("cid") == "XYZ" for d in dropped)


def test_ai_cannot_invent_the_seed_codes_it_is_not_registered_for(record):
    """N18.3 só passa quando o profissional o registrou (o cen6 registrou); K35.5 não existe em tabela alguma."""
    e = cen6_engine(record, [("paciente", "Doutora, estou com tontura.")])
    assert "N18.3" in e.chart.icd_codes and "K35.5" not in e.chart.icd_codes
    kept, dropped = screen(cids("N18.3", "K35.5"), "cids", trusted=e.chart.icd_codes)
    assert [c.codigo for c in kept] == ["N18.3"] and [d["cid"] for d in dropped] == ["K35.5"]


def test_cid_descriptions_of_the_two_bronchitis_codes_are_not_swapped():
    assert icd_entry("J20.9").description == "Bronquite aguda não especificada"
    assert icd_entry("J21.9").description == "Bronquiolite aguda não especificada"


# ---------- medicamentos planejados: fala corrida e classes ----------
@pytest.mark.parametrize(
    "fala, esperado",
    [
        ("vou passa amoxicilina de oito em oito horas", "amoxicilina"),
        ("Vou passar, então, amoxicilina.", "amoxicilina"),
        ("Vou passar penicilina benzatina.", "penicilina_benzatina"),
        ("Vou passar uma cefalosporina.", "@cefalosporina"),
        ("Vou passar sulfametoxazol com trimetoprima.", "sulfametoxazol_trimetoprima"),
        ("Pode tomar amoxicilina.", "amoxicilina"),
        ("Posso passar amoxicilina?", "amoxicilina"),
        ("Estou pensando em passar amoxicilina.", "amoxicilina"),
        ("Vou passar Clavulin.", "amoxicilina_clavulanato"),
    ],
)
def test_planned_drug_is_recognized_in_running_speech(fala, esperado):
    assert [p.ingredient for p in planned_drugs(fala)] == [esperado]


@pytest.mark.parametrize(
    "fala",
    [
        "Não posso passar amoxicilina por causa da alergia.",
        "Vou passar sulfato ferroso.",  # "sulfa" não é prefixo de sulfonamida aqui
        "Vou suspender a amoxicilina.",
        "Vou troca a glibenclamida.",
    ],
)
def test_running_speech_extensions_do_not_create_false_prescriptions(fala):
    assert [p.ingredient for p in planned_drugs(fala) if p.ingredient != "sulfato_ferroso"] == []


def test_swap_without_final_r_keeps_only_the_incoming_drug():
    assert [p.ingredient for p in planned_drugs("Vou troca a glibenclamida por gliclazida")] == ["gliclazida"]


def test_penicillin_by_class_name_from_the_doctor_alerts_the_penicillin_allergic_patient(record):
    ins = cen6_engine(record, [("medico", "Vou passar penicilina benzatina.")]).run()
    alert = next(m for m in ins.sugestoes_medicamentos if m.tipo == "alergia")
    assert alert.severidade == "critico" and "Fala/1" in alert.refs
    assert any(r.startswith("AllergyIntolerance/") for r in alert.refs)


def test_sulfa_allergy_reported_by_the_patient_alerts_when_the_doctor_says_the_generic_name(record):
    e = cen6_engine(
        record, [("paciente", "Sou alérgica a sulfa."), ("medico", "Vou passar sulfametoxazol com trimetoprima.")]
    )
    alert = next(m for m in e.run().sugestoes_medicamentos if m.tipo == "alergia")
    assert alert.severidade == "critico" and alert.refs[0] == "Fala/2" and "Fala/1" in alert.refs


# ---------- alergias e medicamentos do lado do paciente ----------
def test_allergy_of_a_relative_is_not_the_patients_allergy(record):
    e = cen6_engine(record, [("paciente", "Minha mãe tem alergia a sulfa."), ("medico", "Vou passar Bactrim.")])
    ins = e.run()
    assert [a.alergeno for a in ins.alergias if a.fonte == "relatado"] == []
    assert not [m for m in ins.sugestoes_medicamentos if m.tipo == "alergia"]


def test_own_allergy_stays_when_the_relative_is_mentioned_too(record):
    e = cen6_engine(record, [("paciente", "Eu tenho alergia a dipirona, minha mãe também.")])
    assert [a.alergeno for a in e.run().alergias if a.fonte == "relatado"] == ["Dipirona"]


def test_patient_asking_for_a_prescription_is_not_a_drug_in_use(record):
    e = cen6_engine(record, [("paciente", "Doutor, o senhor vai me passar amoxicilina?")])
    assert e.reported_meds() == []
    assert not [m for m in e.run().sugestoes_medicamentos if m.tipo == "alergia"]


def test_patient_taking_amoxicillin_with_penicillin_allergy_still_alerts(record):
    e = cen6_engine(record, [("paciente", "Tomei amoxicilina semana passada.")])
    assert [m for m in e.run().sugestoes_medicamentos if m.tipo == "alergia" and m.severidade == "critico"]


def test_cen6_golden_script_is_unchanged_by_the_running_speech_rules(record):
    e = cen6_engine(record, [(w["falante"], w["texto"]) for w in SCRIPT])
    assert e.planned_meds() == []  # o roteiro do cen6 não tem prescrição dita pelo médico


def test_ai_cannot_turn_a_denied_allergy_into_a_reported_one(client, auth, monkeypatch):
    from app.llm import client as llm_client
    from tests.test_consult import FakeClient, start
    from tests.test_consult_cid_medicamentos import say

    h = auth("medico")
    cid = start(client, h)["id"]
    say(
        client,
        h,
        cid,
        ("paciente", "Não tenho alergia a dipirona."),
        ("paciente", "Tenho alergia a mel, coça a garganta."),
    )
    payload = {
        "resumo": "x", "destaques": [], "queixas": [], "emocoes": [], "perguntas": [], "falantes": [],
        "hipoteses": [], "sugestoes_medicamentos": [], "cirurgia": [], "historico_relatado": [],
        "anamnese": {
            "queixa_principal": "x", "hda": "—", "antecedentes": [], "medicacoes_em_uso": [], "alergias": [],
            "habitos_e_contexto": [], "revisao_de_sistemas": [], "refs": ["Fala/1"],
        },
        "alergias_relatadas": [
            {"alergeno": "Dipirona", "reacao": "x", "criticidade": "alta", "refs": ["Fala/1"]},
            {"alergeno": "Mel", "reacao": "coça a garganta", "criticidade": "baixa", "refs": ["Fala/2"]},
        ],
    }  # fmt: skip
    monkeypatch.setattr(llm_client.get_settings(), "llm_mode", "real")
    monkeypatch.setattr(llm_client, "_client", FakeClient(payload))
    res = client.post(f"/consultations/{cid}/insights", headers=h)
    monkeypatch.setattr(llm_client, "_client", None)
    assert res.status_code == 200, res.text
    ia = [a["alergeno"] for a in res.json()["insights"]["alergias"] if a["origem"] == "ia"]
    assert ia == ["Mel"]
    assert any(d["texto"] == "Dipirona" and "nega" in d["motivo"] for d in res.json()["ia"]["descartados"])


# ---------- confirmar / descartar hipótese (#5, passo 7) ----------
def test_confirm_provisional_and_discard_a_hypothesis_end_to_end(client, auth):
    from tests.test_consult import start
    from tests.test_consult_cid_medicamentos import say

    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, *[(f["falante"], f["texto"]) for f in SCRIPT])
    codes = [x["cid"] for x in ins["hipoteses"]]
    assert {"N17.9", "E16.0", "I95.1"} <= set(codes) and all(x["decisao"] == "pendente" for x in ins["hipoteses"])

    def put(code, decisao, motivo=""):
        return client.put(
            f"/consultations/{cid}/hipoteses", headers=h, json={"cid": code, "decisao": decisao, "motivo": motivo}
        )

    assert put("J99.9", "confirmada").status_code == 422  # hipótese que não está na análise
    assert put("N17.9", "talvez").status_code == 422
    r = put("n179", "confirmada")  # CID normalizado
    assert r.status_code == 200, r.text
    assert next(x for x in r.json()["insights"]["hipoteses"] if x["cid"] == "N17.9")["decisao"] == "confirmada"
    assert put("I95.1", "provisoria").status_code == 200
    r = put("E16.0", "descartada", "glicemia normal na triagem")
    assert r.status_code == 200
    # a decisão sobrevive a uma nova fala (as regras rodam de novo)
    ins = say(client, h, cid, ("paciente", "Obrigada, doutora."))
    by = {x["cid"]: x["decisao"] for x in ins["hipoteses"]}
    assert by["N17.9"] == "confirmada" and by["I95.1"] == "provisoria" and by["E16.0"] == "descartada"

    note = client.post(f"/consultations/{cid}/finish", headers=h).json()["nota"]
    props = {c["codigo"]: c["verificacao"] for c in note["condicoes_propostas"]}
    assert props == {"N17.9": "confirmed", "I95.1": "provisional"}
    got = {c["codigo"]: c["tipo"] for c in note["cids"]}
    assert got["N17.9"] in ("principal", "secundario") and got["I95.1"] == "hipotese" and "E16.0" not in got
    assert "Hipóteses afastadas pelo profissional" in note["avaliacao"]["texto"]
    assert "glicemia normal na triagem" in note["avaliacao"]["texto"]
    # depois de finalizar não muda mais; a edição da nota não perde as propostas
    assert put("I10", "confirmada").status_code == 409
    edited = client.put(f"/consultations/{cid}/note", headers=h, json=note | {"condicoes_propostas": []})
    assert edited.status_code == 200
    assert {c["codigo"] for c in edited.json()["nota"]["condicoes_propostas"]} == {"N17.9", "I95.1"}


def test_clearing_a_decision_returns_the_hypothesis_to_pending(client, auth):
    from tests.test_consult import start
    from tests.test_consult_cid_medicamentos import say

    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, ("paciente", "Doutora, estou com tontura."))
    put = lambda d: client.put(  # noqa: E731
        f"/consultations/{cid}/hipoteses", headers=h, json={"cid": "I95.1", "decisao": d}
    )
    assert put("descartada").status_code == 200
    r = put("pendente")
    assert next(x for x in r.json()["insights"]["hipoteses"] if x["cid"] == "I95.1")["decisao"] == "pendente"
