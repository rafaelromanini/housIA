"""#18 — Codificação: CID secundário só se citado, especificidade, sexo e idade, CIAP-2 e procedimento SIGTAP.

Cada critério de aceite passa pelo caminho real (API/TestClient e Engine sobre o cen6/cen4, LLM mock; cliente falso onde
o critério é sobre a saída da IA). As tabelas curadas têm teste de esquema.
"""

import csv
from types import SimpleNamespace

import pytest

from app.consult import ai as consult_ai
from app.consult import coding, knowledge
from app.consult import service as consult_service
from app.consult.export import bundle_transaction, encounter_diagnosis_patches
from app.consult.schemas import CidSuggestion, ConsultNote, Segment, SurgicalReport
from app.discharge.scoring import charlson_index
from app.fhir_client.client import LocalFhirStore
from app.llm.client import LLMResult
from app.llm.pseudonymize import Pseudonymizer
from app.reference import cid10, icd_entry
from tests.test_consult import SCRIPT
from tests.test_consult_cid_medicamentos import cen6_engine
from tests.test_consult_modelos_nota import finish
from tests.test_surgical import dictate, report_of
from tests.test_surgical import start as start_surgery

FALAS_CEN6 = [(w["falante"], w["texto"]) for w in SCRIPT]


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def seg(n: int, who: str, texto: str) -> Segment:
    return Segment(n=n, t=float(n), falante=who, texto=texto)  # type: ignore[arg-type]


def read(name: str) -> list[dict[str, str]]:
    with open(knowledge.get_settings().data_dir / "reference" / name, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def fake_structured(output: dict):
    def fake(name, variables, schema, mock=None, effort=None, **kw):
        return LLMResult(output=schema.model_validate(output), model="fake", prompt_version="x", mode="real")

    return fake


def run_note_with(e, ins, cids, monkeypatch, falas_valid=True):
    note = consult_ai.mock_note(e, ins, "pt-BR")
    note["cids"] = cids
    monkeypatch.setattr(consult_ai, "generate_structured", fake_structured(note))
    valid = {f"Fala/{s.n}" for s in e.segments} | {r for c in cids for r in c["refs"]}
    result, _, dropped = consult_ai.run_note(e, ins, Pseudonymizer(), valid, "pt-BR")
    assert isinstance(result, ConsultNote)
    return result, dropped


# ---------- critério 1: secundário só se citado ou se muda a conduta ----------
def test_cen6_mock_note_does_not_list_uncited_comorbidities_as_secondary(client, auth):
    h = auth("medico")
    note = finish(client, h)["nota"]
    secondary = {c["codigo"]: c for c in note["cids"] if c["tipo"] == "secundario"}
    # o prontuário do cen6 tem 7 diagnósticos ativos; dislipidemia, obesidade e catarata não entraram na conversa
    # nem mexem na conduta: ficam só no prontuário
    assert not {"E78.5", "E66.9", "H25.9"} & set(secondary)
    assert {"E11.9", "I10", "N18.3"} <= set(secondary)  # os que sustentam alerta, protocolo ou hipótese
    for c in secondary.values():
        assert c["refs"] and c["frase"]  # cada código mostra por que está ali
    ids = {c["id"] for c in [{"id": x} for x in ("cen6-dx-3", "cen6-dx-4", "cen6-dx-6")]}
    assert not any(any(r.endswith(i) for i in ids) for c in note["cids"] for r in c["refs"])


def test_secondary_cited_in_speech_is_kept_with_the_literal_sentence(record):
    falas = [
        ("medico", "Como está o colesterol?"),
        ("paciente", "Eu tenho dislipidemia há anos, doutor."),
        ("medico", "Certo, vamos manter a conduta."),
    ]
    e = cen6_engine(record, falas)
    ins = e.run()
    sec = coding.secondaries(e.chart, ins, e.segments, {h.cid[:3] for h in ins.hipoteses})
    dislipidemia = next(s for s in sec if s.codigo == "E78.5")
    assert dislipidemia.refs[0] == "Fala/2" and dislipidemia.frase == "Eu tenho dislipidemia há anos, doutor."


def test_negated_or_asked_comorbidity_is_not_cited(record):
    falas = [("medico", "Você tem dislipidemia?"), ("paciente", "Não tenho dislipidemia não."), ("medico", "Tudo bem.")]
    e = cen6_engine(record, falas)
    sec = coding.secondaries(e.chart, e.run(), e.segments, set())
    assert "E78.5" not in {s.codigo for s in sec}


def test_ai_secondary_without_speech_or_conduct_is_dropped(record, monkeypatch):
    e = cen6_engine(record, FALAS_CEN6)
    ins = e.run()
    cids = [
        {"codigo": "E16.0", "descricao": "Hipoglicemia", "tipo": "hipotese", "refs": ["Fala/7"]},
        {"codigo": "E66.9", "descricao": "Obesidade", "tipo": "secundario", "refs": ["Condition/cen6-dx-4"]},
        {"codigo": "I10", "descricao": "Hipertensão", "tipo": "secundario", "refs": ["Condition/cen6-dx-1"]},
    ]
    result, dropped = run_note_with(e, ins, cids, monkeypatch)
    assert {c.codigo for c in result.cids} == {"E16.0", "I10"}
    assert any(d.get("cid") == "E66.9" and d["motivo"] == coding.SECONDARY_DROP for d in dropped)


# ---------- critério 2: especificidade (K35.8) ----------
def test_appendicitis_without_perforation_suggests_k35_8_with_the_phrase(record, monkeypatch):
    falas = [
        ("medico", "O ultrassom mostra apendicite sem perfuração, sem sinais de peritonite."),
        ("paciente", "Estou com muita dor na barriga."),
    ]
    e = cen6_engine(record, falas)
    ins = e.run()
    cids = [{"codigo": "K35.9", "descricao": "Apendicite aguda", "tipo": "hipotese", "refs": ["Fala/1"]}]
    result, _ = run_note_with(e, ins, cids, monkeypatch)
    cid = result.cids[0]
    assert [c.codigo for c in cid.mais_especificos] == ["K35.8"]
    assert cid.mais_especificos[0].frase.startswith("O ultrassom mostra apendicite sem perfuração")
    assert cid.mais_especificos[0].refs == ["Fala/1"] and cid.lacunas == []
    assert cid.frase.startswith("O ultrassom")
    assert icd_entry("K35.8") is not None  # existe na tabela (revisões da OMS)


def test_unclosed_specificity_is_listed_as_gap_and_negation_or_question_do_not_refine():
    nada = [seg(1, "medico", "Suspeito de apendicite aguda.")]
    found, gaps = coding.refine("K35.9", nada)
    assert found == [] and "perfuração" in gaps[0]
    pergunta = [seg(1, "medico", "Tem perfuração? Sem perfuração ou com abscesso?")]
    assert coding.refine("K35.9", pergunta)[0] == []
    negado = [seg(1, "medico", "Não há peritonite generalizada.")]
    assert coding.refine("K35.9", negado)[0] == []
    com = [seg(1, "medico", "Apendicite aguda com peritonite generalizada.")]
    assert [c.codigo for c in coding.refine("K35.9", com)[0]] == ["K35.2"]


def test_other_specificity_dimensions():
    dm = coding.refine("E11.9", [seg(1, "paciente", "Tenho neuropatia, formigamento nos pés.")])[0]
    assert [c.codigo for c in dm] == ["E11.4"]
    joelho = coding.refine("M17.9", [seg(1, "medico", "Artrose nos dois joelhos.")])[0]
    assert [c.codigo for c in joelho] == ["M17.0"]
    unilateral = coding.refine("M17.9", [seg(1, "medico", "Artrose no joelho direito.")])[0]
    assert [c.codigo for c in unilateral] == ["M17.1"]
    assert coding.refine("M17.9", [seg(1, "medico", "Joelho dói.")]) == (
        [],
        ["Confirmar a lateralidade (direito; esquerdo ou bilateral)"],
    )
    cronica = coding.refine("K81.9", [seg(1, "medico", "Quadro de colecistite crônica.")])[0]
    assert [c.codigo for c in cronica] == ["K81.1"]
    # fala sem sentido clínico, de outro assunto: nada muda
    assert coding.refine("K35.9", [seg(1, "paciente", "Bom dia, doutor, tudo bem?")])[0] == []


# ---------- critério 3: sexo e idade ----------
def test_male_only_cid_in_female_patient_raises_an_alert(record):
    e = cen6_engine(record, FALAS_CEN6)
    assert e.r.sex == "F"
    e.r.resources["Condition"].append(
        {
            "resourceType": "Condition",
            "id": "cen6-dx-x",
            "clinicalStatus": {
                "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]
            },
            "code": {
                "coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "C61"}],
                "text": "Neoplasia da próstata",
            },
        }
    )
    ins = e.run()
    alert = next(a for a in ins.alertas if a.categoria == "codificacao")
    assert (
        "C61" in alert.texto and "restrito ao sexo masculino" in alert.texto and alert.refs == ["Condition/cen6-dx-x"]
    )


def test_incompatible_cid_in_the_final_note_becomes_a_pendency(record, monkeypatch):
    e = cen6_engine(record, FALAS_CEN6)
    ins = e.run()
    cids = [{"codigo": "C61", "descricao": "Neoplasia da próstata", "tipo": "secundario", "refs": ["Fala/4"]}]
    e.chart.icd_codes  # noqa: B018 - cache
    monkeypatch.setattr(
        type(e.chart), "icd_codes", property(lambda self: frozenset({"C61"}))
    )  # registrado no prontuário
    result, _ = run_note_with(e, ins, cids, monkeypatch)
    assert any("restrito ao sexo masculino" in p.texto for p in result.pendencias)


def test_age_ranges_and_sex_table():
    assert coding.compat_problem("P07.0", "M", 40) and "faixa etária" in coding.compat_problem("P07.0", "M", 40)
    assert coding.compat_problem("P07.0", "M", 0.1) is None
    assert coding.compat_problem("O80", "F", 8)
    assert coding.compat_problem("O80", "F", 28) is None
    assert coding.compat_problem("N40", "F", 60) == "CID restrito ao sexo masculino"
    assert coding.compat_problem("N40", "", None) is None  # sexo desconhecido não acusa


# ---------- critério 4: CIAP-2 na APS ----------
def test_aps_consultation_suggests_mapped_ciap2(client, auth):
    h = auth("medico")
    done = finish(client, h)
    assert all(not c["ciap2"] for c in done["nota"]["cids"])  # fora da APS não há CIAP-2
    trocada = client.put(f"/consultations/{done['id']}/note-template", json={"modelo": "aps_esus"}, headers=h)
    assert trocada.status_code == 200, trocada.text
    by_code = {c["codigo"]: c["ciap2"] for c in trocada.json()["nota"]["cids"]}
    assert by_code["I10"].startswith("K86 ") and by_code["E11.9"].startswith("T90 ")
    assert by_code["E16.0"] == ""  # hipótese sem correspondência na tabela não ganha código inventado


def test_aps_template_selected_up_front_enriches_the_note(record, monkeypatch):
    e = cen6_engine(record, FALAS_CEN6, specialty="clinica_medica")
    ins = e.run()
    monkeypatch.setattr(
        consult_ai.note_templates, "select", lambda kind, spec: consult_ai.note_templates.get("aps_esus")
    )
    cids = [
        {"codigo": "I10", "descricao": "Hipertensão", "tipo": "secundario", "refs": ["Fala/2", "Condition/cen6-dx-1"]}
    ]
    result, _ = run_note_with(e, ins, cids, monkeypatch)
    assert result.cids[0].ciap2 == "K86 Hipertensão sem complicações"


# ---------- critério 5: SIGTAP ----------
def test_sigtap_procedure_is_suggested_and_incompatible_cid_makes_a_pendency(client, auth):
    h = auth("medico")
    cid = start_surgery(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    ins = dictate(client, h, cid, [("medico", "Procedimento realizado: apendicectomia. Início da cirurgia às 9h.")])
    rep = report_of(ins["insights"])
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    sus = rep["procedimento_sus"]
    assert sus["codigo"] == "DEMO-SIGTAP-001" and sus["descricao"] == "Apendicectomia"
    assert "DEMO" in sus["fonte"] and sus["competencia"] == "DEMO"
    # o cirurgião troca o CID principal por um que não consta na relação do procedimento
    rep["cids"] = [{"codigo": "I10", "descricao": "Hipertensão essencial", "tipo": "principal", "refs": ["Fala/1"]}]
    got = client.post(f"/consultations/{cid}/surgical/recompute", json={"relatorio_cirurgico": rep}, headers=h)
    assert got.status_code == 200, got.text
    out = got.json()["relatorio_cirurgico"]
    assert out["procedimento_sus"]["compativel"] is False
    pend = [p for p in out["pendencias"] if p["categoria"] == "sigtap"]
    assert len(pend) == 1 and "incompatível" in pend[0]["texto"] and "I10" in pend[0]["texto"]
    # CID compatível: a pendência some
    rep["cids"] = [{"codigo": "K35.9", "descricao": "Apendicite aguda", "tipo": "principal", "refs": ["Fala/1"]}]
    ok = client.post(f"/consultations/{cid}/surgical/recompute", json={"relatorio_cirurgico": rep}, headers=h).json()
    assert ok["relatorio_cirurgico"]["procedimento_sus"]["compativel"] is True
    assert not [p for p in ok["relatorio_cirurgico"]["pendencias"] if p["categoria"] == "sigtap"]


def test_sigtap_only_when_the_procedure_is_in_the_table_and_sex_age_are_checked():
    report = SurgicalReport(campos=[], opme=[], pendencias=[], cids=[])
    coding.attach_sigtap(report, "F", 40)
    assert report.procedimento_sus is None  # sem modelo/procedimento: nada inventado
    from app.consult.schemas import TemplateInfo

    report.modelo = TemplateInfo(
        id="rtu_prostata",
        rotulo="RTU",
        especialidade="urologia",
        especialidade_rotulo="Urologia",
        versao="1",
        versao_base="1",
        origem="base",
        refs=[],
    )
    report.cids = [CidSuggestion(codigo="N40", descricao="HPB", tipo="principal", refs=["Fala/1"])]
    coding.attach_sigtap(report, "F", 40)
    assert report.procedimento_sus and not report.procedimento_sus.compativel
    assert "restrito ao sexo masculino" in report.procedimento_sus.motivo
    coding.attach_sigtap(report, "M", 70)
    assert report.procedimento_sus.compativel and not report.pendencias


# ---------- Bundle e Charlson/LACE ----------
def test_confirmed_codes_link_to_the_encounter_and_hypotheses_do_not():
    conf = {"resourceType": "Condition", "verificationStatus": {"coding": [{"code": "confirmed"}]}}
    hyp = {"resourceType": "Condition", "verificationStatus": {"coding": [{"code": "differential"}]}}
    patches = encounter_diagnosis_patches("enc1", [("urn:a", conf), ("urn:b", hyp)])
    assert len(patches) == 1 and patches[0]["request"] == {"method": "PATCH", "url": "Encounter/enc1"}
    assert encounter_diagnosis_patches(None, [("urn:a", conf)]) == []
    bundle = bundle_transaction(
        consultation_id=7,
        kind="consulta",
        patient_id="p1",
        patient_name="Paciente",
        encounter_id="enc1",
        author="Dr. A",
        author_id=1,
        when=__import__("app.clock", fromlist=["now"]).now(),
        text="x",
        note={"cids": [], "modelo": {}},
        interpretacao=None,
        approved=True,
        model="m",
        prompt_version="v",
        accepted=[("condicao:i10", conf | {"code": {"coding": [{"system": "s", "code": "I10"}]}})],
        orders=[],
    )
    assert [e["request"]["method"] for e in bundle["entry"]].count("PATCH") == 1
    store = LocalFhirStore.from_resources([{"resourceType": "Encounter", "id": "enc1", "status": "finished"}])
    for _ in range(2):  # reenviar não duplica
        store.transaction(bundle)
    enc = store.resources["Encounter"]["enc1"]
    assert len(enc["diagnosis"]) == 1 and enc["diagnosis"][0]["condition"]["reference"].startswith("Condition/")
    assert enc["diagnosis"][0]["rank"] == 1


def test_charlson_counts_confirmed_conditions_but_not_hypotheses():
    def cond(i, code, status):
        return {
            "id": i,
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": code}]},
            "verificationStatus": {
                "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": status}]
            },
        }

    record = SimpleNamespace(
        resources={"Condition": [cond("a", "I50.0", "confirmed"), cond("b", "C34.9", "differential")]}
    )
    score, items = charlson_index(record)  # type: ignore[arg-type]
    assert [i["cid10"] for i in items] == ["I50.0"] and score >= 1


# ---------- tabelas ----------
def test_tables_are_consistent_with_cid10_and_labelled():
    ciap = {r["codigo"] for r in read("ciap2.csv")}
    assert len(ciap) >= 50 and all(r["fonte"] for r in read("ciap2.csv"))
    prefixes = [r for r in read("ciap2_cid10_map.csv")]
    assert {r["ciap2"] for r in prefixes} <= ciap
    assert all(r["fonte"] and any(c.startswith(r["cid_prefixo"]) for c in cid10()) for r in prefixes)
    for r in read("cid_especificidade.csv"):
        assert icd_entry(r["codigo"]) is not None and r["codigo"][0] == r["cid_prefixo"][0] and r["fonte"]
    procs = {r["chave"]: r for r in read("sigtap_procedimentos.csv")}
    assert all(r["codigo"].startswith("DEMO-") for r in procs.values())  # nenhum código oficial inventado
    glosa = {r["procedimento"] for r in read("glosa_rules/tabela_procedimentos.csv")}
    assert set(procs) == glosa
    for r in read("sigtap_proc_cid.csv"):
        assert r["codigo"] in {p["codigo"] for p in procs.values()} and r["fonte"]
    assert coding.ciap2_for("I10")["ciap2"] == "K86" and coding.ciap2_for("M54.5")["ciap2"] == "L03"
    assert coding.ciap2_for("Z99.9") is None


# ---------- adversariais ----------
@pytest.mark.parametrize(
    "who,fala",
    [
        ("paciente", "Minha mãe teve apendicite sem perfuração."),
        ("paciente", "Meu pai tem insuficiência cardíaca e pressão alta."),
        ("medico", "Será que é apendicite sem perfuração?"),
        ("paciente", "Se for diabetes com neuropatia eu não quero saber."),
        ("paciente", "Nunca tive nefropatia nem retinopatia."),
        ("paciente", "Sem complicacoes de diabetes eu nao sei dizer"),
    ],
)
def test_adversarial_utterances_do_not_refine_or_cite(who, fala):
    segs = [seg(1, who, fala)]
    for code in ("K35.9", "I10", "E11.9"):
        found, _ = coding.refine(code, segs)
        # "sem complicações" repete o E11.9 (não refina) e a negação não vira complicação
        assert not [c for c in found if c.codigo not in ("E11.9",)], (fala, code, found)


def test_refinement_works_without_accents_or_punctuation():
    found = coding.refine("K35.9", [seg(1, "medico", "apendicite sem perfuracao")])[0]
    assert [c.codigo for c in found] == ["K35.8"]


def test_family_history_is_not_a_cited_comorbidity(record):
    falas = [("paciente", "Minha mãe tinha dislipidemia."), ("medico", "Entendi.")]
    e = cen6_engine(record, falas)
    sec = coding.secondaries(e.chart, e.run(), e.segments, set())
    assert "E78.5" not in {s.codigo for s in sec}


@pytest.mark.parametrize(
    "code,who,fala,esperado",
    [
        ("K35.9", "paciente", "dor forte mas sem abscesso e com peritonite generalizada", ["K35.2"]),
        ("K35.9", "medico", "Tem sinal de abscesso? Não. Então é apendicite sem perfuração.", ["K35.8"]),
        ("E11.9", "paciente", "diabetes sem complicações mas tenho albuminuria", ["E11.2"]),
        ("E11.9", "medico", "não há sinais de nefropatia diabética", []),
        ("E11.9", "paciente", "diabetes sem retinopatia e sem nefropatia", []),
        ("M17", "paciente", "nao dói nos dois joelhos", []),
        ("M17", "paciente", "dor nos dois joelhos", ["M17.0"]),
        ("K81", "medico", "colecistite aguda descartada", []),
        ("K81", "paciente", "vesícula com colecistite cronica", ["K81.1"]),
        ("I10", "paciente", "meu marido tem insuficiencia cardiaca, eu tenho pressao alta", []),
    ],
)
def test_adversarial_refinement_priorities_and_denials(code, who, fala, esperado):
    found, _ = coding.refine(code, [seg(1, who, fala)])
    assert [c.codigo for c in found] == esperado, (fala, found)
