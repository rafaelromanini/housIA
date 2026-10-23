"""#43 — Sinais de comunicação: dúvidas e preocupações sem resposta, intensidade por léxico, passagem e alta."""

import pytest
from sqlalchemy import select

from app import clock
from app.consult import knowledge, sinais
from app.consult.engine import Engine
from app.consult.schemas import Segment
from app.db import session as db_session
from app.db.models import Alert
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from tests.test_consult import SCRIPT, say, start


def segs(falas):
    return [Segment(n=i, t=float(i), falante=w, texto=t) for i, (w, t) in enumerate(falas, 1)]  # type: ignore[arg-type]


def engine_for(record, falas, pid="cen6", extra=None) -> Engine:
    rec = record(pid)
    return Engine(
        rec, build_timeline(rec), build_context(rec), segs(falas), "clinica_medica", "consulta", clock.now(), extra
    )


def open_of(falas):
    return [c for c in sinais.unanswered_concerns(segs(falas)) if not c.respondida]


DUVIDA = ("acompanhante", "O que significa essa hemoglobina glicada? E o potássio alto é grave?")


# ---------- critério 1: a dúvida da filha no cen6 ----------
def test_cen6_duvida_da_filha_fica_sem_resposta_ate_o_medico_responder(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    da_filha = [c for c in ins["preocupacoes"] if c["refs"] == ["Fala/18"]]
    assert len(da_filha) == 2 and not any(c["respondida"] for c in da_filha)
    assert {t for c in da_filha for t in c["termos"]} >= {"hemoglobina", "glicada", "potassio"}
    assert all(c["aviso"] == "sinal de comunicação, não diagnóstico" for c in da_filha)
    # "posso operar?" e "será que é o remédio?" foram respondidos pelo médico logo em seguida
    assert all(c["respondida"] for c in ins["preocupacoes"] if c["refs"] in (["Fala/23"], ["Fala/26"]))
    # o médico explica: as duas dúvidas passam a respondidas, com a fala do médico como fonte
    texto = "A hemoglobina glicada mostra a média do açúcar; o potássio alto preocupa e vamos repetir."
    ins = say(client, h, cid, [{"falante": "medico", "texto": texto, "t": 200.0}])
    da_filha = [c for c in ins["preocupacoes"] if c["refs"] == ["Fala/18"]]
    assert da_filha and all(c["respondida"] and c["resposta_refs"] == ["Fala/31"] for c in da_filha)


def test_adiar_nao_e_responder_e_resposta_direta_so_na_fala_seguinte():
    falas = [
        ("paciente", "Posso operar a catarata com essa diabetes?"),
        ("medico", "Já explico. Antes, a senhora tem alguma alergia?"),
        ("paciente", "Tenho de camarão."),
        ("medico", "Pode, sim, com a glicemia controlada."),
    ]
    assert [c.refs for c in open_of(falas)] == [["Fala/1"]]  # "Pode, sim" na 4ª fala não é a fala seguinte
    assert open_of([falas[0], ("medico", "Pode, sim, mas a glicemia precisa estar controlada.")]) == []


def test_sem_termos_de_conteudo_nao_vira_duvida():
    assert open_of([("paciente", "Tudo bem?"), ("paciente", "Entendi tudo. Vou fazer direitinho.")]) == []


def test_preocupacao_respondida_por_acolhimento_ou_assunto():
    medo = ("paciente", "Doutora, eu tenho medo de ter que tomar insulina.")
    assert open_of([medo, ("medico", "Eu entendo o medo. Vamos olhar os exames juntas.")]) == []
    assert len(open_of([medo, ("medico", "Qual a sua pressão em casa?")])) == 1


def test_preocupacao_da_familia_recomenda_teach_back():
    (c,) = open_of([("acompanhante", "Eu fiquei muito preocupada com as quedas dela.")])
    assert c.tipo == "preocupacao" and c.quem == "acompanhante" and c.teach_back is True


# ---------- critério 2: intensidade por léxico ----------
@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Doutora, eu tenho muito medo de operar.", "alta"),
        ("Tenho pavor de agulha.", "alta"),
        ("Estou apavorada com esse resultado.", "alta"),
        ("Estou preocupada com o exame.", "baixa"),
        ("Estou muito preocupada com o exame.", "media"),
        ("Estou um pouco preocupada com o exame.", "baixa"),
    ],
)
def test_intensidade_vem_dos_intensificadores(record, texto, esperado):
    ins = engine_for(record, [("paciente", texto)]).run()
    assert [e.intensidade for e in ins.emocoes] == [esperado]
    if esperado != "baixa" or "pouco" in texto:
        assert ins.emocoes[0].pistas


def test_repetir_a_emocao_nao_aumenta_a_intensidade(record):
    falas = [("paciente", "Estou preocupada."), ("paciente", "Fico preocupada."), ("paciente", "Estou preocupada.")]
    assert engine_for(record, falas).run().emocoes[0].intensidade == "baixa"  # antes: 3 falas = alta


def test_lexico_de_intensidade_e_dado_curado():
    lex = knowledge.intensity_lexicon()
    assert set(lex) == {"alta", "media", "baixa"} and "muito medo" in lex["alta"] and "pavor" in lex["alta"]


def test_cen6_card_leva_o_aviso(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    assert ins["emocoes"] and all(e["aviso"] == "sinal de comunicação, não diagnóstico" for e in ins["emocoes"])
    ansiedade = [e for e in ins["emocoes"] if e["emocao"] == "ansiedade"][0]
    assert ansiedade["intensidade"] == "baixa"  # "tenho medo de ter que tomar insulina": sem intensificador


# ---------- consentimento ----------
def test_sem_consentimento_de_emocoes_nao_ha_sinais(record):
    assert engine_for(record, [DUVIDA], extra={"sem_emocoes": True}).run().preocupacoes == []
    assert sinais.unanswered_concerns(segs([DUVIDA]), emotions_on=False) == []


def test_dispensar_a_duvida_a_tira_da_lista(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    item = next(c for c in ins["preocupacoes"] if not c["respondida"] and c["refs"] == ["Fala/18"])
    body = {"item_key": item["item_key"], "acao": "nao_se_aplica", "motivo": "Respondi fora da transcrição"}
    res = client.post(f"/consultations/{cid}/insight-actions", json=body, headers=h)
    assert res.status_code == 200, res.text
    novo = client.get(f"/consultations/{cid}", headers=h).json()["insights"]
    assert item["item_key"] not in [c["item_key"] for c in novo["preocupacoes"]]


# ---------- critério 3: internado -> passagem de plantão e alta ----------
def comm_alerts():
    with db_session.SessionLocal() as db:
        return list(db.scalars(select(Alert).where(Alert.source == "comunicacao")))


def test_internado_duvida_sem_resposta_aparece_na_passagem_e_na_alta(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen2")["id"]
    falas = [
        {"falante": "medico", "texto": "Bom dia, como o senhor passou a noite?", "t": 0.0},
        {
            "falante": "acompanhante",
            "texto": "Quando ele vai poder voltar para casa? Tenho medo de ele cair sozinho.",
            "t": 5.0,
        },
    ]
    say(client, h, cid, falas)
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    alerts = comm_alerts()
    assert len(alerts) == 2 and all(a.severity == "atencao" and a.source_refs for a in alerts)
    assert all("teach-back" in a.detail for a in alerts)
    assert not any("Tenho medo" in a.title or "Quando ele" in a.title for a in alerts)  # sem texto de fala
    ho = client.post("/patients/cen2/handoff", json={"audience": "medico"}, headers=h)
    assert ho.status_code == 200, ho.text
    content = ho.json()["content"]
    assert any(f["tipo"] == "preocupacao_nao_respondida" for f in content["fatos"])
    assert any("Preocupação sem resposta" in r["texto"] for r in content["riscos_proximas_horas"])
    assert any("teach-back" in p["texto"] for p in content["pendencias"])
    # Waze da alta: vira sugestão com teach-back, citando a mesma fonte
    from tests.test_discharge import RISKY

    checklist = RISKY
    res = client.post("/patients/cen2/discharge-assessment", json={"social_checklist": checklist}, headers=h)
    assert res.status_code == 200, res.text
    sug = [s for s in res.json()["sugestoes"] if "Responder à dúvida" in s["texto"]]
    assert sug and all("teach-back" in s["texto"] and s["refs"] for s in sug), res.json()["sugestoes"]


def test_consulta_respondida_nao_gera_alerta(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    falas = [
        {"falante": "acompanhante", "texto": "O potássio alto é grave?", "t": 1.0},
        {"falante": "medico", "texto": "O potássio alto precisa repetir o exame, mas não é grave agora.", "t": 5.0},
    ]
    say(client, h, cid, falas)
    client.post(f"/consultations/{cid}/finish", headers=h)
    assert comm_alerts() == []
