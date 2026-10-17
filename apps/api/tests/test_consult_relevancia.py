"""#10 — Relevância de cada fala (clínica × social × administrativa), trava de segurança e cobertura da nota."""

import pytest

from app.consult import ai as consult_ai
from app.consult import relevancia
from app.consult import service as consult_service
from app.consult.schemas import Segment
from app.llm.client import LLMResult, load_prompt
from tests.test_consult import SCRIPT, say, start


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def falas(*pairs):
    return [{"falante": who, "texto": t, "t": float(i)} for i, (who, t) in enumerate(pairs, 1)]


def segmentos(client, h, cid):
    return {s["n"]: s for s in client.get(f"/consultations/{cid}", headers=h).json()["segmentos"]}


def ai_says(monkeypatch, relevancias: list[dict]):
    """IA (mockada) devolvendo a reclassificação pedida; o resto é a saída do modo mock."""

    def fake(name, variables, schema, mock=None, effort=None):
        out = mock(variables) | ({"relevancia": relevancias} if name == "consult_live_conversa" else {})
        return LLMResult(output=schema.model_validate(out), model="fake", prompt_version="x", mode="real")

    monkeypatch.setattr(consult_ai, "generate_structured", fake)


# ---------- critério 1: conversa social ----------
def test_traffic_small_talk_is_social_and_clinical_talk_is_clinical(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(
        client,
        h,
        cid,
        falas(
            ("paciente", "Hoje o trânsito estava terrível"),
            ("paciente", "Eu ando muito tonta desde segunda"),
            ("medico", "Qual é o seu convênio, para eu preencher a guia?"),
        ),
    )
    seg = segmentos(client, h, cid)
    assert seg[1]["relevancia"] == "social"
    assert seg[2]["relevancia"] == "clinica"
    assert seg[3]["relevancia"] == "administrativa"
    assert all(s["relevancia_origem"] == "regra" for s in seg.values())


def test_cen6_script_is_classified_and_greetings_are_social(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    seg = segmentos(client, h, cid)
    assert seg[1]["relevancia"] == "social"  # "Bom dia, dona Joana! ... A Fernanda veio junto hoje."
    assert seg[28]["relevancia"] == "social"  # "Ah, que alívio, doutora. Achei que era coisa pior."
    for n in (4, 7, 11, 13, 20, 22, 25):
        assert seg[n]["relevancia"] == "clinica", n


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("Choveu muito ontem, né", "social"),
        ("O Corinthians perdeu ontem", "social"),
        ("Que calor hoje, doutora", "social"),
        ("minha filha vai casar mês que vem", "social"),
        ("Bom dia", "social"),
        ("Obrigada, até logo", "social"),
        ("Preciso remarcar o retorno", "administrativa"),
        ("Qual o horário de atendimento de vocês?", "administrativa"),
        ("Vou ligar pro convênio", "administrativa"),
        ("tomo glibenclamida em jejum", "clinica"),
        ("Tô com dor no peito desde cedo", "clinica"),
        ("tenho alergia a dipirona", "clinica"),
        ("usei 500 mg de manhã", "clinica"),
        ("a pressão deu 14 por 9", "clinica"),
        ("meu pai teve infarto aos 50", "clinica"),
        ("fumo um maço por dia", "clinica"),
    ],
)
def test_adversarial_utterances(texto, esperado):
    assert relevancia.classify(texto) == esperado


def test_short_answer_to_a_clinical_question_stays_clinical():
    pergunta = Segment(n=1, t=0, falante="medico", texto="A tontura piora ao levantar?", relevancia="clinica")
    assert relevancia.classify("sim, piora", pergunta, "paciente") == "clinica"
    assert relevancia.classify("sim, piora", None, "paciente") == "social"
    assert relevancia.classify("obrigada", pergunta, "paciente") == "social"


# ---------- critério 2: trava de segurança com a IA ----------
def test_locked_utterance_stays_clinical_when_the_mock_ai_calls_it_social(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(
        client,
        h,
        cid,
        falas(
            ("paciente", "tomo glibenclamida em jejum"),
            ("paciente", "tenho alergia a camarão"),
            ("paciente", "de vez em quando me dá falta de ar"),
            ("paciente", "tomo dois comprimidos de manhã"),
            ("paciente", "Hoje o trânsito estava terrível"),
            ("paciente", "Eu ando muito tonta desde segunda"),
        ),
    )
    ai_says(
        monkeypatch,
        [{"n": n, "relevancia": "social"} for n in (1, 2, 3, 4, 6)] + [{"n": 5, "relevancia": "administrativa"}],
    )
    res = client.post(f"/consultations/{cid}/insights?force=true", headers=h)
    assert res.status_code == 200, res.text
    seg = {s["n"]: s for s in res.json()["segmentos"]}
    for n in (1, 2, 3, 4):  # medicamento, alergia, sinal de alarme, dose: a trava vence a IA
        assert seg[n]["relevancia"] == "clinica", n
        assert seg[n]["relevancia_origem"] == "regra"
    assert seg[5]["relevancia"] == "administrativa" and seg[5]["relevancia_origem"] == "ia"  # sem trava: a IA decide
    assert seg[6]["relevancia"] == "social"  # sintoma sem medicamento/alarme: a IA pode reclassificar
    stored = segmentos(client, h, cid)
    assert stored[1]["relevancia"] == "clinica" and stored[5]["relevancia"] == "administrativa"


def test_ai_can_promote_a_social_utterance_and_invalid_labels_are_ignored():
    segs = [
        Segment(n=1, t=0, falante="paciente", texto="Hoje o trânsito estava terrível", relevancia="social"),
        Segment(n=2, t=1, falante="paciente", texto="tomo losartana", relevancia="clinica"),
    ]
    assert relevancia.apply_ai(segs, {1: "clinica", 2: "banana"}) == [1]
    assert segs[0].relevancia == "clinica" and segs[0].relevancia_origem == "ia"
    assert segs[1].relevancia == "clinica"
    assert relevancia.apply_ai(segs, {2: "social"}) == []  # trava


def test_ai_prompts_carry_the_relevance_instructions():
    conversa, note = load_prompt("consult_live_conversa"), load_prompt("consult_note")
    assert conversa.version == "1.5.0" and "relevancia" in conversa.system
    assert note.version == "1.4.0" and "conversa social" in note.system
    assert "relevancia" in consult_ai.ConversaOutput.model_fields
    assert set(consult_ai.AiRelevancia.model_fields) == {"n", "relevancia"}


def test_transcript_sent_to_the_ai_marks_social_utterances():

    class Pseudo:
        @staticmethod
        def text(t):
            return t

    segs = [
        Segment(n=1, t=0, falante="paciente", texto="Hoje o trânsito estava terrível", relevancia="social"),
        Segment(n=2, t=5, falante="paciente", texto="tomo losartana", relevancia="clinica"),
    ]
    text = consult_ai.transcript_text(segs, Pseudo())
    assert "· social]" in text.splitlines()[0] and "social" not in text.splitlines()[1]


# ---------- critérios 3 e 4: cobertura e "incluir" no cen6 ----------
def finalizar(client, h):
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    res = client.post(f"/consultations/{cid}/finish", headers=h)
    assert res.status_code == 200, res.text
    return cid, res.json()


def test_cen6_shows_the_computed_coverage_and_what_stayed_out(client, auth):
    h = auth("medico")
    cid, body = finalizar(client, h)
    cob = body["cobertura"]
    assert cob["total"] >= 15 and 0 < cob["citadas"] < cob["total"]
    assert cob["rotulo"] == f"{cob['citadas']} de {cob['total']} falas clínicas citadas"
    assert cob["percentual"] == round(100 * cob["citadas"] / cob["total"])
    assert len(cob["fora"]) == cob["total"] - cob["citadas"]
    assert all(f["ref"] == f"Fala/{f['n']}" and f["texto"] for f in cob["fora"])
    assert cob["sociais"] >= 2
    # nada do que está "fora" é citado por alguma seção da nota
    assert not {f["n"] for f in cob["fora"]} & relevancia.cited_falas(body["nota"])
    # o que a nota gerou fica registrado em nota_meta
    assert body["nota_meta"]["cobertura"]["total"] == cob["total"]
    again = client.get(f"/consultations/{cid}", headers=h).json()
    assert again["cobertura"] == cob


def test_including_fala_16_in_the_plan_adds_its_ref_and_updates_the_coverage(client, auth):
    h = auth("medico")
    cid, body = finalizar(client, h)
    antes = body["cobertura"]
    res = client.post(f"/consultations/{cid}/nota/incluir", json={"n": 16, "destino": "plano"}, headers=h)
    assert res.status_code == 200, res.text
    out = res.json()
    plano = out["nota"]["plano"]
    assert "Fala/16" in plano["refs"]
    assert "medo de ter que tomar insulina" in plano["texto"]
    assert out["incluidas"][0]["n"] == 16 and out["incluidas"][0]["destino"] == "plano"
    assert 16 not in {f["n"] for f in out["cobertura"]["fora"]}
    assert out["cobertura"]["citadas"] >= antes["citadas"]
    # incluir de novo não duplica o trecho
    again = client.post(f"/consultations/{cid}/nota/incluir", json={"n": 16, "destino": "plano"}, headers=h).json()
    assert again["nota"]["plano"]["texto"].count("medo de ter que tomar insulina") == 1
    assert len(again["incluidas"]) == 1
    # a revisão do texto não apaga o registro de proveniência
    note = client.get(f"/consultations/{cid}", headers=h).json()["nota"]
    saved = client.put(f"/consultations/{cid}/note", json=note | {"incluidas": []}, headers=h)
    assert saved.status_code == 200, saved.text
    assert saved.json()["nota"]["incluidas"][0]["n"] == 16
    assert "Fala/16" in saved.json()["nota"]["plano"]["refs"]


def test_include_to_pendencias_and_validation_errors(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    antes = client.post(f"/consultations/{cid}/nota/incluir", json={"n": 16, "destino": "plano"}, headers=h)
    assert antes.status_code == 409  # a nota só existe depois de finalizar
    client.post(f"/consultations/{cid}/finish", headers=h)
    assert (
        client.post(f"/consultations/{cid}/nota/incluir", json={"n": 99, "destino": "plano"}, headers=h).status_code
        == 404
    )
    assert (
        client.post(f"/consultations/{cid}/nota/incluir", json={"n": 16, "destino": "xyz"}, headers=h).status_code
        == 422
    )
    ok = client.post(f"/consultations/{cid}/nota/incluir", json={"n": 21, "destino": "pendencia"}, headers=h)
    assert ok.status_code == 200
    assert ok.json()["nota"]["pendencias"][-1]["refs"] == ["Fala/21"]


def test_discarded_items_of_the_note_reach_the_consultation(client, auth):
    h = auth("medico")
    cid, body = finalizar(client, h)
    assert isinstance(body["nota_meta"]["descartados"], list)  # a tela da consulta lista os de nota_meta
    assert "cobertura" in body and body["segmentos"][0]["relevancia"] in ("clinica", "social", "administrativa")


def test_old_consultations_without_relevance_are_classified_on_read():
    segs = relevancia.ensure(
        [Segment.model_validate({"n": 1, "t": 0, "falante": "paciente", "texto": "Hoje o trânsito estava terrível"})]
    )
    assert segs[0].relevancia == "social"
    note = {
        "plano": {"texto": "", "refs": ["Fala/3"]},
        "cids": [{"refs": ["Fala/4"]}],
        "cronologia": [{"refs": ["Fala/5"]}],
    }
    assert relevancia.cited_falas(note) == {3, 4, 5}


@pytest.mark.parametrize(
    "texto",
    [
        "sinto um caroço no seio",
        "to cagando muito",
        "a criança engasgou",
        "faz dias que sinto uma coisa estranha",
    ],
)
def test_sintoma_coloquial_nao_vira_social(texto):
    assert relevancia.classify(texto) == "clinica"
