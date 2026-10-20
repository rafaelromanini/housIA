"""CID-10 oficial validado, base de hipóteses ampliada (#5) e medicamentos e alergias de qualquer falante (#6).

Cada critério de aceite passa pelo caminho real (API/TestClient sobre o cen6 e o amb01, LLM mock; cliente falso onde
o critério é sobre a saída da IA). As bases curadas têm testes de esquema.
"""

import csv
import re
from types import SimpleNamespace

import pytest

from app import clock
from app.consult import ai as consult_ai
from app.consult import knowledge
from app.consult import service as consult_service
from app.consult.cid import screen
from app.consult.engine import Engine
from app.consult.schemas import CidSuggestion, ConsultNote, Segment
from app.consult.text import brands, mentioned_drugs, planned_drugs
from app.fhir_client import codes
from app.llm import client as llm_client
from app.llm.client import LLMResult
from app.llm.pseudonymize import Pseudonymizer
from app.reference import (
    adverse_patterns,
    cid10,
    class_labels,
    drugs,
    icd_entry,
    icd_problem,
    interactions,
    norm_icd,
    valid_icd,
)
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from tests.test_consult import FakeClient, start
from tests.test_consult import say as say_script
from tests.test_consult_perguntas import child_engine

SCRIPT = knowledge.demo_scripts()["cen6_consulta"]["falas"]
REF = "data/reference"


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def say(client, h, cid, *falas: tuple[str, str]) -> dict:
    payload = [{"falante": who, "texto": text, "t": float(i)} for i, (who, text) in enumerate(falas)]
    return say_script(client, h, cid, payload)


def cen6_engine(record, falas: list[tuple[str, str]], specialty: str = "clinica_medica") -> Engine:
    rec = record("cen6")
    segs = [Segment(n=i, t=float(i), falante=who, texto=t) for i, (who, t) in enumerate(falas, 1)]  # type: ignore[arg-type]
    return Engine(rec, build_timeline(rec), build_context(rec), segs, specialty, "consulta", clock.now())


def read(name: str) -> list[dict[str, str]]:
    with open(knowledge.get_settings().data_dir / "reference" / name, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ======================================================================================================
# #5 — CID-10 oficial
# ======================================================================================================
def test_cid10_table_is_the_official_datasus_one():
    table = cid10()
    assert len(table) > 14000  # 2.045 categorias + 12.188 subcategorias (revisão 2008)
    for code in ("J06.9", "J18.9", "J21.9", "A09", "E86", "N39.0", "H66.9", "J03.9", "A90", "G44.2", "G43.9"):
        assert code in table, code
    assert icd_entry("H66.9").description == "Otite média não especificada"
    assert icd_entry("N80.0").sex == "F" and icd_entry("N40").sex == "M" or icd_entry("N40").sex == ""
    assert all(e.kind in ("categoria", "subcategoria") for e in table.values())
    # a descrição vem do arquivo do DATASUS, em português
    assert icd_entry("J45.9").description.startswith("Asma")


def test_icd_validation_normalizes_and_refuses_invented_codes():
    assert valid_icd("J06.9") and valid_icd("j069") and valid_icd(" J06,9 ") and valid_icd("E86")
    assert norm_icd("j069") == "J06.9"
    assert not valid_icd("J99.9") and not valid_icd("Z99.99") and not valid_icd("") and not valid_icd(None)
    assert not valid_icd("ABC") and not valid_icd("J6.9")
    assert icd_problem("J99.9") == "CID inexistente"
    assert icd_problem("N80.0", "M") == "CID restrito ao sexo feminino"
    assert icd_problem("N80.0", "F") is None
    # X99.9 existe na tabela, mas causa externa não pode ser a hipótese/diagnóstico
    assert valid_icd("X99.9") and icd_problem("X99.9", primary=True) == "CID de causa externa não é diagnóstico"
    assert icd_problem("X99.9") is None  # como diagnóstico secundário, complementa


def test_build_script_documents_origin_and_matches_the_csv():
    text = open("../../scripts/build_cid10.py", encoding="utf-8").read()
    assert "DATASUS" in text and "CID10CSV.zip" in text
    rows = read("cid10.csv")
    assert list(rows[0]) == ["codigo", "descricao", "tipo", "sexo"]
    assert len({r["codigo"] for r in rows}) == len(rows)
    assert all(re.fullmatch(r"[A-Z]\d{2}(\.\d)?", r["codigo"]) for r in rows)


def test_every_cid_of_the_differentials_exists_in_the_official_table():
    rows = read("consult_differentials.csv")
    assert not [r["cid"] for r in rows if r["cid"] not in cid10()]
    # e nenhuma linha é barrada pela regra que vale para hipótese (causa externa, restrição de sexo)
    for r in rows:
        problem = icd_problem(r["cid"], r["sexo"], primary=True)
        assert problem is None, (r["hipotese"], problem)


def test_differentials_base_has_over_40_curated_rows_with_source_question_and_exam():
    rows = read("consult_differentials.csv")
    assert len(rows) >= 40 and len({r["hipotese"] for r in rows}) == len(rows)
    cids = {r["cid"] for r in rows}
    required = "J06.9 J18.9 J21.9 A09 E86 N39.0 H66.9 J03.9 A90 G44.2 G43.9 M54.5 K21.9 F41.1 F32.9".split()
    assert set(required) <= cids
    symptoms = {d.key for d in knowledge.symptoms()}
    classes = set(class_labels())
    for r in rows:
        name = r["hipotese"]
        for col in ("especialidade", "pergunta", "exame", "fonte", "proximos_passos", "sinais_de_alarme"):
            assert r[col].strip(), (name, col)
        if r["idade_min"] and r["idade_max"]:
            assert int(r["idade_min"]) <= int(r["idade_max"]), name
        assert r["sexo"] in ("", "F", "M"), name
        tokens = [t for t in r["evidencias"].split("|") if t]
        favor = [t.lstrip("!") for t in tokens if not t.startswith("-")]
        assert int(r["minimo"]) <= len(favor), name
        for token in (t.lstrip("!-") for t in tokens):
            kind, _, spec = token.partition(":")
            assert kind in ("s", "l", "v", "c", "d", "p", "n"), (name, token)
            if kind == "s":
                assert spec in symptoms, (name, token)
            elif kind in ("l", "v"):
                key, op, val = spec.split(":")
                assert key in (codes.LABS if kind == "l" else {**codes.VITALS}) or key == "pas", (name, token)
                assert op in ("lt", "gt") and float(val) >= 0, (name, token)
            elif kind == "c":
                assert spec in classes or spec in drugs(), (name, token)


def test_each_hypothesis_points_to_a_question_and_an_exam_that_discriminate_it(record):
    # cen6 + fala de queixa que levanta hipótese: pergunta e exame chegam na saída, com a fonte
    e = cen6_engine(record, [("paciente", "Doutora, estou com falta de ar e uma tosse com catarro, com febre.")])
    hyps = {h.cid: h for h in e.run().hipoteses}
    assert "J18.9" in hyps
    h = hyps["J18.9"]
    assert h.pergunta_discriminante and h.exame_discriminante and h.fonte
    assert h.cid_descricao == icd_entry("J18.9").description


def test_mock_fever_and_cough_gives_a_respiratory_hypothesis_with_valid_cid(client, auth):
    h = auth("medico")
    cid = start(client, h, pid="amb01")["id"]
    ins = say(
        client,
        h,
        cid,
        ("medico", "Bom dia, o que a traz aqui hoje?"),
        ("paciente", "Doutora, estou com febre e tosse há três dias."),
    )
    resp = [x for x in ins["hipoteses"] if x["cid"].startswith("J")]
    assert resp and all(valid_icd(x["cid"]) for x in resp)
    assert resp[0]["cid_descricao"] and resp[0]["origem"] == "regra"
    assert all(x["refs"] and all(r.startswith("Fala/") for r in x["refs"]) for x in resp)


def test_child_with_fever_and_ear_pain_gets_acute_otitis_media(record):
    e = child_engine(
        record,
        "pediatria",
        [("acompanhante", "Doutora, ele está com febre e dor de ouvido desde ontem, chorando muito.")],
        birth="2021-03-01",
    )
    hyps = {h.cid: h for h in e.run().hipoteses}
    assert e.r.age is not None and e.r.age < 12
    assert "H66.9" in hyps and hyps["H66.9"].hipotese == "Otite média aguda"
    assert {x.refs[0] for x in hyps["H66.9"].a_favor} == {"Fala/1"}


def test_hypotheses_respect_age_and_sex_of_the_patient(record):
    adult = cen6_engine(record, [("paciente", "Doutora, ando com febre, tosse, coriza e chiado no peito.")])
    assert "J21.9" not in {h.cid for h in adult.run().hipoteses}  # bronquiolite é de lactente (até 2 anos)
    woman = cen6_engine(record, [("paciente", "Doutora, o jato do xixi está fraco e levanto muito à noite.")])
    assert "N40" not in {h.cid for h in woman.run().hipoteses}  # próstata: só homem


def test_normal_vital_sign_counts_as_evidence_against_hypertensive_crisis_with_source(record):
    e = cen6_engine(record, [("paciente", "Doutora, estou com uma dor de cabeça forte e a visão embaçada.")])
    crisis = next(h for h in e.run().hipoteses if h.cid == "I10")
    assert {x.texto for x in crisis.a_favor} == {"Relata dor de cabeça", "Relata visão embaçada"}
    against = next(x for x in crisis.contra if "PA" in x.texto)
    assert "118/72" in against.texto and "não sustenta" in against.texto
    assert against.refs and against.refs[0].startswith("Observation/")  # a fonte do valor normal


def test_surgery_already_done_counts_against_the_hypothesis_with_the_procedure_as_source(record):
    rec = record("cen1")  # colecistectomia convertida, registrada no prontuário
    segs = [
        Segment(n=1, t=0, falante="paciente", texto="Doutora, estou com dor na barriga, enjoo e os olhos amarelos.")
    ]
    ins = Engine(rec, build_timeline(rec), build_context(rec), segs, "clinica_medica", "visita", clock.now()).run()
    colic = next(h for h in ins.hipoteses if h.cid == "K80.2")
    against = next(x for x in colic.contra if x.texto.startswith("Já realizou: Colecistectomia"))
    assert against.refs == ["Procedure/cen1-proc1"]


def test_required_evidence_blocks_a_hypothesis_without_its_main_symptom(record):
    # cansaço sozinho não levanta cefaleia tensional nem refluxo (a ficha "!" exige o sintoma principal)
    e = cen6_engine(record, [("paciente", "Doutora, estou muito cansada e com tosse.")])
    cids = {h.cid for h in e.run().hipoteses}
    assert "G44.2" not in cids and "K21.9" not in cids


def test_invalid_cid_from_the_ai_never_reaches_the_note_and_goes_to_descartados(client, auth, monkeypatch):
    """X99.9 é causa externa (não é hipótese) e J99.9 não existe: nenhum dos dois chega à análise, ambos ficam em
    `descartados` com o motivo; o CID válido da IA entra normalizado e com a descrição oficial."""
    h = auth("medico")
    cid = start(client, h)["id"]
    say_script(client, h, cid, SCRIPT)
    hyp = lambda name, code: {  # noqa: E731
        "hipotese": name,
        "cid": code,
        "confianca": "baixa",
        "a_favor": [{"texto": "Tontura relatada", "refs": ["Fala/4"]}],
        "contra": [],
        "proximos_passos": "Avaliar.",
        "sinais_de_alarme": "",
        "refs": ["Fala/4"],
    }
    payload = {
        "resumo": "x",
        "destaques": [],
        "queixas": [],
        "emocoes": [],
        "perguntas": [],
        "falantes": [],
        "hipoteses": [hyp("Agressão", "X99.9"), hyp("Inventada", "J99.9"), hyp("Vertigem", "h811")],
        "sugestoes_medicamentos": [],
        "cirurgia": [],
        "historico_relatado": [],
        "anamnese": {
            "queixa_principal": "Tontura",
            "hda": "—",
            "antecedentes": [],
            "medicacoes_em_uso": [],
            "alergias": [],
            "habitos_e_contexto": [],
            "revisao_de_sistemas": [],
            "refs": ["Fala/4"],
        },
    }
    monkeypatch.setattr(llm_client.get_settings(), "llm_mode", "real")
    monkeypatch.setattr(llm_client, "_client", FakeClient(payload))
    res = client.post(f"/consultations/{cid}/insights", headers=h)
    monkeypatch.setattr(llm_client, "_client", None)
    assert res.status_code == 200, res.text
    body = res.json()
    cids = {x["cid"] for x in body["insights"]["hipoteses"]}
    assert "X99.9" not in cids and "J99.9" not in cids and "H81.1" in cids
    vertigo = next(x for x in body["insights"]["hipoteses"] if x["cid"] == "H81.1")
    assert vertigo["origem"] == "ia" and vertigo["cid_descricao"] == icd_entry("H81.1").description
    dropped = {d["cid"]: d["motivo"] for d in body["ia"]["descartados"] if "motivo" in d}
    assert dropped["X99.9"] == "CID de causa externa não é diagnóstico" and dropped["J99.9"] == "CID inexistente"


def _fake_structured(output: dict):
    def fake(name, variables, schema, mock=None, effort=None):
        return LLMResult(output=schema.model_validate(output), model="fake", prompt_version="x", mode="real")

    return fake


def test_invalid_cid_in_the_final_note_is_dropped_with_reason(record, monkeypatch):
    e = cen6_engine(record, [(w["falante"], w["texto"]) for w in SCRIPT])
    ins = e.run()
    note = consult_ai.mock_note(e, ins, "pt-BR")
    note["cids"] = [
        {"codigo": "X99.9", "descricao": "Agressão", "tipo": "hipotese", "refs": ["Fala/4"]},
        {"codigo": "J99.9", "descricao": "Inventado", "tipo": "principal", "refs": ["Fala/4"]},
        {"codigo": "X99.9", "descricao": "Causa externa de um trauma", "tipo": "secundario", "refs": ["Fala/4"]},
        {"codigo": "e16.0", "descricao": "Hipoglicemia", "tipo": "hipotese", "refs": ["Fala/4"]},
        {
            "codigo": "N18.3",
            "descricao": "DRC 3 (registrada pelo profissional)",
            "tipo": "secundario",
            "refs": ["Fala/4"],
        },
    ]
    monkeypatch.setattr(consult_ai, "generate_structured", _fake_structured(note))
    valid = {f"Fala/{s.n}" for s in e.segments}
    result, _, dropped = consult_ai.run_note(e, ins, Pseudonymizer(), valid, "pt-BR")
    assert isinstance(result, ConsultNote)
    got = {(c.codigo, c.tipo) for c in result.cids}
    assert got == {("X99.9", "secundario"), ("E16.0", "hipotese"), ("N18.3", "secundario")}
    reasons = {d["cid"]: d["motivo"] for d in dropped if "cid" in d}
    assert reasons == {"J99.9": "CID inexistente"} | {"X99.9": "CID de causa externa não é diagnóstico"}


def test_invalid_cid_in_the_surgical_report_is_dropped(record):
    items = [
        CidSuggestion(codigo="K35.9", descricao="Apendicite", tipo="principal", refs=["Fala/1"]),
        CidSuggestion(codigo="K99.99", descricao="Inventado", tipo="principal", refs=["Fala/1"]),
        CidSuggestion(codigo="N40", descricao="Próstata", tipo="secundario", refs=["Fala/1"]),
    ]
    kept, dropped = screen(items, "cids", "F", primary=lambda c: c.tipo != "secundario")
    assert [c.codigo for c in kept] == ["K35.9"]
    assert {d["cid"]: d["motivo"] for d in dropped} == {
        "K99.99": "CID inexistente",
        "N40": "CID restrito ao sexo masculino",
    }


def test_rule_engine_sends_a_differential_with_a_refused_cid_to_descartados(record, monkeypatch):
    from app.consult import engine as engine_mod

    rows = [
        knowledge.DifferentialDef("Linha com CID inexistente", "J99.9", ("s:tontura",), 1, "x", "y"),
        knowledge.DifferentialDef("Linha válida", "H81.1", ("s:tontura",), 1, "x", "y"),
    ]
    monkeypatch.setattr(engine_mod.knowledge, "differentials", lambda: tuple(rows))
    ins = cen6_engine(record, [("paciente", "Doutora, estou com tontura.")]).run()
    assert [h.cid for h in ins.hipoteses] == ["H81.1"]
    assert ins.descartados == [
        {
            "onde": "hipoteses",
            "texto": "Linha com CID inexistente",
            "refs_invalidas": [],
            "motivo": "CID inexistente",
            "cid": "J99.9",
        }
    ]


# ======================================================================================================
# #6 — medicamentos e alergias de qualquer falante
# ======================================================================================================
def test_brand_base_has_at_least_150_brands_and_all_point_to_the_catalog():
    rows = read("drug_brands.csv")
    assert len(rows) >= 150
    assert len({r["marca"].lower() for r in rows}) == len(rows)
    assert all(r["principio_ativo"] in drugs() for r in rows)
    assert len(brands()) == len(rows)


@pytest.mark.parametrize(
    ("fala", "ingredient"),
    [
        ("Tomo Puran todo dia de manhã", "levotiroxina"),
        ("Tomo Puran T4 em jejum", "levotiroxina"),
        ("Tomo o Glifage de 850", "metformina"),
        ("Uso Daonil antes do café", "glibenclamida"),
        ("Tomei um Dorflex ontem", "dipirona"),
        ("Tomo Rivotril para dormir", "clonazepam"),
        ("Tomo Losaprex e Norvasc", "losartana"),
        ("Uso Xarelto por causa do coração", "rivaroxabana"),
    ],
)
def test_brand_names_become_the_active_ingredient(fala, ingredient):
    assert any(m.ingredient == ingredient and m.status == "em_uso" for m in mentioned_drugs(fala)), fala


def test_brand_is_a_whole_word():
    assert not mentioned_drugs("Meu pai toma atenolol")[0].term == "Atenol"  # "atenol" não casa com "atenolol"
    assert [m.ingredient for m in mentioned_drugs("Meu pai toma atenolol")] == ["atenolol"]


def test_puran_reported_by_the_patient_becomes_levothyroxine_in_use(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("paciente", "Doutora, eu tomo Puran todo dia, mas esqueci de contar."))
    item = next(m for m in ins["medicamentos"] if m["medicamento"] == "Levotiroxina")
    assert item["status"] == "relatado" and item["refs"] == ["Fala/1"]


def test_allergy_to_iodine_and_contrast_is_captured_as_reported_allergy(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(
        client, h, cid, ("paciente", "Doutora, eu tenho alergia a iodo, contraste. Já fiquei com a boca inchada.")
    )
    a = next(a for a in ins["alergias"] if a["fonte"] == "relatado")
    assert a["alergeno"] == "Iodo e contraste iodado" and a["refs"] == ["Fala/1"]
    assert a["criticidade"] == "alta" and a["fonte_regra"]  # boca inchada = reação de via aérea


def test_doctor_saying_amoxicillin_to_a_penicillin_allergic_patient_raises_a_critical_alert(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, *[(f["falante"], f["texto"]) for f in SCRIPT])
    assert not any("Amoxicilina" in m["texto"] for m in ins["sugestoes_medicamentos"])  # antes da fala do médico
    ins = say(client, h, cid, ("medico", "Vou passar amoxicilina 500 mg de oito em oito horas por sete dias."))
    alert = next(m for m in ins["alertas"] if "Amoxicilina" in m["texto"])
    assert alert["severidade"] == "critico" and "Fala/31" in alert["refs"]
    assert any(r.startswith("AllergyIntolerance/") for r in alert["refs"])
    suggestion = next(m for m in ins["sugestoes_medicamentos"] if m["tipo"] == "alergia")
    assert suggestion["fonte"] and "prescrever" in suggestion["texto"]
    penicillin = next(a for a in ins["alergias"] if a["fonte"] == "prontuario")
    assert any("amoxicilina" in c.lower() for c in penicillin["conflitos"])
    # a prescrição planejada aparece na tabela de medicamentos, sem virar "em uso"
    planned = next(m for m in ins["medicamentos"] if m["medicamento"] == "Amoxicilina")
    assert planned["status"] == "planejado" and planned["refs"] == ["Fala/31"]


@pytest.mark.parametrize(
    "fala",
    [
        "Não vou passar amoxicilina por causa da sua alergia.",
        "A senhora toma amoxicilina?",
        "Amoxicilina eu não passo para a senhora.",
        "O outro médico passou amoxicilina, mas vamos suspender.",
        "Vou pedir um hemograma e uma glicemia.",
        "Se a febre voltar, vamos pensar em outro antibiótico.",
        "Vou examinar a senhora agora.",
        "Evite amoxicilina e outros remédios dessa família.",
    ],
)
def test_doctor_sentences_that_are_not_a_new_prescription_do_not_alert(record, fala):
    e = cen6_engine(record, [("medico", fala)])
    assert e.planned_meds() == []
    assert not [m for m in e.run().sugestoes_medicamentos if m.tipo == "alergia"]


def test_patient_saying_they_will_take_something_is_not_a_prescription(record):
    e = cen6_engine(record, [("paciente", "Vou tomar amoxicilina que sobrou aqui em casa.")])
    assert e.planned_meds() == []


def test_planned_drug_parser_handles_swaps_lists_and_instead_of():
    p = lambda text: [(x.ingredient, x.verb) for x in planned_drugs(text)]  # noqa: E731
    assert p("Vou trocar a glibenclamida por gliclazida") == [("gliclazida", "trocar")]
    assert p("Vou trocar a glibenclamida") == []
    assert p("Vou passar losartana, anlodipino e sinvastatina") == [
        ("losartana", "passar"),
        ("anlodipino", "passar"),
        ("sinvastatina", "passar"),
    ]
    assert p("Vou passar dipirona em vez de ibuprofeno") == [("dipirona", "passar")]
    assert p("Vamos começar com Puran T4") == [("levotiroxina", "comecar")]
    assert p("Vou aumentar a dose da losartana") == [("losartana", "aumentar")]
    assert p("Vou pedir uma tomografia com contraste") == [("@contraste_iodado", "pedir")]
    assert p("Vou fazer uma tomografia sem contraste") == []
    assert p("Vou passar um anti-inflamatório") == [("@aine", "passar")]


def test_planned_drug_goes_through_renal_dose_and_interaction_checks(record):
    e = cen6_engine(
        record,
        [
            ("medico", "Para a dor no joelho vou passar diclofenaco."),
            ("medico", "E vou prescrever claritromicina para essa infecção."),
        ],
    )
    suggestions = e.run().sugestoes_medicamentos
    dose = next(m for m in suggestions if m.texto.startswith("Diclofenaco (a prescrever)"))
    assert dose.severidade == "critico" and dose.tipo == "alternativa" and "Fala/1" in dose.refs
    assert dose.fonte  # a regra de dose cita a fonte
    inter = next(m for m in suggestions if "Sinvastatina" in m.texto and "Claritromicina" in m.texto)
    assert inter.severidade == "critico" and "Fala/2" in inter.refs and inter.fonte


def test_cross_reactivity_is_an_attention_alert_with_the_rule_source(record):
    e = cen6_engine(record, [("medico", "Vou passar cefalexina por sete dias.")])
    ins = e.run()
    cross = next(m for m in ins.sugestoes_medicamentos if m.texto.startswith("Reatividade cruzada"))
    assert cross.severidade == "atencao" and "Fala/1" in cross.refs and cross.fonte
    # cruzada não é bloqueio: só alerta crítico bloqueia (a de atenção aparece em `alertas`, recolhida)
    assert not [m for m in ins.alertas if "Cefalexina" in m.texto and m.severidade == "critico"]


def test_reported_drug_is_crossed_with_the_chart_allergy_chart_py_fix(record):
    """Paciente alérgica à penicilina (prontuário) que diz que toma amoxicilina por conta própria."""
    e = cen6_engine(record, [("paciente", "Doutora, eu tomei uma amoxicilina que sobrou da minha filha.")])
    ins = e.run()
    penicillin = next(a for a in ins.alergias if a.fonte == "prontuario")
    assert any("relatado na consulta" in c for c in penicillin.conflitos)
    alert = next(m for m in ins.alertas if "Amoxicilina" in m.texto)
    assert alert.refs[0] == "Fala/1" and alert.refs[-1].startswith("AllergyIntolerance/")


def test_allergy_gets_criticality_from_the_chart_and_from_the_reaction_told(record):
    e = cen6_engine(
        record,
        [
            ("paciente", "Tenho alergia a camarão, a minha garganta fechou uma vez."),
            ("paciente", "Também tenho alergia a látex, só dá coceira na pele."),
            ("paciente", "E alergia a amendoim."),
        ],
    )
    by = {a.alergeno: a for a in e.run().alergias}
    assert by["Penicilinas (amoxicilina)"].criticidade == "alta"  # criticality=high no AllergyIntolerance
    assert by["Camarão/crustáceos"].criticidade == "alta"
    assert by["Látex"].criticidade == "baixa"
    assert by["Amendoim"].criticidade == "indeterminada"


def test_cen6_shows_dizziness_after_hydrochlorothiazide_started_with_both_sources(record):
    e = cen6_engine(record, [(f["falante"], f["texto"]) for f in SCRIPT])
    ins = e.run()
    adverse = [m for m in ins.sugestoes_medicamentos if m.tipo == "efeito_adverso"]
    assert len(adverse) == 1  # a glibenclamida e a losartana começaram há meses/anos: fora da janela de 90 dias
    m = adverse[0]
    assert m.texto.startswith("Tontura após o início de Hidroclorotiazida em ")
    assert "Fala/26" in m.refs and "MedicationRequest/cen6-rx-hidroclorotiazida-c3" in m.refs
    assert any(r in m.refs for r in ("Fala/2", "Fala/4"))  # a fala em que o sintoma foi contado
    assert m.severidade == "atencao" and m.fonte


def test_adverse_effect_needs_the_symptom_to_start_after_the_drug(record):
    # tontura "há dois anos", muito antes da hidroclorotiazida (iniciada há ~3 meses): não é efeito dela
    e = cen6_engine(
        record,
        [
            ("medico", "Desde quando a senhora sente a tontura?"),
            ("paciente", "Estou tonta faz uns dois anos."),
        ],
    )
    assert not [m for m in e.run().sugestoes_medicamentos if m.tipo == "efeito_adverso"]


def test_adverse_effect_without_onset_date_only_counts_for_recent_drugs(record):
    # sem data do início do sintoma, a hidroclorotiazida (91 dias) é antiga demais para o alerta
    e = cen6_engine(record, [("paciente", "Doutora, ando muito tonta.")])
    assert not [m for m in e.run().sugestoes_medicamentos if m.tipo == "efeito_adverso"]


def test_high_alert_seal_for_planned_and_reported_high_alert_drugs(record):
    e = cen6_engine(record, [("medico", "Vamos começar insulina NPH à noite.")])
    seal = next(m for m in e.run().sugestoes_medicamentos if m.tipo == "alto_alerta")
    assert seal.texto.startswith("Dupla checagem: Insulina NPH (a prescrever)") and seal.severidade == "atencao"
    assert "Fala/1" in seal.refs and seal.fonte


def test_high_alert_seal_for_chart_drugs_reuses_the_reconciliation_findings(record):
    rec = record("cen3")  # insulina NPH e regular ativas no prontuário
    e = Engine(rec, build_timeline(rec), build_context(rec), [], "clinica_medica", "consulta", clock.now())
    seals = [m for m in e.run().sugestoes_medicamentos if m.tipo == "alto_alerta"]
    assert any(m.texto.startswith("Dupla checagem: Insulina") and m.severidade == "info" for m in seals)
    assert all(m.refs[0].startswith("MedicationRequest/") and m.fonte for m in seals)


def test_ai_reported_allergy_is_validated_sealed_and_checked_against_the_drugs(client, auth, monkeypatch):
    """`alergias_relatadas` da IA: vale com fonte em fala do paciente; entra como "IA — confirmar" e passa pela mesma
    checagem das regras (ibuprofeno relatado × alergia a anti-inflamatório)."""
    h = auth("medico")
    cid = start(client, h)["id"]
    say_script(client, h, cid, SCRIPT)
    payload = {
        "resumo": "x", "destaques": [], "queixas": [], "emocoes": [], "perguntas": [], "falantes": [],
        "hipoteses": [], "sugestoes_medicamentos": [], "cirurgia": [], "historico_relatado": [],
        "anamnese": {
            "queixa_principal": "Tontura", "hda": "—", "antecedentes": [], "medicacoes_em_uso": [], "alergias": [],
            "habitos_e_contexto": [], "revisao_de_sistemas": [], "refs": ["Fala/4"],
        },
        "alergias_relatadas": [
            {"alergeno": "Ibuprofeno", "reacao": "manchas na pele", "criticidade": "baixa", "refs": ["Fala/20"]},
            {"alergeno": "Dipirona", "reacao": "x", "criticidade": "alta", "refs": ["Fala/19"]},  # fala da médica
            {"alergeno": "Mel", "reacao": "x", "criticidade": "baixa", "refs": ["Fala/999"]},  # fonte inexistente
            {"alergeno": "Camarão", "reacao": "boca inchou", "criticidade": "alta", "refs": ["Fala/20"]},  # já tem
        ],
    }  # fmt: skip
    monkeypatch.setattr(llm_client.get_settings(), "llm_mode", "real")
    monkeypatch.setattr(llm_client, "_client", FakeClient(payload))
    res = client.post(f"/consultations/{cid}/insights", headers=h)
    monkeypatch.setattr(llm_client, "_client", None)
    assert res.status_code == 200, res.text
    ins = res.json()["insights"]
    by = {a["alergeno"]: a for a in ins["alergias"]}
    aine = by["Anti-inflamatórios (AINE)"]
    assert aine["origem"] == "ia" and aine["fonte"] == "relatado" and aine["refs"] == ["Fala/20"]
    assert aine["criticidade"] == "baixa" and any("Ibuprofeno" in c for c in aine["conflitos"])
    assert "Dipirona" not in by and "Mel" not in by  # fala do profissional e fonte inexistente
    assert sum(a["alergeno"].startswith("Camarão") for a in ins["alergias"]) == 1  # sem duplicar
    dropped = {d["texto"] for d in res.json()["ia"]["descartados"]}
    assert {"Dipirona", "Mel"} <= dropped
    assert any(m["tipo"] == "alergia" and "Ibuprofeno" in m["texto"] for m in ins["sugestoes_medicamentos"])
    assert any(a["severidade"] == "critico" and "Ibuprofeno" in a["texto"] for a in ins["alertas"])


# ---------- bases curadas: esquema ----------
def test_allergy_base_has_25_allergens_each_with_source_and_the_required_groups():
    rows = read("allergy_cross.csv")
    assert len(rows) >= 25 and len({r["alergeno"] for r in rows}) == len(rows)
    for r in rows:
        assert r["fonte"].strip(), r["alergeno"]
        assert r["palavras"] and r["rotulo"] and r["evitar"], r["alergeno"]
        for token in (t for t in r["cruza_com"].split("|") if t):
            assert token.startswith("@") and token[1:] in class_labels() or token in drugs(), (r["alergeno"], token)
        if r["alergeno"].startswith("@"):
            assert r["alergeno"][1:] in class_labels(), r["alergeno"]
        elif not re.match(r"(alimento|veneno|antisseptico):|latex|niquel|gadolinio", r["alergeno"]):
            assert r["alergeno"] in drugs(), r["alergeno"]
    keys = {r["alergeno"] for r in rows}
    needed = {
        "@contraste_iodado", "latex", "alimento:ovo", "aas", "@aine", "@sulfonamida", "@fluoroquinolona",
        "@macrolideo", "carbamazepina", "fenitoina", "fenobarbital", "@opioide", "@anestesico_local", "@cefalosporina",
        "cefalexina", "ceftriaxona", "cefazolina",
    }  # fmt: skip
    assert needed <= keys


def test_dose_alerts_interactions_and_allergy_rows_all_have_a_source():
    assert all(r["fonte"].strip() for r in read("dose_alerts.csv"))
    assert all(r["fonte"].strip() for r in read("interactions.csv")) and all(r["fonte"] for r in interactions())
    assert all(r["fonte"].strip() for r in read("allergy_cross.csv"))
    assert all(d.source for d in knowledge.dose_alerts()) and all(a.source for a in knowledge.allergy_defs())


def test_adverse_patterns_map_every_adverse_effect_to_symptoms_of_the_base():
    symptoms = {d.key for d in knowledge.symptoms()}
    for r in adverse_patterns():
        if r["tipo"] == "efeito_adverso":
            assert r["sintomas"], r
            assert set(r["sintomas"].split("|")) <= symptoms, r


def test_new_catalog_drugs_have_classes_with_labels():
    assert {d.drug_class for d in drugs().values()} <= set(class_labels())
    assert all(re.fullmatch(r"[A-Z]\d{2}[A-Z]{2}\d{2}", d.atc) for d in drugs().values())
    assert len({d.atc for d in drugs().values()}) == len(drugs())


def test_ai_output_models_stay_small_for_the_structured_output_grammar():
    # a saída clínica ganhou um campo pequeno (alergias_relatadas): três textos, uma enumeração e as refs
    assert set(consult_ai.AiAlergia.model_fields) == {"alergeno", "reacao", "criticidade", "refs"}
    assert "alergias_relatadas" in consult_ai.ClinicaOutput.model_fields
    assert "alergias_relatadas" not in consult_ai.ConversaOutput.model_fields


def test_prompt_versions_were_bumped():
    from app.llm.client import load_prompt

    assert load_prompt("consult_live_clinica").version == "1.5.0"
    assert load_prompt("consult_note").version == "1.5.0"
    assert load_prompt("surgical_report").version == "1.2.0"


def test_simple_namespace_import_is_used():  # mantém o import usado se um teste acima mudar
    assert SimpleNamespace(a=1).a == 1
