"""Cronologia viva, frases com fonte e nota completa mesmo sem a IA (#15).

Caminho real: API → service → engine/cronologia → ai.mock_note, em LLM_MODE=mock, sobre o cen6; a queda do
fornecedor é simulada trocando `generate_structured` por uma função que levanta LLMError.
"""

import json

import pytest

from app.consult import ai as consult_ai
from app.consult import cronologia, notas
from app.consult import service as consult_service
from app.consult.schemas import Insights, Segment
from app.llm.client import LLMError
from tests.test_consult import SCRIPT, say, start
from tests.test_consult_revisao import consult_finished, detail, review, surgery_finished

SECTIONS = notas.SECTION_KEYS


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def seg(n: int, who: str, text: str, t: float = 0.0) -> Segment:
    return Segment(n=n, t=t or n * 5.0, falante=who, texto=text)


def events(*falas: tuple[str, str]) -> list[dict]:
    segments = [seg(i, who, text) for i, (who, text) in enumerate(falas, 1)]
    ins = Insights(tipo="consulta", especialidade="clinica_medica", falas_analisadas=len(segments), modo="regras")
    return cronologia.build(segments, ins)


def kinds(evs: list[dict]) -> set[str]:
    return {e["tipo"] for e in evs}


# ======================= cen6: critérios de aceite =======================
def test_cen6_plano_has_the_conduct_of_fala_27_with_ref(client, auth):
    h = auth("medico")
    _, note = consult_finished(client, h)
    plano = note["plano"]
    assert "Fala/27" in plano["refs"]
    frase = next(f for f in plano["frases"] if "Fala/27" in f["refs"])
    assert "Vamos ajustar" in frase["texto"] and "Vamos ajustar" in plano["texto"] and frase["origem"] == "regra"


def test_cen6_chronology_has_started_diuretic_from_fala_26(client, auth):
    h = auth("medico")
    _, note = consult_finished(client, h)
    item = next(c for c in note["cronologia"] if "iniciou diurético" in c["texto"].lower())
    assert item["refs"] == ["Fala/26"] and item["tipo"] == "medicamento" and item["falante"] == "paciente"
    # cada evento leva mm:ss, Fala/N e o falante; os tipos cobrem o que foi dito
    assert all(c["quando"] and c["refs"] and c["tipo"] and c["falante"] for c in note["cronologia"])
    assert {"queixa", "negativa", "achado", "medicamento"} <= {c["tipo"] for c in note["cronologia"]}
    assert any(c["tipo"] == "negativa" and "dor no peito" in c["texto"].lower() for c in note["cronologia"])
    # a cronologia sai em ordem de fala
    ns = [int(c["refs"][0].split("/")[1]) for c in note["cronologia"]]
    assert ns == sorted(ns)


def test_cen6_every_sentence_has_a_ref_or_is_the_professionals(client, auth):
    h = auth("medico")
    _, note = consult_finished(client, h)
    assert notas.unsourced(note) == []
    total = 0
    for key in SECTIONS:
        sec = note[key]
        for f in sec["frases"]:
            total += 1
            assert f["refs"] or f["origem"] == "profissional"
            assert f["origem"] in ("ia", "regra", "profissional")
            assert f["texto"].strip(" .;") in sec["texto"]  # a frase é um pedaço do texto da seção
        assert set(r for f in sec["frases"] for r in f["refs"]) <= set(sec["refs"])
    assert total >= 10


def test_cen6_physical_exam_has_the_verbalized_findings(client, auth):
    h = auth("medico")
    _, note = consult_finished(client, h)
    ef = note["exame_fisico"]
    assert "Fala/25" in ef["refs"] and "118 por 72" in ef["texto"]  # PA dita pelo médico, além da triagem
    assert any(r.startswith("Observation/") for r in ef["refs"])


def test_professional_edit_turns_only_the_edited_sentence_into_professional(client, auth):
    h = auth("medico")
    cid, note = consult_finished(client, h)
    plano = note["plano"]
    keep = plano["frases"][1]  # uma frase que o profissional não toca
    changed = plano["frases"][0]
    edited = json.loads(json.dumps(note))
    new_text = plano["texto"].replace("Vamos ajustar.", "Vamos ajustar a glibenclamida e a dose do diurético.")
    assert new_text != plano["texto"]
    edited["plano"]["texto"] = new_text + "; Retorno em 15 dias com exames."
    saved = review(client, h, cid, edited).json()["nota"]["plano"]
    by_text = {f["texto"]: f for f in saved["frases"]}
    assert by_text[keep["texto"]]["origem"] == "regra" and by_text[keep["texto"]]["refs"] == keep["refs"]
    mine = [f for f in saved["frases"] if f["origem"] == "profissional"]
    assert len(mine) >= 2
    assert any("Retorno em 15 dias" in f["texto"] and f["refs"] == [] for f in mine)  # frase nova: sem fonte, assumida
    changed_now = next(f for f in mine if "Vamos ajustar a glibenclamida" in f["texto"])
    assert "Fala/27" in changed_now["refs"]  # herda a fonte da frase de que se parece
    assert changed["texto"] not in by_text
    assert notas.unsourced(client.get(f"/consultations/{cid}", headers=h).json()["nota"]) == []


def test_client_cannot_forge_sentence_origin(client, auth):
    h = auth("medico")
    cid, note = consult_finished(client, h)
    edited = json.loads(json.dumps(note))
    edited["hda"]["texto"] = "Texto inventado pelo cliente."
    edited["hda"]["frases"] = [{"texto": "Texto inventado pelo cliente.", "refs": ["Fala/2"], "origem": "ia"}]
    saved = review(client, h, cid, edited).json()["nota"]["hda"]
    assert [f["origem"] for f in saved["frases"]] == ["profissional"] and saved["frases"][0]["refs"] == []


# ======================= nota sem a IA =======================
def break_llm(monkeypatch):
    def boom(*_a, **_k):
        raise LLMError("O serviço de IA está fora do ar.")

    monkeypatch.setattr(consult_ai, "generate_structured", boom)


def test_llm_error_still_returns_a_rules_note_with_a_notice(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    break_llm(monkeypatch)
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "finalizada" and body["nota_meta"]["modo"] == "regras_fallback"
    assert "IA indisponível" in body["nota_meta"]["aviso"]
    note = body["nota"]
    assert "Fala/27" in note["plano"]["refs"] and "Fala/25" in note["exame_fisico"]["refs"]
    assert any("iniciou diurético" in c["texto"].lower() for c in note["cronologia"])
    assert notas.unsourced(note) == []
    # a nota é revisável e exportável como qualquer outra
    review(client, h, cid, note | {"orientacoes_paciente": note["orientacoes_paciente"]})
    # o alerta crítico não aceito continua valendo sem a IA: copiar/baixar pede o envio com justificativa
    blocked = client.get(f"/consultations/{cid}/export", headers=h)
    assert blocked.status_code == 409 and detail(blocked)["codigo"] == "pendencia_critica"
    sent = client.post(
        f"/consultations/{cid}/send", json={"justificativa": "Risco discutido com a paciente"}, headers=h
    )
    assert sent.status_code == 200, sent.text
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 200


def test_llm_error_on_surgery_falls_back_to_the_rules_report(client, auth, monkeypatch):
    h = auth("medico")
    from app.consult.knowledge import demo_scripts
    from tests.test_surgical import dictate
    from tests.test_surgical import start as start_surgery

    cid = start_surgery(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    dictate(client, h, cid, [(f["falante"], f["texto"]) for f in demo_scripts()["cen4_cirurgia"]["falas"]])
    break_llm(monkeypatch)
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200, done.text
    assert (
        done.json()["nota_meta"]["modo"] == "regras_fallback" and done.json()["nota"]["relatorio_cirurgico"]["campos"]
    )


def test_llm_error_is_audited_without_the_transcript(client, auth, monkeypatch):
    from tests.test_consult_revisao import audit

    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    break_llm(monkeypatch)
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    row = audit("consulta.erro_llm")[-1]
    assert row["fallback"] == "regras" and "glibenclamida" not in json.dumps(row).lower()


# ======================= export =======================
def test_exported_text_has_the_chronological_summary_and_pending_items(client, auth):
    h = auth("medico")
    cid, note = consult_finished(client, h)
    review(client, h, cid, note)
    text = client.get(f"/consultations/{cid}/export", headers=h).json()["texto"]
    assert "Resumo cronológico:" in text and "Pendências:" in text
    assert "Iniciou diurético" in text and "Registrar alergia relatada" in text
    # a cronologia editada pelo profissional é a que vai
    edited = json.loads(json.dumps(note))
    edited["cronologia"] = edited["cronologia"][:2] + [
        {"quando": "09:99", "texto": "Evento escrito na revisão", "refs": []}
    ]
    review(client, h, cid, edited)
    assert "Evento escrito na revisão" in client.get(f"/consultations/{cid}/export", headers=h).json()["texto"]


def test_surgical_export_lists_the_pending_items(client, auth):
    h = auth("medico")
    cid, note = surgery_finished(client, h)
    review(client, h, cid, note)
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 409  # campo obrigatório vazio
    sent = client.post(f"/consultations/{cid}/send", json={"justificativa": "Campos no prontuário físico"}, headers=h)
    assert sent.status_code == 200, sent.text
    text = client.get(f"/consultations/{cid}/export", headers=h).json()["texto"]
    assert "Pendências:" in text


# ======================= detector: adversarial (negação, pergunta, quem fala) =======================
def test_questions_are_never_events():
    evs = events(
        ("medico", "O senhor tem edema nas pernas?"),
        ("medico", "A senhora está tomando o diurético?"),
        ("medico", "Será que precisa voltar na semana que vem?"),
        ("medico", "Qual é a sua pressão em casa?"),
    )
    assert evs == []


def test_doctor_findings_are_events_but_patient_values_are_not():
    evs = events(
        ("medico", "Sua pressão está 150 por 90 hoje."),
        ("medico", "Ausculta pulmonar limpa, sem sibilos."),
        ("medico", "Abdome flácido e indolor."),
        ("medico", "Sem edema de membros inferiores."),
        ("medico", "A frequência cardíaca está 92."),
        ("paciente", "Em casa minha pressão deu 140 por 90."),
    )
    achados = [e for e in evs if e["tipo"] == "achado"]
    assert {e["n"] for e in achados} == {1, 2, 3, 4, 5} and all(e["falante"] == "medico" for e in achados)
    assert not any(e["n"] == 6 for e in evs)


def test_patient_medication_decisions_and_their_negations():
    assert [e["texto"] for e in events(("paciente", "Comecei aquele diurético semana passada."))] == [
        "Iniciou diurético"
    ]
    assert [e["texto"] for e in events(("paciente", "Parei a metformina porque passava mal."))] == ["Parou Metformina"]
    assert [e["texto"] for e in events(("acompanhante", "O médico trocou o remédio da pressão mês passado."))] == [
        "Trocou remédio da pressão"
    ]
    assert events(("paciente", "Não comecei o diurético ainda.")) == []
    assert events(("paciente", "Nunca iniciei insulina.")) == []
    assert events(("paciente", "Comecei a caminhar de manhã.")) == []  # sem medicamento: não é decisão de remédio


def test_doctor_decisions_by_type_without_accents_or_punctuation():
    evs = events(
        ("medico", "vou pedir um hemograma e a creatinina"),
        ("medico", "Vamos suspender o ibuprofeno"),
        ("medico", "Volte em duas semanas com os exames"),
        ("medico", "Evite sal e beba bastante água"),
        ("medico", "vou passar losartana 50 mg"),
    )
    by = {(e["n"], e["tipo"]) for e in evs}
    assert (1, "exame") in by and (2, "medicamento") in by and (3, "retorno") in by
    assert (4, "orientacao") in by and (5, "medicamento") in by
    assert next(e for e in evs if e["n"] == 2)["texto"].startswith("Suspender")
    assert "Prescrever" in next(e for e in evs if e["n"] == 5 and e["tipo"] == "medicamento")["texto"]


def test_small_talk_and_patient_side_produce_no_decisions():
    evs = events(
        ("medico", "Bom dia! Como foi o trânsito hoje?"),
        ("paciente", "Foi tranquilo, doutora, o tempo está ótimo."),
        ("paciente", "Vou voltar na semana que vem se precisar."),  # paciente não decide retorno
        ("acompanhante", "Vou pedir um exame para ela."),  # nem acompanhante pede exame
    )
    assert evs == []


def test_pediatric_and_elderly_utterances_keep_working():
    evs = events(
        ("medico", "A temperatura dele está 38 graus e a saturação 97."),
        ("paciente", "Ele começou a tomar o antibiótico ontem."),
        ("medico", "Vamos manter o antibiótico e reavaliar em 48 horas."),
        ("medico", "Para a senhora de 85 anos vou reduzir a dose do anti-hipertensivo."),
    )
    texts = [e["texto"] for e in evs]
    assert any(t.startswith("Temperatura") for t in texts) and any(t.startswith("Saturação") for t in texts)
    assert any(t == "Iniciou antibiótico" for t in texts)
    assert any(e["tipo"] == "retorno" for e in evs)
    assert any(e["tipo"] == "medicamento" and e["falante"] == "medico" for e in evs)


def test_conducts_only_include_what_the_doctor_said():
    evs = events(
        ("paciente", "Comecei o diurético."),
        ("medico", "Pode ser o remédio mesmo. Vamos ajustar."),
        ("medico", "Retorno em 15 dias."),
    )
    cond = cronologia.conducts(evs)
    assert [c["n"] for c in cond] == [2, 3] and all(c["falante"] == "medico" for c in cond)


def test_detail_helper_reads_string_errors():
    class R:
        def json(self):
            return {"detail": "texto simples"}

    assert detail(R()) == {"mensagem": "texto simples"}


# ======================= verificação: o que a fala citada não sustenta não vira evento =======================
def test_negated_or_third_party_or_conditional_speech_is_not_an_event():
    assert events(("medico", "Nao vou pedir exame nenhum hoje")) == []
    assert events(("medico", "Evite pedir exame sem necessidade"))[0]["tipo"] == "orientacao"
    assert events(("paciente", "Meu marido começou o diurético ontem")) == []
    assert events(("paciente", "O doutor me receitou losartana, mas eu não comecei")) == []
    # "Sr." não encerra a frase: a condicional inteira fica fora dos achados
    assert not [
        e for e in events(("medico", "Se o sr. tiver edema, procure o pronto socorro")) if e["tipo"] == "achado"
    ]


def test_new_guards_keep_the_real_decisions():
    by = {(e["tipo"], e["texto"]) for e in events(("medico", "Sem dor no peito, vou pedir um hemograma"))}
    assert any(t == "exame" for t, _ in by)
    assert [e["texto"] for e in events(("paciente", "Comecei a losartana, não tomo mais o outro"))] == [
        "Iniciou Losartana"
    ]
    assert [e["texto"] for e in events(("paciente", "Comecei aquele diurético semana passada."))] == [
        "Iniciou diurético"
    ]
    assert any(e["tipo"] == "achado" for e in events(("medico", "Dra. Ana examinou: abdome flácido e indolor.")))
