"""#43 verificado: entradas adversariais. O erro perigoso é dar a dúvida por respondida sem ter sido."""

import pytest
from sqlalchemy import select

from app import clock
from app.consult import privacidade
from app.consult.engine import Engine
from app.consult.schemas import Anamnese, Segment
from app.db import session as db_session
from app.db.models import Alert, AuditLog, Consultation
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from tests.conftest import CONSENT
from tests.test_consult import say, start
from tests.test_consult_sinais import comm_alerts, open_of, segs


def _engine(record, falas, pid="cen6") -> Engine:
    rec = record(pid)
    return Engine(rec, build_timeline(rec), build_context(rec), falas, "clinica_medica", "consulta", clock.now())


# ---------- falso "respondida" ----------
@pytest.mark.parametrize(
    ("duvida", "resposta"),
    [
        # o médico só repete a palavra, ou devolve a pergunta
        ("Vou ter que fazer hemodiálise?", "Hemodiálise? Vamos ver."),
        ("Posso tomar ibuprofeno com essa pressão?", "Qual ibuprofeno a senhora toma?"),
        ("Posso tomar ibuprofeno com essa pressão?", "Que ibuprofeno é esse, o da caixa azul?"),
        # adiar com o assunto na frase não é responder
        ("Posso operar a catarata com essa diabetes?", "Já explico a catarata depois, calma."),
        ("O potássio alto é grave?", "Sobre o potássio falo mais tarde."),
        # "não sei" e "não precisa" soltos não respondem
        ("O potássio alto é grave?", "Não sei dizer ainda."),
        ("Posso tomar dipirona?", "Não precisa falar sobre isso agora, deite aqui."),
        # "sim" não responde pergunta aberta
        ("O que é esse exame de sangue?", "Sim, sim, anotei."),
        # "calma" que muda de assunto não é acolhimento
        ("Estou com medo da cirurgia.", "Calma, vamos ao exame físico agora."),
    ],
)
def test_resposta_que_nao_responde_deixa_a_duvida_aberta(duvida, resposta):
    assert len(open_of([("paciente", duvida), ("medico", resposta)])) == 1


@pytest.mark.parametrize(
    ("duvida", "resposta"),
    [
        ("Posso operar a catarata?", "Pode, sim."),
        ("O que é esse exame de sangue?", "É o exame que mostra a média do açúcar no sangue."),
        ("Estou com medo da cirurgia.", "Entendo o medo. Vamos explicar cada passo da cirurgia."),
        ("Vou ter que fazer hemodiálise?", "Por enquanto não, a hemodiálise só entra se o rim piorar."),
        ("Posso tomar dipirona?", "Não precisa, o paracetamol resolve."),
    ],
)
def test_resposta_de_verdade_continua_valendo(duvida, resposta):
    assert open_of([("paciente", duvida), ("medico", resposta)]) == []


def test_acompanhante_e_paciente_nao_respondem_um_ao_outro():
    falas = [
        ("paciente", "O que é essa glicada?"),
        ("acompanhante", "Glicada é o açúcar, a glicada dela está alta."),
    ]
    assert len(open_of(falas)) == 1


def test_negacao_nao_vira_preocupacao():
    for frase in ("Não tenho medo de operar.", "Eu não estou preocupada com isso."):
        assert open_of([("paciente", frase)]) == [], frase


def test_emocao_do_acompanhante_nao_e_do_paciente(record):
    ins = _engine(record, segs([("acompanhante", "Eu tenho muito medo que ela caia.")])).run()
    assert [(e.quem, e.intensidade) for e in ins.emocoes] == [("acompanhante", "alta")]
    assert [c.quem for c in ins.preocupacoes] == ["acompanhante"]


def test_fala_do_profissional_nunca_vira_duvida():
    assert open_of([("medico", "O que o senhor sente? Está com medo?")]) == []


# ---------- outro idioma ----------
def _foreign(texto, traducao=None, idioma="es-MX"):
    return Segment(n=1, t=1.0, falante="paciente", texto=texto, idioma=idioma, traducao=traducao)


def test_intensidade_de_fala_traduzida_vale_pela_traducao(record):
    seg = _foreign("Tengo mucho miedo de operarme.", "Tenho muito medo de operar.")
    (e,) = _engine(record, [seg]).run().emocoes
    assert (e.emocao, e.intensidade) == ("ansiedade", "alta")


def test_intensidade_de_fala_sem_traducao_usa_os_sinonimos(record):
    (e,) = _engine(record, [_foreign("Tengo miedo de operarme.")]).run().emocoes
    assert e.emocao == "ansiedade" and e.intensidade == "baixa"


def test_merge_da_ia_le_a_fala_como_as_regras_leem(record):
    """A IA mescla sobre as falas cruas: fala em espanhol com tradução dá a mesma intensidade das regras."""
    from app.consult import ai

    seg = _foreign("Tengo mucho miedo de operarme.", "Tenho muito medo de operar.")
    base = _engine(record, [seg]).run()
    emocao = ai.AiEmotion(
        emocao="ansiedade", quem="paciente", intensidade="alta", trecho="t", abordagem="", refs=["Fala/1"]
    )
    out = ai.ConsultLiveOutput(
        resumo="x",
        destaques=[],
        queixas=[],
        emocoes=[emocao],
        perguntas=[],
        hipoteses=[],
        sugestoes_medicamentos=[],
        cirurgia=[],
        historico_relatado=[],
        anamnese=Anamnese(
            queixa_principal="",
            hda="",
            antecedentes=[],
            medicacoes_em_uso=[],
            alergias=[],
            habitos_e_contexto=[],
            revisao_de_sistemas=[],
            refs=[],
        ),
        falantes=[],
        perguntas_abordadas=[],
        alergias_relatadas=[],
        pedidos_ia=[],
        resumo_frases=[],
        relevancia=[],
    )
    merged = ai.merge_live(base, out, {"Fala/1"}, [seg])
    assert [e.intensidade for e in merged.emocoes] == ["alta"]


# ---------- consentimento, vazamento, descarte ----------
def test_consentimento_de_emocoes_desligado_nao_gera_sinal_nem_alerta(client, auth):
    h = auth("medico")
    fin = CONSENT["finalidades"] | {"emocoes": False}
    cid = start(client, h, "cen2", consentimento=CONSENT | {"finalidades": fin})["id"]
    ins = say(
        client,
        h,
        cid,
        [
            {"falante": "acompanhante", "texto": "Quando ele vai poder voltar para casa? Tenho muito medo.", "t": 1.0},
            {"falante": "medico", "texto": "Bom dia.", "t": 5.0},
        ],
    )
    assert ins["preocupacoes"] == [] and ins["emocoes"] == []
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    assert comm_alerts() == []


def test_fala_nao_vaza_para_alerta_auditoria_passagem_e_expurgo(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen2")["id"]
    frase = "Quando ele vai poder voltar para casa? Tenho medo de ele cair sozinho."
    say(client, h, cid, [{"falante": "acompanhante", "texto": frase, "t": 1.0}])
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    for a in comm_alerts():
        assert "Quando ele vai" not in a.title + a.detail and "cair sozinho" not in a.title + a.detail
    with db_session.SessionLocal() as db:
        audit = " ".join(str(r.payload_json) for r in db.scalars(select(AuditLog)))
        assert "Quando ele vai" not in audit and "cair sozinho" not in audit
    ho = client.post("/patients/cen2/handoff", json={"audience": "medico"}, headers=h).json()["content"]
    assert "Quando ele vai" not in str(ho) and "cair sozinho" not in str(ho)
    with db_session.SessionLocal() as db:
        c = db.get(Consultation, cid)
        privacidade.purge_consultation(db, c, 30)
        db.commit()
        db.refresh(c)
        dump = str(c.insights_json) + str(c.insight_actions_json) + str(c.extra_json) + str(c.ai_meta_json)
        assert "Quando ele vai" not in dump and "cair sozinho" not in dump
        assert all(p["termos"] == [] and p["trecho"] == "" for p in c.insights_json["preocupacoes"])


def test_descartada_nao_vira_alerta_e_o_assunto_some_no_expurgo(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen2")["id"]
    ins = say(client, h, cid, [{"falante": "acompanhante", "texto": "A hemodiálise vai ser necessária?", "t": 1.0}])
    item = ins["preocupacoes"][0]
    body = {"item_key": item["item_key"], "acao": "nao_se_aplica", "motivo": "Respondi fora da transcrição"}
    assert client.post(f"/consultations/{cid}/insight-actions", json=body, headers=h).status_code == 200
    client.post(f"/consultations/{cid}/finish", headers=h)
    assert comm_alerts() == []  # item descartado não vira alerta de passagem
    with db_session.SessionLocal() as db:
        c = db.get(Consultation, cid)
        privacidade.purge_consultation(db, c, 30)
        db.commit()
        db.refresh(c)
        assert "hemodialise" not in str(c.insight_actions_json).lower()


def test_duas_consultas_do_mesmo_encontro_nao_apagam_o_alerta_uma_da_outra(client, auth):
    h = auth("medico")
    a = start(client, h, "cen2")["id"]
    say(client, h, a, [{"falante": "acompanhante", "texto": "A hemodiálise vai ser necessária?", "t": 1.0}])
    assert client.post(f"/consultations/{a}/finish", headers=h).status_code == 200
    b = start(client, h, "cen2")["id"]
    say(client, h, b, [{"falante": "acompanhante", "texto": "Quando ele recebe a fisioterapia?", "t": 1.0}])
    assert client.post(f"/consultations/{b}/finish", headers=h).status_code == 200
    titulos = sorted(x.title for x in comm_alerts())
    assert len(titulos) == 2 and any("hemodialise" in t for t in titulos) and any("fisioterapia" in t for t in titulos)
    # finalizar de novo a mesma consulta não duplica
    assert client.post(f"/consultations/{b}/finish", headers=h).status_code == 200
    assert len(comm_alerts()) == 2


def test_alertas_de_comunicacao_so_do_paciente_da_consulta(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen2")["id"]
    say(client, h, cid, [{"falante": "acompanhante", "texto": "A hemodiálise vai ser necessária?", "t": 1.0}])
    client.post(f"/consultations/{cid}/finish", headers=h)
    with db_session.SessionLocal() as db:
        assert {a.patient_id for a in db.scalars(select(Alert).where(Alert.source == "comunicacao"))} == {"cen2"}
