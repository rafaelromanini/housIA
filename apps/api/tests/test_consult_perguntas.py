"""Perguntas sugeridas que acompanham qualquer diálogo (#4) e anamnese por queixa com HDA redigida (#9).

Critérios de aceite exercitados pelo caminho real (API/TestClient sobre o cen6, LLM mock) e, para idade e sexo, pelo
Engine sobre cópias do prontuário. As bases curadas (sintomas, perguntas, semiologia) têm testes de esquema.
"""

import csv
import json
import re
from copy import deepcopy

import pytest

from app import clock
from app.consult import knowledge
from app.consult import service as consult_service
from app.consult.engine import Engine
from app.consult.schemas import CampoSemio, Insights, Segment
from app.llm import client as llm_client
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from tests.test_consult import FakeClient, start

SYMPTOM_KEYS = {d.key for d in knowledge.symptoms()}
PEDIATRIC = {
    "febre_crianca",
    "vomitos_crianca",
    "diarreia",
    "recusa_alimentar",
    "chiado",
    "irritabilidade",
    "manchas",
}


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def say(client, h, cid, *falas: tuple[str | None, str]) -> dict:
    """Envia falas (falante, texto); falante None = o botão Automático (a heurística decide)."""
    segs = [{"texto": t} | ({"falante": who} if who else {}) for who, t in falas]
    res = client.post(f"/consultations/{cid}/segments", json={"segmentos": segs}, headers=h)
    assert res.status_code == 200, res.text
    return res.json()["insights"]


def row_of(question: dict) -> knowledge.QuestionDef:
    """A linha de consult_questions.csv de uma pergunta devolvida pela API (o id vem como `qNNN` ou `qNNN:chave`)."""
    if question["id"].startswith("protocolo:"):  # pergunta de protocolo (#28): vem do YAML, sem linha no CSV
        return knowledge.QuestionDef(question["id"], "protocolo:*", question["pergunta"], "", (), "media", "", "")
    return next(q for q in knowledge.questions() if q.id == question["id"].split(":")[0])


def child_engine(record, specialty: str, falas: list[tuple[str, str]], birth: str, gender: str = "male") -> Engine:
    """Engine sobre uma cópia do prontuário do cen6 com outra idade e sexo (o prontuário da pessoa não muda)."""
    rec = record("cen6")
    rec.patient = deepcopy(rec.patient) | {"birthDate": birth, "gender": gender}
    segs = [Segment(n=i, t=float(i), falante=who, texto=t) for i, (who, t) in enumerate(falas, 1)]  # type: ignore[arg-type]
    return Engine(rec, build_timeline(rec), build_context(rec), segs, specialty, "consulta", clock.now())


# ---------- bases curadas: esquema ----------
def test_symptom_base_has_60_symptoms_including_pediatric_ones():
    assert len(knowledge.symptoms()) >= 60
    assert PEDIATRIC <= SYMPTOM_KEYS
    for d in knowledge.symptoms():
        assert d.words and all(w == w.lower() for w in d.words), d.key
        assert d.sex in ("", "F", "M"), d.key
        if d.age_min is not None and d.age_max is not None:
            assert d.age_min <= d.age_max, d.key
        assert not d.group or d.group in SYMPTOM_KEYS, d.key
    pediatric_fever = next(d for d in knowledge.symptoms() if d.key == "febre_crianca")
    assert (
        pediatric_fever.age_max is not None
        and next(d for d in knowledge.symptoms() if d.key == "febre").age_min == pediatric_fever.age_max + 1
    )


def test_every_question_row_has_a_source_and_every_symptom_has_two_questions():
    path = knowledge.get_settings().data_dir / "reference" / "consult_questions.csv"
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert {"id", "fonte", "idade_min", "idade_max", "sexo", "discrimina"} <= set(rows[0])
    assert all(r["fonte"].strip() for r in rows), [r["id"] for r in rows if not r["fonte"].strip()]
    assert len({r["id"] for r in rows}) == len(rows)
    per_symptom = {k: 0 for k in SYMPTOM_KEYS}
    for q in knowledge.questions():
        if q.trigger.startswith("sintoma:"):
            per_symptom[q.trigger.split(":", 1)[1]] += 1
    assert all(n >= 2 for n in per_symptom.values()), {k: n for k, n in per_symptom.items() if n < 2}
    assert sum(q.specialty == "clinica_medica" for q in knowledge.questions()) >= 5
    assert sum(q.specialty == "pediatria" for q in knowledge.questions()) >= 8
    # as perguntas de pediatria da AIDPI valem só para criança; as de sexo e idade apontam para faixas coerentes
    for q in knowledge.questions():
        assert q.sex in ("", "F", "M"), q.id
        if q.age_min is not None and q.age_max is not None:
            assert q.age_min <= q.age_max, q.id
        if q.specialty == "pediatria":
            assert q.age_max is not None and q.age_max <= 17, q.id


def test_question_rows_are_consistent_with_the_other_bases():
    cids = {d.icd for d in knowledge.differentials()}
    fields = set(knowledge.semiology().fields)
    for q in knowledge.questions():
        assert set(q.discriminates) <= cids, q.id
        assert re.fullmatch(r"q\d{3}|pk_[a-z]+_\d{2}", q.id)  # CSV ou pacote de especialidade (#27)
        text = q.text.format(trat="a senhora", med="X", alergeno="Y", reacao="Z", queixa="tontura")
        assert "{" not in text
        kind, _, key = q.trigger.partition(":")
        if kind == "sintoma":
            assert key in SYMPTOM_KEYS, q.id
        if kind == "semiologia" and key in fields:
            assert "{queixa}" in q.text, q.id
        assert all(w == w.lower() and w for w in q.asked_words), q.id


def test_semiology_data_is_consistent():
    sem = knowledge.semiology()
    assert sem.source and all(g.source for g in sem.groups)
    assert {"inicio_duracao", "localizacao", "intensidade", "piora", "melhora", "frequencia", "associados"} == set(
        sem.fields
    )
    listed = [s for g in sem.groups for s in g.symptoms]
    assert set(listed) <= SYMPTOM_KEYS and len(listed) == len(set(listed))
    assert all(set(g.fields) <= set(sem.fields) for g in sem.groups)
    assert sem.group_of("tontura").id == "geral" and sem.group_of("dor_joelho").id == "dor"
    # cada campo semiológico tem a sua pergunta ("semiologia:<campo>") e a especialidade, as dela
    rows = knowledge.semiology_questions()
    assert set(rows.values()) == set(sem.fields)
    extras = {f.key for fields in sem.specialty_fields.values() for f in fields}
    assert extras == {"gestacao_parto", "vacinas", "dnpm"}
    asked = {q.trigger.split(":", 1)[1] for q in knowledge.questions() if q.trigger.startswith("semiologia:")}
    assert extras <= asked
    assert all(f.source for fields in sem.specialty_fields.values() for f in fields)


# ---------- #4 critério 1: queixa em mock vira perguntas sobre a queixa ----------
@pytest.mark.parametrize("who", ["paciente", None], ids=["falante-informado", "automatico"])
def test_febre_tosse_falta_de_ar_gera_perguntas_da_queixa_no_top_8(client, auth, who):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, (who, "Febre e tosse há três dias e falta de ar quando ando"))
    assert {t["rotulo"] for t in ins["queixas"]} == {"Febre", "Tosse", "Falta de ar"}
    top8 = ins["perguntas"][:8]
    about = [q for q in top8 if row_of(q).trigger in ("sintoma:febre", "sintoma:tosse", "sintoma:dispneia")]
    assert len(about) >= 3, [q["pergunta"] for q in top8]
    assert all(q["fonte"] and q["id"] for q in ins["perguntas"])


# ---------- #4 critério 2: só o profissional faz a pergunta "abordada" ----------
def test_paciente_dizer_sono_nao_tira_a_pergunta_sobre_sono(client, auth):
    h = auth("medico")
    cid = start(client, h, pid="cen1", especialidade="geriatria")["id"]
    sono = lambda lista: [q for q in lista if "sono" in q["pergunta"].lower() and "Como estão o sono" in q["pergunta"]]  # noqa: E731
    assert sono(client.get(f"/consultations/{cid}", headers=h).json()["insights"]["perguntas"])
    ins = say(client, h, cid, ("paciente", "Doutora, eu tomo o remédio da pressão antes do sono e o sono é tranquilo"))
    assert sono(ins["perguntas"]) and not sono(ins["perguntas_abordadas"])
    # quando o profissional pergunta, a pergunta sai da lista, com a fala dele e a resposta do paciente
    ins = say(
        client, h, cid, ("medico", "E como estão o sono, o apetite e o ânimo?"), ("paciente", "O apetite está bom.")
    )
    assert not sono(ins["perguntas"])
    done = sono(ins["perguntas_abordadas"])[0]
    assert done["refs"] == ["Fala/2"] and done["resposta_refs"] == ["Fala/3"] and "apetite" in done["resposta"]


def test_fala_do_paciente_sozinha_nunca_aborda_pergunta(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(
        client,
        h,
        cid,
        ("paciente", "Comprei um ibuprofeno na farmácia e tomo todo dia"),
        ("paciente", "Não como direito, só como mais tarde, e sinto tontura"),
    )
    assert not ins["perguntas_abordadas"]
    assert any("anti-inflamatório" in q["pergunta"] for q in ins["perguntas"])
    assert any("Toma a Glibenclamida" in q["pergunta"] for q in ins["perguntas"])


# ---------- #4 critério 3: pergunta parafraseada vira "abordada" com a resposta ----------
@pytest.mark.parametrize("asker", ["medico", None], ids=["falante-informado", "automatico"])
def test_pergunta_parafraseada_do_medico_leva_a_falta_de_ar_para_as_abordadas(client, auth, asker):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("paciente", "Ando muito cansada, doutora"))
    falta_de_ar = lambda lista: [q for q in lista if "falta de ar" in q["pergunta"]]  # noqa: E731
    assert falta_de_ar(ins["perguntas"]) and not falta_de_ar(ins["perguntas_abordadas"])
    # no automático, "o ar tem faltado quando caminha" (3ª pessoa, sem "?") é pergunta do profissional
    ins = say(client, h, cid, (asker, "o ar tem faltado quando caminha"), ("paciente", "Falta sim, quando subo escada"))
    assert [(s["falante"]) for s in client.get(f"/consultations/{cid}", headers=h).json()["segmentos"]][1:] == [
        "medico",
        "paciente",
    ]
    assert not falta_de_ar(ins["perguntas"]) or all(
        row_of(q).trigger != "sintoma:cansaco" for q in falta_de_ar(ins["perguntas"])
    )
    done = [q for q in falta_de_ar(ins["perguntas_abordadas"]) if row_of(q).trigger == "sintoma:cansaco"]
    assert done and done[0]["refs"] == ["Fala/2"]
    assert done[0]["resposta"] == "Falta sim, quando subo escada" and done[0]["resposta_refs"] == ["Fala/3"]
    assert done[0]["fonte"]


def test_pergunta_de_dispneia_parafraseada_com_a_queixa_ja_dita(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("paciente", "Estou com falta de ar, doutora"))
    esforco = [q for q in ins["perguntas"] if row_of(q).trigger == "sintoma:dispneia"]
    assert esforco
    ins = say(
        client, h, cid, ("medico", "O ar falta quando a senhora anda ou sobe escada?"), ("paciente", "Só na escada")
    )
    assert not [q for q in ins["perguntas"] if q["id"] == esforco[0]["id"]]
    done = next(q for q in ins["perguntas_abordadas"] if q["id"] == esforco[0]["id"])
    assert done["resposta"] == "Só na escada"


# ---------- #4 critério 4: idade e sexo ----------
def test_paciente_de_60_anos_em_pediatria_nao_recebe_pergunta_de_crianca(client, auth):
    h = auth("medico")
    c = start(client, h, especialidade="pediatria")
    assert c["insights"]["perguntas"]
    ins = say(client, h, c["id"], ("paciente", "Estou com febre e vomitando desde ontem"))
    shown = ins["perguntas"] + ins["perguntas_abordadas"]
    assert shown and not any(row_of(q).specialty == "pediatria" for q in shown)
    assert not any("criança" in q["pergunta"].lower() for q in shown)
    assert {t["rotulo"] for t in ins["queixas"]} >= {"Febre", "Náusea/vômito"}  # sintoma de adulto, não "na criança"
    assert ins["anamnese"]["campos_especialidade"] == []


def test_crianca_em_pediatria_recebe_as_perguntas_da_aidpi(record):
    falas = [("acompanhante", "Meu filho está com febre há dois dias e vomitou duas vezes")]
    eng = child_engine(record, "pediatria", falas, birth="2022-03-01")
    ins = eng.run()
    assert eng.r.age == 4
    assert {t.rotulo for t in ins.queixas} >= {"Febre na criança", "Vômitos na criança"}
    rows = [
        next(q for q in knowledge.questions() if q.id == p.id.split(":")[0])
        for p in ins.perguntas
        if not p.id.startswith("protocolo:")  # protocolos vivos (#28) vêm do YAML, não do CSV de perguntas
    ]
    assert sum(r.specialty == "pediatria" for r in rows) >= 4
    assert any(r.trigger == "sintoma:febre_crianca" for r in rows)
    assert all("AIDPI" in r.source for r in rows if r.trigger.startswith("sintoma:") and r.age_max == 11)
    assert not any(r.trigger == "sintoma:febre" for r in rows)  # a febre do adulto não vale para criança
    # e nada da lista de adulto que depende de idade: o prontuário do cen6 (diabetes etc.) continua valendo
    assert ins.anamnese and {c.campo for c in ins.anamnese.campos_especialidade} == {
        "gestacao_parto",
        "vacinas",
        "dnpm",
    }


def test_pergunta_e_sintoma_de_um_sexo_so_valem_para_ele(record):
    # atraso menstrual é de mulher em idade fértil; sangramento vaginal, de mulher
    falas = [("paciente", "Minha menstruação está atrasada e estou com sangramento vaginal")]
    mulher = child_engine(record, "clinica_medica", falas, birth="1990-01-10", gender="female").run()
    assert {t.rotulo for t in mulher.queixas} >= {"Atraso menstrual", "Sangramento vaginal"}
    rows = {q.id.split(":")[0] for q in mulher.perguntas}
    sexuais = {q.id for q in knowledge.questions() if q.sex == "F"}
    assert rows & sexuais
    homem = child_engine(record, "clinica_medica", falas, birth="1990-01-10", gender="male").run()
    assert not {t.rotulo for t in homem.queixas} & {"Atraso menstrual", "Sangramento vaginal"}
    assert not {q.id.split(":")[0] for q in homem.perguntas} & sexuais
    # fora da idade (atraso menstrual: 10 a 55 anos): sem sintoma nem pergunta
    idosa = child_engine(record, "clinica_medica", falas, birth="1950-01-10", gender="female").run()
    assert "Atraso menstrual" not in {t.rotulo for t in idosa.queixas}


# ---------- #4 item 7: perguntas que discriminam as 2 hipóteses de maior confiança sobem ----------
def test_perguntas_que_discriminam_as_duas_hipoteses_de_maior_confianca_sobem(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    falas = [
        ("medico", "Bom dia, dona Joana."),
        ("paciente", "Ando muito tonta, doutora, quando levanto da cama tudo escurece"),
        ("paciente", "Antes do almoço me dá um suor frio e uma tremedeira, e um cansaço"),
        ("paciente", "Comprei um ibuprofeno na farmácia e tomo duas vezes por dia"),
    ]
    ins = say(client, h, cid, *falas)
    top2 = [x["hipotese"] for x in ins["hipoteses"][:2]]
    boosted = [q for q in ins["perguntas"] if q["discrimina"]]
    assert boosted and all(set(q["discrimina"]) <= set(top2) for q in boosted)
    assert all(row_of(q).discriminates for q in boosted)
    # dentro da mesma prioridade e origem, as que discriminam vêm antes das que não discriminam
    by_tier: dict[tuple[str, bool], list[bool]] = {}
    for q in ins["perguntas"]:
        if q["id"].split(":")[0] in knowledge.semiology_questions():
            continue
        tier = (q["prioridade"], any(r.startswith("Fala/") for r in q["refs"]))
        by_tier.setdefault(tier, []).append(bool(q["discrimina"]))
    assert any(any(flags) for flags in by_tier.values())


# ---------- #9 critérios: semiologia do cen6 ----------
def test_cen6_tontura_tem_duracao_3_semanas_e_piora_ao_levantar(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, *[(f["falante"], f["texto"]) for f in knowledge.demo_scripts()["cen6_consulta"]["falas"]])
    q = {x["chave"]: x for x in ins["anamnese"]["queixas_detalhadas"]}
    tontura = q["tontura"]
    assert tontura["inicio_duracao"] == {"valor": "3 semanas", "refs": ["Fala/4"]}
    assert tontura["piora"]["valor"] == "ao levantar" and tontura["piora"]["refs"] == ["Fala/4"]
    assert "queda ou quase queda" in tontura["associados"]["valor"]
    assert tontura["intensidade"]["valor"] == "" and "intensidade" in tontura["faltam"]
    # as tags mostram os atributos, e o texto da tag continua sendo o rótulo
    tag = next(t for t in ins["queixas"] if t["rotulo"] == "Tontura")
    assert tag["detalhe"] == "3 sem · ao levantar"
    joelho = q["dor_joelho"]
    assert joelho["localizacao"]["valor"] == "joelho direito" and joelho["piora"]["valor"] == "ao subir escada"
    assert joelho["intensidade"]["valor"] == "forte"
    # a duração do ibuprofeno ("faz umas duas semanas") não vira duração da dor no joelho
    assert joelho["inicio_duracao"]["valor"] == ""
    assert q["nicturia"]["frequencia"]["valor"] == "3 vezes à noite"
    assert ins["anamnese"]["qp_principal"] == "tontura" and ins["anamnese"]["qp_fixada"] is False
    assert ins["anamnese"]["queixa_principal"].startswith("Tontura há 3 semanas")


def test_cada_campo_semiologico_cita_falas_do_paciente_e_a_hda_cita_as_falas(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, *[(f["falante"], f["texto"]) for f in knowledge.demo_scripts()["cen6_consulta"]["falas"]])
    patient = {
        s["n"] for s in client.get(f"/consultations/{cid}", headers=h).json()["segmentos"] if s["falante"] != "medico"
    }
    for q in ins["anamnese"]["queixas_detalhadas"]:
        for f in ("inicio_duracao", "localizacao", "intensidade", "piora", "melhora", "frequencia", "associados"):
            campo = q[f]
            assert bool(campo["valor"]) == bool(campo["refs"]), (q["chave"], f)
            assert {int(r.split("/")[1]) for r in campo["refs"]} <= patient, (q["chave"], f)
    hda = ins["anamnese"]["hda"]
    assert re.search(r"Tontura há 3 semanas, piora ao levantar.*\(falas 2, 4, 5, 7\)", hda)
    assert "“" not in hda and '"' not in hda


# ---------- #9 critério 2: intensidade vazia pergunta; "de 0 a 10" tira e guarda a resposta ----------
def test_intensidade_vazia_gera_pergunta_que_some_quando_o_medico_pergunta_de_0_a_10(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("paciente", "Estou com tontura desde ontem, doutora"))
    intensity = lambda lista: [q for q in lista if "de 0 a 10" in q["pergunta"] and "tontura" in q["pergunta"]]  # noqa: E731
    assert intensity(ins["perguntas"]) and not intensity(ins["perguntas_abordadas"])
    assert row_of(intensity(ins["perguntas"])[0]).trigger == "semiologia:intensidade"
    assert "intensidade" in ins["anamnese"]["queixas_detalhadas"][0]["faltam"]
    ins = say(client, h, cid, ("medico", "De 0 a 10, quanto é essa tontura?"), ("paciente", "Uns sete"))
    assert not intensity(ins["perguntas"])
    done = intensity(ins["perguntas_abordadas"])[0]
    assert done["refs"] == ["Fala/2"] and done["resposta"] == "Uns sete" and done["resposta_refs"] == ["Fala/3"]
    tontura = ins["anamnese"]["queixas_detalhadas"][0]
    assert tontura["intensidade"] == {"valor": "7/10", "refs": ["Fala/3"]}
    assert "intensidade" not in tontura["faltam"] and tontura["inicio_duracao"]["valor"] == "desde ontem"
    tag = next(t for t in ins["queixas"] if t["rotulo"] == "Tontura")
    assert tag["detalhe"] == "desde ontem · 7/10"


def test_pergunta_de_semiologia_nao_confunde_a_queixa_perguntada(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, ("paciente", "Estou com tontura e também com dor de cabeça"))
    ins = say(client, h, cid, ("medico", "A dor de cabeça, de 0 a 10?"), ("paciente", "Oito"))
    by = {q["chave"]: q for q in ins["anamnese"]["queixas_detalhadas"]}
    assert by["cefaleia"]["intensidade"]["valor"] == "8/10"
    assert by["tontura"]["intensidade"]["valor"] == ""  # o 8 era da dor de cabeça


# ---------- #9 critério 3: HDA do modo regras na nota ----------
def test_hda_da_nota_mock_nao_tem_aspas_de_citacao(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, *[(f["falante"], f["texto"]) for f in knowledge.demo_scripts()["cen6_consulta"]["falas"]])
    nota = client.post(f"/consultations/{cid}/finish", headers=h).json()["nota"]
    for secao in ("hda", "queixa_principal"):
        texto = nota[secao]["texto"]
        assert not re.search(r"[“”\"«»]", texto), texto
    assert "há 3 semanas" in nota["hda"]["texto"] and nota["hda"]["refs"]


# ---------- #9 critério 4: teste unitário do extrator com 10 frases ----------
FRASES = [
    # (frase do paciente, queixa, campos esperados)
    ("Faz umas três semanas que estou tonta", "tontura", {"inicio_duracao": "3 semanas"}),
    ("Estou com dor de cabeça desde ontem", "cefaleia", {"inicio_duracao": "desde ontem"}),
    ("A dor de cabeça é uns 7 de 0 a 10", "cefaleia", {"intensidade": "7/10"}),
    (
        "Tenho dor no joelho direito que piora ao subir escada",
        "dor_joelho",
        {"localizacao": "joelho direito", "piora": "ao subir escada"},
    ),
    (
        "Dor no peito forte quando faço esforço, melhora com repouso",
        "dor_toracica",
        {"intensidade": "forte", "piora": "aos esforços", "melhora": "com repouso"},
    ),
    (
        "Tenho tosse todos os dias há duas semanas",
        "tosse",
        {"frequencia": "todos os dias", "inicio_duracao": "2 semanas"},
    ),
    ("Acordo três vezes por semana com falta de ar", "dispneia", {"frequencia": "3 vezes por semana"}),
    ("A tontura começou de repente", "tontura", {"inicio_duracao": "início súbito"}),
    ("Estou com febre e tosse há três dias", "tosse", {"inicio_duracao": "3 dias", "associados": "febre"}),
    (
        "A dor na barriga piora depois de comer e melhora quando deito",
        "dor_abdominal",
        {"piora": "depois de comer", "melhora": "ao deitar"},
    ),
    (
        "Minha dor de cabeça é fraquinha, vem de vez em quando",
        "cefaleia",
        {"intensidade": "fraca", "frequencia": "às vezes"},
    ),
    ("Estou com tontura, nota 6", "tontura", {"intensidade": "6/10"}),
    # nada deve ser inventado: duração de remédio, "um dia" e "a cada 8 horas" não são duração da queixa
    ("Tomei ibuprofeno há duas semanas e a dor de cabeça continua", "cefaleia", {"inicio_duracao": ""}),
    ("Um dia eu caí e desde então tenho dor no joelho", "dor_joelho", {"inicio_duracao": ""}),
    ("Tenho dor de cabeça e tomo dipirona a cada 8 horas", "cefaleia", {"inicio_duracao": "", "frequencia": ""}),
]


@pytest.mark.parametrize(("frase", "chave", "esperado"), FRASES, ids=[f[0][:40] for f in FRASES])
def test_extrator_semiologico_com_frases(record, frase, chave, esperado):
    ins = child_engine(record, "clinica_medica", [("paciente", frase)], birth="1966-03-12", gender="female").run()
    queixa = next(q for q in ins.anamnese.queixas_detalhadas if q.chave == chave)
    for campo, valor in esperado.items():
        got = getattr(queixa, campo)
        assert got.valor == valor, (frase, campo, got)
        assert bool(got.refs) == bool(valor)
        assert all(r == "Fala/1" for r in got.refs)
    assert len(FRASES) >= 10


def test_resposta_ao_profissional_alimenta_o_campo_no_lugar_certo(record):
    falas = [
        ("paciente", "Tenho dor no peito"),
        ("medico", "O que melhora essa dor?"),
        ("paciente", "Quando eu sento e descanso"),
        ("medico", "E com que frequência acontece?"),
        ("paciente", "Umas duas vezes por dia"),
    ]
    q = child_engine(record, "clinica_medica", falas, birth="1966-03-12", gender="female").run()
    peito = next(x for x in q.anamnese.queixas_detalhadas if x.chave == "dor_toracica")
    assert peito.melhora.valor == "com repouso" and peito.melhora.refs == ["Fala/3"]
    assert peito.frequencia == CampoSemio(valor="2 vezes por dia", refs=["Fala/5"])
    done = {x.id.split(":")[0]: x for x in q.perguntas_abordadas}
    assert {"q081", "q082"} <= set(done)  # melhora e frequência
    assert done["q081"].resposta == "Quando eu sento e descanso"


# ---------- #9: campos extras da especialidade (pediatria) ----------
def test_pediatria_gestacao_parto_vacinas_e_dnpm(record):
    falas = [
        ("acompanhante", "Ele nasceu de parto normal, a termo, e as vacinas estão em dia"),
        ("medico", "E o desenvolvimento dele? Já andou?"),
        ("acompanhante", "Andou com um ano e já fala palavras"),
    ]
    ins = child_engine(record, "pediatria", falas, birth="2022-03-01").run()
    extras = {c.campo: c for c in ins.anamnese.campos_especialidade}
    assert set(extras) == {"gestacao_parto", "vacinas", "dnpm"}
    assert "parto normal" in extras["gestacao_parto"].valor and "a termo" in extras["gestacao_parto"].valor
    assert extras["vacinas"].valor == "em dia" and extras["vacinas"].refs == ["Fala/1"]
    assert "anda" in extras["dnpm"].valor and "fala" in extras["dnpm"].valor
    # o que já foi dito não vira pergunta; a pergunta que o profissional fez fica nas abordadas
    pending = {p.id.split(":")[0] for p in ins.perguntas}
    rows = {q.trigger.split(":", 1)[1]: q.id for q in knowledge.questions() if q.trigger.startswith("semiologia:")}
    assert rows["gestacao_parto"] not in pending and rows["vacinas"] not in pending
    assert rows["dnpm"] in {p.id.split(":")[0] for p in ins.perguntas_abordadas}
    vazio = child_engine(record, "pediatria", [("acompanhante", "Ele está com tosse")], birth="2022-03-01").run()
    pend = {p.id.split(":")[0] for p in vazio.perguntas}
    assert {rows["gestacao_parto"], rows["vacinas"], rows["dnpm"]} <= pend


# ---------- a queixa principal que o médico fixa ----------
def test_medico_fixa_e_solta_a_queixa_principal(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("paciente", "Estou com tontura há dois dias e com dor de cabeça"))
    assert ins["anamnese"]["qp_principal"] == "tontura"
    res = client.put(f"/consultations/{cid}/queixa-principal", json={"chave": "cefaleia"}, headers=h)
    assert res.status_code == 200, res.text
    anam = res.json()["insights"]["anamnese"]
    assert anam["qp_principal"] == "cefaleia" and anam["qp_fixada"] is True
    assert anam["queixa_principal"].startswith("Dor de cabeça") and anam["queixas_detalhadas"][0]["chave"] == "cefaleia"
    # fica fixada nas falas seguintes
    ins = say(client, h, cid, ("paciente", "Também estou com muita sede"))
    assert ins["anamnese"]["qp_principal"] == "cefaleia"
    res = client.put(f"/consultations/{cid}/queixa-principal", json={"chave": "dor_lombar"}, headers=h)
    assert res.status_code == 422
    res = client.put(f"/consultations/{cid}/queixa-principal", json={"chave": None}, headers=h)
    assert res.json()["insights"]["anamnese"]["qp_principal"] == "tontura"
    assert not res.json()["insights"]["anamnese"]["qp_fixada"]
    assert client.put(f"/consultations/{cid}/queixa-principal", json={"chave": "tontura"}).status_code == 401


# ---------- IA: paráfrase e anamnese com fontes ----------
def _payload(**over) -> dict:
    anam = {
        "queixa_principal": "Tontura",
        "hda": "Tontura há 3 semanas.",
        "antecedentes": [],
        "medicacoes_em_uso": [],
        "alergias": [],
        "habitos_e_contexto": [],
        "revisao_de_sistemas": [],
        "refs": ["Fala/2"],
        "queixas_detalhadas": [],
        "campos_especialidade": [],
    }
    base = {
        "resumo": "Tontura.",
        "destaques": [],
        "queixas": [],
        "emocoes": [],
        "perguntas": [],
        "falantes": [],
        "perguntas_abordadas": [],
        "hipoteses": [],
        "sugestoes_medicamentos": [],
        "cirurgia": [],
        "historico_relatado": [],
        "anamnese": anam,
    }
    return base | over


def _campo(valor: str = "", refs: list[str] | None = None) -> dict:
    return {"valor": valor, "refs": refs or []}


def _detalhada(**campos) -> dict:
    """Queixa detalhada como a IA a devolve: lista de campos {campo, valor, refs}; o que não foi dito fica de fora."""
    return {
        "chave": "tontura",
        "rotulo": "Tontura",
        "campos": [{"campo": nome, "valor": c["valor"], "refs": c["refs"]} for nome, c in campos.items()],
    }


def _live(client, h, cid, monkeypatch, payload) -> dict:
    fake = FakeClient(payload)
    monkeypatch.setattr(llm_client.get_settings(), "llm_mode", "real")
    monkeypatch.setattr(llm_client, "_client", fake)
    try:
        res = client.post(f"/consultations/{cid}/insights", headers=h)
    finally:
        monkeypatch.setattr(llm_client, "_client", None)
    assert res.status_code == 200, res.text
    return res.json()


def test_ia_devolve_ids_de_perguntas_abordadas_por_parafrase_validados_pelas_fontes(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(
        client,
        h,
        cid,
        ("paciente", "Ando muito tonta, doutora, e com a vista escurecendo"),
        ("medico", "Já chegou a ir ao chão, a perder os sentidos?"),
        ("paciente", "Uma vez quase"),
        ("paciente", "Também estou com muito cansaço"),
    )
    desmaio = next(q for q in ins["perguntas"] if "desmaiar, perder a consciência" in q["pergunta"])
    cansaco = next(q for q in ins["perguntas"] if "O cansaço vem com falta de ar" in q["pergunta"])
    payload = _payload(
        perguntas_abordadas=[
            {"id": desmaio["id"], "refs": ["Fala/2"], "resposta_refs": ["Fala/3"]},  # válida
            {"id": cansaco["id"], "refs": ["Fala/1"], "resposta_refs": []},  # fala do paciente: não vale
            {"id": "q999", "refs": ["Fala/2"], "resposta_refs": []},  # id que não existe
            {"id": cansaco["id"], "refs": ["Fala/99"], "resposta_refs": []},  # fala que não existe
        ]
    )
    body = _live(client, h, cid, monkeypatch, payload)
    out = body["insights"]
    assert not [q for q in out["perguntas"] if q["id"] == desmaio["id"]]
    done = next(q for q in out["perguntas_abordadas"] if q["id"] == desmaio["id"])
    assert done["origem"] == "ia" and done["refs"] == ["Fala/2"] and done["resposta"] == "Uma vez quase"
    assert [q for q in out["perguntas"] if q["id"] == cansaco["id"]]  # continua sugerida
    onde = [d["onde"] for d in body["ia"]["descartados"]]
    assert onde.count("perguntas_abordadas") == 3
    # a IA recebeu os ids das perguntas já sugeridas
    again = say(client, h, cid, ("paciente", "Tenho sede o tempo todo"))
    assert [q for q in again["perguntas_abordadas"] if q["id"] == desmaio["id"]]  # a próxima fala re-mescla


def test_ia_nao_ve_o_paciente_perguntar_e_a_pergunta_so_vale_com_fala_do_profissional(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, ("paciente", "Ando muito tonta"), ("medico", "Há quanto tempo?"), ("paciente", "Três semanas"))
    sent = {}

    def capture(payload):
        fake = FakeClient(payload)
        monkeypatch.setattr(llm_client.get_settings(), "llm_mode", "real")
        monkeypatch.setattr(llm_client, "_client", fake)
        client.post(f"/consultations/{cid}/insights", headers=h)
        monkeypatch.setattr(llm_client, "_client", None)
        sent["calls"] = json.dumps(fake.calls[0], ensure_ascii=False, default=str)

    capture(_payload())
    assert "perguntas_ja_sugeridas" in sent["calls"] and '\\"id\\": \\"q' in sent["calls"]
    assert "anamnese_por_regra" in sent["calls"]


def test_merge_semiologico_vale_o_campo_da_regra_que_tem_fonte(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, ("paciente", "Ando muito tonta"), ("paciente", "Faz umas três semanas"))
    payload = _payload(
        anamnese=_payload()["anamnese"]
        | {
            "queixas_detalhadas": [
                _detalhada(
                    inicio_duracao=_campo("há dois meses", ["Fala/1"]),  # a regra já tem, com fonte: prevalece
                    piora=_campo("ao levantar", ["Fala/2"]),  # fonte que não diz isso, mas é ref válida: entra
                    melhora=_campo("com repouso", ["Fala/99"]),  # ref inválida: fora
                    frequencia=_campo("todo dia", []),  # sem ref: fora
                )
            ]
        }
    )
    body = _live(client, h, cid, monkeypatch, payload)
    tontura = body["insights"]["anamnese"]["queixas_detalhadas"][0]
    assert tontura["inicio_duracao"]["valor"] == "3 semanas"
    assert tontura["piora"]["valor"] == "ao levantar"
    assert tontura["melhora"]["valor"] == "" and tontura["frequencia"]["valor"] == ""
    assert {"melhora", "frequencia"} <= set(tontura["faltam"]) and "piora" not in tontura["faltam"]
    # o que a IA preencheu com fonte sai da lista de perguntas pendentes
    assert not any("o que piora ou desencadeia" in q["pergunta"] for q in body["insights"]["perguntas"])
    assert any("o que alivia" in q["pergunta"] for q in body["insights"]["perguntas"])


def test_merge_mantem_a_queixa_principal_fixada_pelo_medico(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, ("paciente", "Estou com tontura e dor de cabeça"))
    client.put(f"/consultations/{cid}/queixa-principal", json={"chave": "cefaleia"}, headers=h)
    body = _live(client, h, cid, monkeypatch, _payload())
    anam = body["insights"]["anamnese"]
    assert anam["qp_principal"] == "cefaleia" and anam["qp_fixada"] is True
    assert anam["queixa_principal"].startswith("Dor de cabeça")  # a IA escreveu "Tontura" e não prevaleceu


def test_modelos_de_saida_da_ia_continuam_pequenos_e_validam_json_antigo():
    # saída guardada de antes (sem os campos novos) continua valendo
    old = {k: v for k, v in _payload().items() if k != "perguntas_abordadas"}
    old["anamnese"] = {
        k: v
        for k, v in old["anamnese"].items()
        if k
        in (
            "queixa_principal",
            "hda",
            "antecedentes",
            "medicacoes_em_uso",
            "alergias",
            "habitos_e_contexto",
            "revisao_de_sistemas",
            "refs",
        )
    }
    from app.consult import ai

    out = ai.ConsultLiveOutput.model_validate(old)
    assert out.perguntas_abordadas == [] and out.anamnese.queixas_detalhadas == []
    assert isinstance(
        Insights.model_validate(
            {"tipo": "consulta", "especialidade": "clinica_medica", "falas_analisadas": 0, "modo": "regras"}
        ),
        Insights,
    )


# ---------- verificação independente: falsos positivos de "abordada" e de semiologia ----------
@pytest.mark.parametrize(
    ("frase", "pergunta"),
    [
        ("O ar tem faltado quando caminha?", True),
        ("o ar tem faltado quando caminha", True),
        ("e a febre", True),
        ("E como estão o sono, o apetite e o ânimo?", True),
        ("a senhora dorme bem", True),
        ("de zero a dez quanto doi", True),
        ("desde quando o joelho dói", True),
        ("Tem tosse com catarro?", True),
        ("Vou pedir exames para investigar a falta de ar, tá?", False),
        ("Vamos olhar o sono depois", False),
        ("A dor piora quando a pessoa carrega peso e melhora com repouso, é comum", False),
        ("Isso explica parte da tontura.", False),
        ("Pode ser, junto com o açúcar baixo e o anti-inflamatório. Vamos ajustar.", False),
        ("Bom dia, dona Joana! Que bom ver a senhora.", False),
        ("Vou suspender o ibuprofeno e a tontura deve melhorar quando levantar devagar", False),
    ],
)
def test_so_pergunta_do_profissional_conta_como_pergunta(frase, pergunta):
    from app.consult.text import is_question

    assert is_question(frase) is pergunta


@pytest.mark.parametrize(
    "explicacao",
    [
        "Vou pedir exames pra investigar esse cansaço e a falta de ar, tá?",
        "Esse cansaço pode ser da anemia, a falta de ar a gente vê depois com o ecocardiograma.",
        "Anote aí: dor no peito e falta de ar são sinais de alerta, se aparecerem volte aqui.",
    ],
)
def test_medico_que_comenta_o_assunto_sem_perguntar_nao_aborda_a_pergunta(client, auth, explicacao):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(
        client, h, cid, ("paciente", "Ando muito cansada, doutora"), ("medico", explicacao), ("paciente", "Tá bom")
    )
    assert not [q for q in ins["perguntas_abordadas"] if row_of(q).trigger == "sintoma:cansaco"], explicacao
    assert [q for q in ins["perguntas"] if row_of(q).trigger == "sintoma:cansaco"]


def test_medico_explicando_piora_e_melhora_nao_responde_a_semiologia(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(
        client,
        h,
        cid,
        ("paciente", "Estou com dor nas costas faz uma semana"),
        ("medico", "A dor piora quando a pessoa carrega peso e melhora com repouso, é comum"),
    )
    assert not [q for q in ins["perguntas_abordadas"] if q["id"].startswith(("q080", "q081"))]
    assert any(q["id"].startswith("q080") for q in ins["perguntas"])


def test_demo_do_cen6_nao_trata_comentario_do_medico_como_pergunta_feita(client, auth):
    """Fala/27 ("Pode ser, junto com o açúcar baixo e o anti-inflamatório") explica; ninguém perguntou do remédio."""
    h = auth("medico")
    cid = start(client, h)["id"]
    roteiro = json.loads((clock_root() / "data" / "consultations" / "cen6_consulta.json").read_text(encoding="utf-8"))
    segs = [{"falante": f["falante"], "texto": f["texto"]} for f in roteiro["falas"]]
    res = client.post(f"/consultations/{cid}/segments", json={"segmentos": segs}, headers=h)
    ins = res.json()["insights"]
    assert not [q for q in ins["perguntas_abordadas"] if q["refs"] == ["Fala/27"]]
    assert all(
        q["resposta"] != "Ah, que alívio, doutora. Achei que era coisa pior." for q in ins["perguntas_abordadas"]
    )
    # as perguntas que o profissional fez de fato continuam abordadas, com a resposta
    done = {q["id"]: q for q in ins["perguntas_abordadas"]}
    assert done["q078:tontura"]["resposta_refs"] == ["Fala/4"]
    assert done["q007"]["refs"] == ["Fala/21"] and done["q007"]["resposta_refs"] == ["Fala/22"]


def clock_root():
    from app.config import get_settings

    return get_settings().data_dir.parent


def test_hda_nao_repete_a_queixa_que_ja_aparece_como_associada(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("paciente", "Estou com febre e tosse há dois dias"))
    hda = ins["anamnese"]["hda"]
    assert "associada a tosse" in hda and "Refere também" not in hda


def test_posologia_e_inicio_da_noite_nao_viram_frequencia_nem_piora_da_queixa(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(
        client,
        h,
        cid,
        ("paciente", "Tive tosse. Volto em 15 dias para retorno"),
        ("paciente", "Tomo o remédio 3 vezes ao dia"),
        ("paciente", "Dor de cabeça desde ontem à noite que piora quando levanto"),
    )
    q = {x["chave"]: x for x in ins["anamnese"]["queixas_detalhadas"]}
    assert q["tosse"]["frequencia"]["valor"] == "" and q["tosse"]["inicio_duracao"]["valor"] == ""
    assert q["cefaleia"]["inicio_duracao"]["valor"] == "desde ontem à noite"
    assert "à noite" not in q["cefaleia"]["piora"]["valor"]


def test_febre_nao_gera_pergunta_de_intensidade_de_0_a_10(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("paciente", "Estou com febre faz três dias"))
    assert not [q for q in ins["perguntas"] if q["id"].startswith(("q079:febre", "q080:febre"))]
    assert any(q["id"].startswith("q081:febre") for q in ins["perguntas"])


def test_campo_da_ia_com_fonte_na_fala_do_medico_nao_entra_na_anamnese(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, ("paciente", "Ando muito tonta"), ("medico", "Isso piora ao levantar, é comum"))
    payload = _payload(
        anamnese=_payload()["anamnese"]
        | {"queixas_detalhadas": [_detalhada(piora=_campo("ao levantar", ["Fala/2"]))]}  # Fala/2 é do médico
    )
    body = _live(client, h, cid, monkeypatch, payload)
    tontura = body["insights"]["anamnese"]["queixas_detalhadas"][0]
    assert tontura["piora"]["valor"] == ""


def test_lista_de_perguntas_do_cen6_tem_limite_para_caber_no_tablet(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    roteiro = json.loads((clock_root() / "data" / "consultations" / "cen6_consulta.json").read_text(encoding="utf-8"))
    segs = [{"falante": f["falante"], "texto": f["texto"]} for f in roteiro["falas"]]
    ins = client.post(f"/consultations/{cid}/segments", json={"segmentos": segs}, headers=h).json()["insights"]
    proto = [q for q in ins["perguntas"] if q["id"].startswith("protocolo:")]  # protocolos vivos (#28): até 3, na cauda
    semio = [q for q in ins["perguntas"] if q not in proto and row_of(q).trigger.startswith("semiologia:")]
    assert len(ins["perguntas"]) - len(semio) - len(proto) <= 10 and len(semio) <= 8 and len(proto) <= 3
    assert len({q["pergunta"] for q in ins["perguntas"]}) == len(ins["perguntas"])


# ---------- verificação independente: frases adversariais (remédio e queixa na mesma frase) ----------
@pytest.mark.parametrize(
    ("frase", "chave", "campo", "esperado"),
    [
        ("tomo losartana de manhã e tenho tontura ao levantar", "tontura", "piora", "ao levantar"),
        ("tomo losartana 2 vezes ao dia e tenho tontura ao levantar", "tontura", "frequencia", ""),
        ("tomei dipirona e a febre não baixou há 4 dias", "febre", "inicio_duracao", "4 dias"),
        ("tenho dor de barriga há 2 dias depois de comer", "dor_abdominal", "piora", "depois de comer"),
        ("Meu pai teve infarto, eu tenho dor de barriga há 2 dias", "dor_abdominal", "inicio_duracao", "2 dias"),
        ("não tenho febre mas estou com tosse há duas semanas", "tosse", "inicio_duracao", "2 semanas"),
        ("Ela está com diarreia desde ontem à noite", "diarreia", "piora", ""),
    ],
)
def test_frases_adversariais_da_semiologia(client, auth, frase, chave, campo, esperado):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, ("paciente", frase))
    q = {x["chave"]: x for x in ins["anamnese"]["queixas_detalhadas"]}
    assert chave in q, frase
    assert q[chave][campo]["valor"] == esperado, frase
