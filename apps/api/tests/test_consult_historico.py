"""#7 — Histórico, cirurgias e exames ditos na conversa viram dados estruturados (+ RCRI), pelo caminho real."""

import re

import pytest

from app import clock
from app.consult import extract, knowledge
from app.consult.engine import Engine
from app.consult.schemas import Segment
from app.reference import valid_icd
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from tests.test_consult import SCRIPT, say, start


def engine_for(record, falas, pid="cen6", specialty="clinica_medica") -> Engine:
    rec = record(pid)
    segs = [Segment(n=i, t=float(i), falante=who, texto=t) for i, (who, t) in enumerate(falas, 1)]  # type: ignore[arg-type]
    return Engine(rec, build_timeline(rec), build_context(rec), segs, specialty, "consulta", clock.now())


def relatado(ins, kind):
    """Itens do histórico que saíram da conversa (antecedentes: `condicao`; cirurgias: `procedimento`)."""
    return [h for h in ins.historico if (h.condicao if kind == "condicao" else h.procedimento)]


# ---------- léxicos em dados ----------
def test_lexicons_are_curated_data_with_real_icd_codes():
    conditions = knowledge.conditions()
    assert len(conditions) >= 80
    for c in conditions:
        assert valid_icd(c.cid), f"{c.key}: CID {c.cid} não existe na tabela oficial"
        assert c.terms and c.source
        assert all(t == t.lower() and re.fullmatch(r"[a-z0-9 \-]+", t) for t in c.terms), c.key  # sem acento
    assert len({c.key for c in conditions}) == len(conditions)
    assert len(knowledge.procedures()) >= 15
    assert all(p.terms and p.chart_patterns and p.source for p in knowledge.procedures())


def test_lab_lay_has_at_least_30_analytes_with_reference_ranges():
    lay = knowledge.lab_lay()
    assert len(lay) >= 30
    required = {"tsh", "t4_livre", "colesterol_total", "hdl", "triglicerides", "albuminuria", "eas", "tgo", "tgp"}
    required |= {"acido_urico", "ferritina", "b12", "vitamina_d", "psa"}
    assert required <= set(lay)
    numeric = [a for a in knowledge.analytes() if not a.qualitative]
    assert len(numeric) >= 30 and all(a.synonyms for a in numeric)
    for key, row in lay.items():
        assert row["o_que_e"] and row["normal"], key


# ---------- antecedente pessoal ----------
def test_condition_with_time_expressions(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(
        client,
        h,
        cid,
        [{"falante": "paciente", "texto": "Tenho hipotireoidismo há cinco anos e asma desde criança.", "t": 1}],
    )
    items = {i["cid"]: i for i in ins["historico"] if i.get("condicao")}
    assert set(items) == {"E03.9", "J45.9"}
    assert items["E03.9"]["inicio_aprox"].startswith("há 5 anos") and items["E03.9"]["ano"] == clock.now().year - 5
    assert items["J45.9"]["inicio_aprox"] == "desde a infância"
    assert all(i["refs"] == ["Fala/1"] and i["conciliacao"] == "relatado_nao_registrado" for i in items.values())
    assert "não consta no prontuário" in items["E03.9"]["texto"]


def test_condition_is_reconciled_with_condition_onset(record):
    year = clock.now().year
    e = engine_for(
        record,
        [
            ("paciente", f"Tenho pressão alta desde {year - 14} e diabetes."),  # cen6: HAS desde -5110 d (≈ 14 anos)
            ("paciente", f"Tenho dislipidemia desde {year - 1}."),  # cen6: desde ≈ 8 anos atrás
        ],
    )
    ins = e.run()
    by_cid = {h.cid: h for h in relatado(ins, "condicao")}
    assert by_cid["I10"].conciliacao == "prontuario" and "Condition/cen6-dx-1" in by_cid["I10"].refs
    assert by_cid["E11.9"].conciliacao == "prontuario"
    assert by_cid["E78.5"].conciliacao == "diverge" and "diverge" in by_cid["E78.5"].observacao
    # o item do prontuário que o relato cobre sai da lista (sem duplicar), o que não foi dito continua
    chart_refs = [r for h in ins.historico for r in h.refs if r.startswith("Condition/")]
    assert chart_refs.count("Condition/cen6-dx-1") == 1
    assert "Condition/cen6-dx-2" in chart_refs  # DRC não foi dita


@pytest.mark.parametrize(
    "who, text",
    [
        ("medico", "A senhora tem diabetes? Tem pressão alta?"),
        ("paciente", "Minha mãe tem diabetes e meu pai teve infarto."),
        ("paciente", "Eu não tenho asma nem hipertensão."),
        ("paciente", "Tenho medo de ter diabetes como a minha mãe."),
        ("paciente", "Será que eu tenho depressão, doutora?"),
        ("paciente", "Tive dengue ano passado e covid."),
        ("paciente", "Hoje minha pressão tá alta, 15 por 9."),
        ("paciente", "Se eu tiver asma, o que eu faço?"),
        ("medico", "Isso pode ser hipotireoidismo, vamos pedir o TSH."),
        ("paciente", "Tomo remédio para a tireoide, mas não sei o nome."),
        ("paciente", "Fiquei com muita ansiedade ontem à noite"),  # sintoma de hoje, não o transtorno
    ],
)
def test_conditions_not_said_by_the_patient_do_not_create_history(record, who, text):
    """Pergunta do médico, antecedente de parente, negativa, dúvida e hipótese não viram antecedente do paciente."""
    ins = engine_for(record, [(who, text)], pid="cen1").run()
    extracted = [h for h in relatado(ins, "condicao")]
    # "ansiedade" sozinha na fala corrida é o termo do léxico: aceitável apenas se afirmado; aqui só a última pode sair
    if "ansiedade" in text:
        return
    assert extracted == [], [h.texto for h in extracted]


def test_acompanhante_speaking_about_the_patient_counts_but_about_himself_does_not(record):
    ins = engine_for(
        record, [("acompanhante", "Ela tem asma desde pequena."), ("acompanhante", "Eu tenho diabetes.")], pid="cen1"
    ).run()
    assert [h.cid for h in relatado(ins, "condicao")] == ["J45.9"]


# ---------- cirurgia prévia ----------
def test_surgeries_with_year_and_reconciliation(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, [{"falante": "paciente", "texto": "Tirei a vesícula em 2015 e fiz cesárea.", "t": 1}])
    surgeries = [i for i in ins["historico"] if i.get("procedimento")]
    assert len(surgeries) == 2
    assert surgeries[0]["procedimento"].startswith("Colecistectomia") and surgeries[0]["ano"] == 2015
    assert surgeries[1]["procedimento"] == "Cesariana" and surgeries[1]["ano"] is None
    # cen6 tem as duas no prontuário (Procedure): selo "no prontuário"
    assert all(s["conciliacao"] == "prontuario" for s in surgeries)
    assert all("no prontuário" in s["texto"] for s in surgeries)
    assert surgeries[0]["refs"][0] == "Fala/1" and any(r.startswith("Procedure/") for r in surgeries[0]["refs"])
    # a Procedure que o relato cobre não aparece duas vezes no histórico
    refs = [r for i in ins["historico"] for r in i["refs"] if r.startswith("Procedure/")]
    assert len(refs) == len(set(refs)) == 2
    # a aba Cirurgia também mostra as duas, estruturadas
    texts = [c["texto"] for c in ins["cirurgia"] if c["tipo"] == "historico"]
    assert any("Colecistectomia" in t and "2015" in t for t in texts) and any("Cesariana" in t for t in texts)


def test_surgery_not_in_the_chart_is_only_reported_and_year_mismatch_diverges(record):
    year = clock.now().year
    ins = engine_for(
        record,
        [
            ("paciente", "Fui operada do apêndice quando tinha 12 anos."),
            ("paciente", f"Fiz cesárea em {year - 2}, a última."),  # cen6: cesariana ≈ 26 anos atrás
        ],
    ).run()
    got = {h.procedimento.split(" (")[0]: h for h in relatado(ins, "procedimento")}
    assert got["Apendicectomia"].conciliacao == "relatado_nao_registrado"
    assert got["Cesariana"].conciliacao == "diverge" and str(year - 2) in got["Cesariana"].observacao


def test_surgery_complication_is_captured_and_negation_is_respected(record):
    ins = engine_for(
        record,
        [
            ("paciente", "Tirei a vesícula em 2015, mas sangrei muito."),
            ("paciente", "Operei a hérnia em 2010 sem complicação."),
            ("paciente", "Coloquei um stent em 2019 e tive reação à anestesia."),
        ],
        pid="cen1",
    ).run()
    got = {h.procedimento.split(" (")[0]: h for h in relatado(ins, "procedimento")}
    assert got["Colecistectomia"].intercorrencia == "sangramento"
    assert got["Herniorrafia"].intercorrencia == ""
    assert "anestesia" in got["Angioplastia com stent"].intercorrencia


@pytest.mark.parametrize(
    "who, text",
    [
        ("medico", "Tirou a vesícula quando?"),
        ("paciente", "Vou operar a vesícula mês que vem."),
        ("paciente", "O médico quer fazer cirurgia de varizes."),
        ("paciente", "Nunca fui operada de nada."),
        ("paciente", "Minha mãe tirou a vesícula em 2015."),
        ("medico", "A senhora já fez cesárea?"),
        ("paciente", "Será que preciso operar a catarata?"),
    ],
)
def test_surgery_questions_future_and_relatives_are_not_history(record, who, text):
    ins = engine_for(record, [(who, text)], pid="cen1").run()
    assert relatado(ins, "procedimento") == [], text


def test_operated_condition_is_a_surgery_not_a_current_condition(record):
    ins = engine_for(record, [("paciente", "Operei a catarata no ano passado.")], pid="cen1").run()
    assert [h.procedimento for h in relatado(ins, "procedimento")] == ["Cirurgia de catarata"]
    assert relatado(ins, "condicao") == []


# ---------- exame relatado ----------
def test_reported_exams_are_compared_with_the_fhir_series(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, [{"falante": "paciente", "texto": "Meu colesterol deu 250 e o TSH deu 8.", "t": 1}])
    reported = [e for e in ins["exames"] if e["origem_dado"].startswith("relatado")]
    assert {e["chave"] for e in reported} == {"colesterol_total", "tsh"}
    chol = next(e for e in reported if e["chave"] == "colesterol_total")
    tsh = next(e for e in reported if e["chave"] == "tsh")
    assert chol["origem_dado"] == "relatado (fala 1)" and chol["valor"] == "250 mg/dL" and chol["status"] == "alto"
    assert tsh["valor"].startswith("8,0") and tsh["status"] == "alto"
    for e in (chol, tsh):  # cen6 tem as duas séries: a comparação cita o último resultado e a Observation
        assert e["conciliacao"] == "diverge" and "do último do prontuário" in e["comparacao"]
        assert "Fala/1" in e["refs"] and any(r.startswith("Observation/") for r in e["refs"])
    # o exame do prontuário continua na lista, com a origem do prontuário
    assert any(e["origem_dado"] == "prontuário" for e in ins["exames"])


def test_reported_exam_consistent_with_the_chart_and_without_series(record):
    ins = engine_for(
        record,
        [
            ("paciente", "Minha glicada tava 7,3 e o potássio de 5,5."),
            ("paciente", "Fiz o PSA e a vitamina D deu 18."),
            ("paciente", "A ferritina está 12, o ácido úrico 8,2 e a TGP 90."),
        ],
    ).run()
    got = {e.chave: e for e in ins.exames if e.origem_dado != "prontuário"}
    assert got["hba1c"].conciliacao == "prontuario" and got["potassio"].conciliacao == "prontuario"  # 7,1 e 5,3 no cen6
    assert got["vitamina_d"].conciliacao == "relatado_nao_registrado" and got["vitamina_d"].status == "baixo"
    assert "sem série" in got["vitamina_d"].comparacao
    assert got["ferritina"].status == "baixo" and got["acido_urico"].status == "alto" and got["tgp"].status == "alto"
    assert "psa" not in got  # sem número, não há resultado


@pytest.mark.parametrize(
    "who, text",
    [
        ("medico", "Sua glicemia deu 64 hoje."),
        ("paciente", "Fiz exame de colesterol semana passada."),
        ("paciente", "Tomo 2 comprimidos de manhã."),
        ("paciente", "Tsh pedi 3 vezes e nada."),
        ("paciente", "Meu colesterol não deu 250, deu menos."),
        ("medico", "Seu colesterol deu quanto?"),
    ],
)
def test_exam_mentions_without_a_patient_result_are_ignored(record, who, text):
    ins = engine_for(record, [(who, text)], pid="cen1").run()
    assert [e for e in ins.exames if e.origem_dado != "prontuário"] == [], text


# ---------- RCRI ----------
def test_rcri_for_the_planned_cataract_in_cen6_with_points_and_sources(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    rcri = ins["rcri"]
    assert rcri and rcri["pontos"] == 0 and rcri["classe"] == "I" and rcri["risco_pct"] == 3.9
    assert "catarata" in rcri["cirurgia"].lower()
    assert len(rcri["itens"]) == 6 and all("pontos" in i and i["evidencia"] for i in rcri["itens"])
    by = {i["criterio"].split(" (")[0]: i for i in rcri["itens"]}
    creat = by["Creatinina acima de 2,0 mg/dL"]
    assert creat["pontos"] == 0 and creat["refs"] and creat["refs"][0].startswith("Observation/")
    diabetes = by["Diabetes tratado com insulina"]
    assert diabetes["pontos"] == 0 and any(r.startswith("Condition/") for r in diabetes["refs"])
    assert any(r.startswith("MedicationRequest/") for r in diabetes["refs"])  # glibenclamida: sem insulina
    assert "Lee" in rcri["fonte"] and rcri["refs"]
    # a aba Cirurgia mostra o resumo do risco
    assert any(c["tipo"] == "checagem" and c["texto"] == rcri["interpretacao"] for c in ins["cirurgia"])


def test_rcri_counts_points_from_chart_and_conversation(record):
    ins = engine_for(
        record,
        [
            ("paciente", "Já tive infarto, sabe."),
            ("paciente", "Agora vou ser operada do intestino."),
        ],
    ).run()
    rcri = ins.rcri
    assert rcri and rcri.pontos == 2 and rcri.classe == "III" and rcri.risco_pct == 10.1
    points = {i.criterio.split(" (")[0]: i for i in rcri.itens}
    assert points["Cirurgia de alto risco"].pontos == 1 and "Fala/2" in points["Cirurgia de alto risco"].refs
    ischemic = points["Cardiopatia isquêmica"]
    assert ischemic.pontos == 1 and ischemic.refs == ["Fala/1"]


def test_rcri_needs_a_planned_surgery_and_flags_missing_creatinine(record):
    assert engine_for(record, [("paciente", "Estou com dor de cabeça.")], pid="cen1").run().rcri is None
    ins = engine_for(record, [("paciente", "O cirurgião vai me operar da vesícula.")], pid="cen4").run()
    creat = next(i for i in ins.rcri.itens if i.criterio.startswith("Creatinina")) if ins.rcri else None
    assert ins.rcri and creat is not None
    assert (creat.sem_dado and creat.pontos == 0) or creat.refs


def test_extract_module_is_pure_on_text_and_does_not_need_ai(record):
    ex = extract.extract_all(
        [Segment(n=1, t=0, falante="paciente", texto="Tenho gastrite desde 2019 e fiz bariátrica em 2018.")],
        engine_for(record, []).chart,
    )
    assert [a.cid for a in ex.antecedentes] == ["K29.7"] and ex.antecedentes[0].ano == 2019
    assert [c.procedimento for c in ex.cirurgias] == ["Cirurgia bariátrica"] and ex.cirurgias[0].ano == 2018
