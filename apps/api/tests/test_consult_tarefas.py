"""#47 — Pendências da consulta viram tarefas (FHIR Task) com responsável, prazo e ligação com passagem e alta."""

from datetime import datetime, timedelta

import pytest
from fhir.resources.R4B.task import Task
from sqlalchemy import select

from app import clock
from app.consult import tarefas
from app.consult.schemas import Insights, ProtocolItem, Segment
from app.db import session as db_session
from app.db.models import AuditLog, JourneyTask, User
from tests.test_consult import SCRIPT, say, start
from tests.test_discharge import GOOD
from tests.test_journey import jstore  # noqa: F401  (FHIR local próprio de cada teste)


def seg(n, falante, texto):
    return Segment(n=n, t=float(n), falante=falante, texto=texto)  # type: ignore[arg-type]


def propostas(falas, protocolos=()):
    segs = [seg(i, w, t) for i, (w, t) in enumerate(falas, 1)]
    return tarefas.extrair(segs, Insights.model_construct(protocolos=list(protocolos)), "cen6")


def audit(action):
    with db_session.SessionLocal() as db:
        return [r.payload_json for r in db.scalars(select(AuditLog).where(AuditLog.action == action))]


def finalizar(client, h, pid, falas, completo=False):
    cid = start(client, h, pid)["id"]
    if completo:
        say(client, h, cid, SCRIPT)
    say(client, h, cid, [{"falante": w, "texto": t, "t": 300.0 + i} for i, (w, t) in enumerate(falas)])
    res = client.post(f"/consultations/{cid}/finish", headers=h)
    assert res.status_code == 200, res.text
    return cid


def da_consulta(client, h, cid):
    res = client.get(f"/consultations/{cid}/tarefas", headers=h)
    assert res.status_code == 200, res.text
    return res.json()


# ---------- regras (unidade) ----------
def test_retorno_em_2_semanas_gera_proposta_com_prazo_e_a_fala():
    (p,) = propostas([("medico", "Vamos ajustar o remédio. Retorno em 2 semanas.")])
    assert p.regra == "consulta_retorno" and p.titulo == "Retorno em 2 semanas"
    assert p.horas == 14 * 24 and p.refs == ["Fala/1"] and not p.critica
    assert p.papel == "medico"


@pytest.mark.parametrize(
    ("fala", "horas"),
    [
        ("Volte daqui a 15 dias para reavaliar.", 15 * 24),
        ("Quero te ver de novo em um mês.", 30 * 24),
        ("Retorne na semana que vem com os exames.", 7 * 24),
        ("Reavalio em três dias.", 3 * 24),
    ],
)
def test_formas_de_dizer_o_retorno(fala, horas):
    (p,) = propostas([("medico", fala)])
    assert p.regra == "consulta_retorno" and p.horas == horas


@pytest.mark.parametrize(
    "fala",
    [
        "Se piorar, volte em 3 dias.",  # condicional: orientação ao paciente
        "Não precisa retornar tão cedo.",
        "Retorne se tiver febre.",  # sem prazo
        "A senhora volta em quanto tempo?",  # pergunta
        "Não vou pedir exame nenhum.",
    ],
)
def test_nao_vira_tarefa(fala):
    assert propostas([("medico", fala)]) == []


def test_so_fala_da_equipe_vale():
    assert (
        propostas([("paciente", "Retorno em 2 semanas, doutora."), ("acompanhante", "Vou pedir um hemograma.")]) == []
    )


def test_solicitar_varios_e_ligar_com_o_resultado_e_urgente():
    ps = propostas(
        [
            ("medico", "Vou pedir um hemograma e uma tomografia."),
            ("medico", "Eu ligo para a senhora com o resultado do hemograma em 3 dias."),
            ("medico", "Preciso solicitar um parecer da cardiologia, urgente."),
        ]
    )
    por_regra = {(p.regra, p.assunto): p for p in ps}
    assert {"consulta_solicitar"} == {p.regra for p in ps if p.regra != "consulta_ligar_resultado"}
    assert ("consulta_solicitar", "hemograma") in por_regra and ("consulta_solicitar", "tomografia") in por_regra
    ligar = por_regra[("consulta_ligar_resultado", "hemograma")]
    assert ligar.papel == "enfermagem" and ligar.horas == 72 and ligar.refs == ["Fala/2"]
    assert por_regra[("consulta_solicitar", "parecer da cardiologia")].critica


def test_mesma_fala_nao_duplica_e_protocolo_atrasado_vira_tarefa():
    prot = ProtocolItem(
        protocolo="Diabetes",
        item="Hemoglobina glicada",
        status="pendente",
        orientacao="Pedir HbA1c.",
        refs=["Observation/x"],
        id="dm-hba1c",
        fonte="SBD 2024",
        ultimo="há 8 meses",
    )
    risco = prot.model_copy(update={"status": "critico", "id": "dm-risco", "item": "Ideação"})
    ps = propostas([("medico", "Retorno em 2 semanas. Retorno em 2 semanas.")], [prot, risco])
    assert [p.regra for p in ps].count("consulta_retorno") == 1
    dm = next(p for p in ps if p.chave == "protocolo:dm-hba1c")
    assert dm.refs == ["Observation/x"] and not dm.critica and "SBD 2024" in dm.detalhe
    assert next(p for p in ps if p.chave == "protocolo:dm-risco").critica


# ---------- critério 1: 'retorno em 2 semanas' gera uma Task com prazo ----------
def test_retorno_em_2_semanas_vira_task_fhir_com_responsavel_prazo_focus_e_fala(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    cid = finalizar(client, h, "cen6", [("medico", "Vamos ajustar o remédio. Retorno em 2 semanas.")], completo=True)
    lista = da_consulta(client, h, cid)
    (t,) = [x for x in lista["tarefas"] if x["regra"] == "consulta_retorno"]
    assert t["titulo"] == "Retorno em 2 semanas" and t["papel"] == "medico" and t["status"] == "aberta"
    assert t["consulta_id"] == cid and t["origem"] == "consulta" and not t["critica"]
    prazo = datetime.fromisoformat(t["prazo"])
    criada = datetime.fromisoformat(t["criada_em"])
    assert timedelta(days=13, hours=23) < prazo - criada < timedelta(days=14, hours=1)
    assert any(r.startswith("Fala/") for r in t["fontes"])
    # o FHIR Task válido: papel, prazo, focus no encontro e a fala como motivo
    res = jstore.get("Task", t["fhir_ref"].split("/")[1])
    Task.model_validate(res)
    assert res["performerType"][0]["coding"][0]["code"] == "medico"
    assert res["restriction"]["period"]["end"] == t["prazo"]
    assert res["focus"]["reference"].startswith("Encounter/") and res["for"]["reference"] == "Patient/cen6"
    assert f"Consulta #{cid}" in res["reasonCode"]["text"] and "Fala/" in res["reasonCode"]["text"]
    assert any(i.get("valueString", "").startswith("Fala/") for i in res["input"])
    assert res["priority"] == "routine"
    # a tarefa também está na lista geral da jornada
    geral = client.get("/journey/tasks", params={"consulta": cid}, headers=h).json()
    assert [x["id"] for x in geral] == [x["id"] for x in lista["tarefas"]]
    assert audit("jornada.tarefa_criada")[0]["consulta"] == cid


def test_o_texto_da_fala_nao_vai_para_a_tarefa_nem_para_a_auditoria(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    cid = finalizar(client, h, "cen6", [("medico", "Dona Joana, retorno em 2 semanas por causa da tontura.")])
    blob = str(da_consulta(client, h, cid)) + str(audit("jornada.tarefa_criada"))
    assert "Dona Joana" not in blob and "tontura" not in blob


def test_finalizar_nao_duplica_e_sincronizar_de_novo_e_idempotente(client, auth, jstore):  # noqa: F811
    from app.consult import service, tarefas_service
    from app.db.models import Consultation

    h = auth("medico")
    cid = finalizar(client, h, "cen6", [("medico", "Retorno em 2 semanas.")])
    antes = da_consulta(client, h, cid)["tarefas"]
    with db_session.SessionLocal() as db:
        c = db.get(Consultation, cid)
        segs = service._segments(c)
        user = db.scalars(select(User)).first()
        out = tarefas_service.sincronizar(db, jstore, user, c, segs, Insights.model_construct(protocolos=[]))
        db.commit()
    assert out["criadas"] == 0
    assert [t["id"] for t in da_consulta(client, h, cid)["tarefas"]] == [t["id"] for t in antes]


# ---------- tarefa crítica: não some nem se conclui sem justificativa ----------
def test_tarefa_critica_exige_justificativa_para_concluir_e_o_fechamento_e_auditado(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    cid = finalizar(client, h, "cen6", [("medico", "Vou pedir uma tomografia urgente.")])
    (t,) = [x for x in da_consulta(client, h, cid)["tarefas"] if x["regra"] == "consulta_solicitar"]
    assert t["critica"] and jstore.get("Task", t["fhir_ref"].split("/")[1])["priority"] == "urgent"
    sem = client.patch(f"/journey/tasks/{t['id']}", json={"status": "concluida"}, headers=h)
    assert sem.status_code == 422 and "Tarefa crítica" in sem.json()["detail"]
    curta = client.patch(f"/journey/tasks/{t['id']}", json={"status": "concluida", "observacao": "ok"}, headers=h)
    assert curta.status_code == 422
    assert da_consulta(client, h, cid)["criticas_abertas"] == 1
    ok = client.patch(
        f"/journey/tasks/{t['id']}",
        json={"status": "concluida", "observacao": "Tomografia solicitada e agendada para sexta."},
        headers=h,
    )
    assert ok.status_code == 200 and ok.json()["status"] == "concluida"
    assert jstore.get("Task", t["fhir_ref"].split("/")[1])["status"] == "completed"
    (linha,) = audit("jornada.tarefa_concluida")
    assert linha["critica"] is True and linha["consulta"] == cid and linha["com_justificativa"] is True
    assert "Tomografia" not in str(linha)  # o texto da justificativa não vai para a auditoria
    with db_session.SessionLocal() as db:
        fechada = db.get(JourneyTask, t["id"])
        assert fechada.closed_by and fechada.closing_note.startswith("Tomografia")


def test_fechar_tarefa_comum_registra_na_auditoria(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    cid = finalizar(client, h, "cen6", [("medico", "Retorno em 1 mês.")])
    (t,) = [x for x in da_consulta(client, h, cid)["tarefas"] if x["regra"] == "consulta_retorno"]
    assert client.patch(f"/journey/tasks/{t['id']}", json={"status": "concluida"}, headers=h).status_code == 200
    (linha,) = audit("jornada.tarefa_concluida")
    assert linha["regra"] == "consulta_retorno" and linha["critica"] is False and linha["fhir"] == t["fhir_ref"]
    cancelar = client.patch(f"/journey/tasks/{t['id']}", json={"status": "cancelada"}, headers=h)
    assert cancelar.status_code == 409  # já encerrada


# ---------- edição na revisão ----------
def test_editar_papel_prazo_e_titulo_atualiza_o_task_e_audita(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    cid = finalizar(client, h, "cen6", [("medico", "Retorno em 2 semanas.")])
    (t,) = da_consulta(client, h, cid)["tarefas"]
    novo = (datetime.fromisoformat(t["prazo"]) + timedelta(days=3)).isoformat()
    res = client.put(
        f"/journey/tasks/{t['id']}",
        json={"papel": "enfermagem", "prazo": novo, "titulo": "Agendar o retorno da paciente"},
        headers=h,
    )
    assert res.status_code == 200, res.text
    assert res.json()["papel"] == "enfermagem" and res.json()["titulo"] == "Agendar o retorno da paciente"
    fhir = jstore.get("Task", t["fhir_ref"].split("/")[1])
    assert fhir["performerType"][0]["coding"][0]["code"] == "enfermagem"
    assert fhir["restriction"]["period"]["end"] == res.json()["prazo"]
    assert fhir["code"]["text"] == "Agendar o retorno da paciente"
    assert fhir["focus"]["reference"].startswith("Encounter/") and fhir["status"] == "requested"
    (linha,) = audit("jornada.tarefa_editada")
    assert set(linha["campos"]) == {"titulo", "papel", "prazo"} and "Agendar" not in str(linha)


def test_tirar_a_marca_de_critica_exige_justificativa(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    cid = finalizar(client, h, "cen6", [("medico", "Vou pedir uma tomografia urgente.")])
    (t,) = [x for x in da_consulta(client, h, cid)["tarefas"] if x["critica"]]
    assert client.put(f"/journey/tasks/{t['id']}", json={"critica": False}, headers=h).status_code == 422
    ok = client.put(
        f"/journey/tasks/{t['id']}",
        json={"critica": False, "justificativa": "Paciente estável; exame de rotina."},
        headers=h,
    )
    assert ok.status_code == 200 and ok.json()["critica"] is False
    assert audit("jornada.tarefa_editada")[0]["justificativa"].startswith("Paciente")


def test_profissional_acrescenta_pendencia_na_revisao(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    cid = finalizar(client, h, "cen6", [("medico", "Retorno em 2 semanas.")])
    res = client.post(
        f"/consultations/{cid}/tarefas",
        json={"titulo": "Pedir o laudo do ecocardiograma", "papel": "enfermagem", "prazo_horas": 48, "critica": True},
        headers=h,
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["critica"] and body["papel"] == "enfermagem" and body["fhir_ref"]
    assert audit("jornada.tarefa_criada")[-1]["origem"] == "profissional"
    assert da_consulta(client, h, cid)["criticas_abertas"] == 1
    assert client.post(f"/consultations/{cid}/tarefas", json={"titulo": "x"}, headers=h).status_code == 422


# ---------- critério 2: passagem de plantão e alta (trava suave) ----------
FALAS_INTERNADO = [
    ("medico", "Bom dia, como o senhor passou a noite?"),
    ("medico", "Vou pedir uma tomografia urgente. Retorno em 2 semanas."),
]


def test_tarefa_aberta_aparece_na_passagem_de_plantao(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    finalizar(client, h, "cen2", FALAS_INTERNADO)
    ho = client.post("/patients/cen2/handoff", json={"audience": "enfermagem"}, headers=h)
    assert ho.status_code == 200, ho.text
    content = ho.json()["content"]
    fatos = [f for f in content["fatos"] if f["tipo"] == "tarefa_aberta"]
    assert len(fatos) == 2 and all(f["refs"] for f in fatos)
    critica = next(f for f in fatos if "tomografia" in f["texto"])
    assert critica["severidade"] == "critico" and "responsável" in critica["texto"] and "prazo" in critica["texto"]
    assert any("Tarefa aberta" in r["texto"] for r in content["riscos_proximas_horas"])
    assert any("tomografia" in p["texto"] for p in content["pendencias"])
    # a tarefa não vira alerta da passagem (já é uma tarefa com dono)
    assert not any(
        "Tarefa aberta" in a["title"]
        for a in client.get("/patients/cen2/alerts", headers=h).json()
        if a["source"] == "handoff"
    )


def test_omissao_da_tarefa_critica_na_passagem_verbal_aparece(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    finalizar(client, h, "cen2", FALAS_INTERNADO)
    texto = "Paciente tranquilo, comeu bem, sem queixas durante a noite, aceitou a dieta e dormiu."
    res = client.post("/patients/cen2/handoff/check", json={"audience": "medico", "texto": texto}, headers=h)
    assert res.status_code == 200, res.text
    omit = [i for i in res.json()["itens"] if "tomografia" in i["fato"].lower()]
    assert omit and omit[0]["status"] == "omitido" and omit[0]["severidade"] == "critico" and omit[0]["refs"]
    citado = "Pendente a tomografia pedida na consulta, a tarefa está aberta."
    res = client.post("/patients/cen2/handoff/check", json={"audience": "medico", "texto": citado}, headers=h)
    item = next(i for i in res.json()["itens"] if "tomografia" in i["fato"].lower())
    assert item["status"] == "mencionado"


def test_tarefa_critica_aberta_aparece_na_alta_e_ativa_a_trava_suave(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    finalizar(client, h, "cen2", FALAS_INTERNADO)
    a = client.post("/patients/cen2/discharge-assessment", json={"social_checklist": GOOD}, headers=h).json()
    assert a["final_risk"] != "alto"  # a trava vem da tarefa, não do risco
    assert a["trava"]["ativa"] and a["trava"]["por_tarefas"] and "tarefa" in a["trava"]["motivo"]
    assert len(a["tarefas"]["criticas"]) == 1 and len(a["tarefas"]["abertas"]) == 2
    sug = [s for s in a["sugestoes"] if "Resolver a pendência" in s["texto"]]
    assert len(sug) == 2 and all(s["refs"] for s in sug)
    assert next(s for s in sug if "tomografia" in s["texto"])["prioridade"] == "alta"
    # a confirmação fica bloqueada
    blocked = client.post(f"/discharge-assessments/{a['id']}/confirm", headers=h)
    assert blocked.status_code == 409 and "tarefa" in blocked.json()["detail"]
    assert audit("discharge.confirmacao_bloqueada")[0]["tarefas_criticas"] == a["tarefas"]["criticas"]
    # trava suave: o override com justificativa libera e fica na auditoria
    curta = client.post(f"/discharge-assessments/{a['id']}/override", json={"justification": "curta"}, headers=h)
    assert curta.status_code == 422
    text = "Tomografia será feita ambulatorialmente em 5 dias, paciente e família orientados."
    ov = client.post(f"/discharge-assessments/{a['id']}/override", json={"justification": text}, headers=h)
    assert ov.status_code == 200 and not ov.json()["trava"]["ativa"]
    assert client.post(f"/discharge-assessments/{a['id']}/confirm", headers=h).status_code == 200
    assert audit("discharge.override")[0]["tarefas_criticas"]


def test_concluir_a_tarefa_critica_com_justificativa_destrava_a_alta(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    finalizar(client, h, "cen2", FALAS_INTERNADO)
    a = client.post("/patients/cen2/discharge-assessment", json={"social_checklist": GOOD}, headers=h).json()
    tid = a["tarefas"]["criticas"][0]
    done = client.patch(
        f"/journey/tasks/{tid}",
        json={"status": "concluida", "observacao": "Tomografia feita e laudada, sem achados."},
        headers=h,
    )
    assert done.status_code == 200
    assert client.post(f"/discharge-assessments/{a['id']}/confirm", headers=h).status_code == 200


def test_sem_tarefa_critica_a_alta_nao_trava_e_override_nao_se_aplica(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    a = client.post("/patients/cen4/discharge-assessment", json={"social_checklist": GOOD}, headers=h).json()
    assert not a["trava"]["ativa"] and a["tarefas"]["criticas"] == []
    ov = client.post(
        f"/discharge-assessments/{a['id']}/override",
        json={"justification": "Justificativa longa o bastante para passar."},
        headers=h,
    )
    assert ov.status_code == 409


# ---------- limites: sandbox, outra organização, LGPD, cartão "Antes de entrar" ----------
def test_consulta_de_sandbox_nao_gera_tarefa(client, auth, jstore):  # noqa: F811
    from tests.test_multiorg import cadastro_completo

    h, _ = cadastro_completo(client)
    cid = start(client, h, "cen6", sandbox=True)["id"]
    say(client, h, cid, [{"falante": "medico", "texto": "Retorno em 2 semanas.", "t": 1.0}])
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    assert da_consulta(client, h, cid)["tarefas"] == []
    assert client.post(f"/consultations/{cid}/tarefas", json={"titulo": "Qualquer coisa"}, headers=h).status_code == 409


def test_outra_organizacao_nao_ve_nem_edita_a_tarefa(client, auth, jstore):  # noqa: F811
    from tests.test_multiorg import cadastro_completo

    h = auth("medico")
    cid = finalizar(client, h, "cen6", [("medico", "Retorno em 2 semanas.")])
    (t,) = da_consulta(client, h, cid)["tarefas"]
    hb, _ = cadastro_completo(client)
    assert client.get(f"/consultations/{cid}/tarefas", headers=hb).status_code == 404
    assert client.put(f"/journey/tasks/{t['id']}", json={"papel": "gestor"}, headers=hb).status_code == 404
    assert client.patch(f"/journey/tasks/{t['id']}", json={"status": "concluida"}, headers=hb).status_code == 404
    assert client.get("/journey/tasks", headers=hb).json() == []


def test_excluir_dados_do_paciente_apaga_as_tarefas_da_consulta(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    finalizar(client, h, "cen6", [("medico", "Retorno em 2 semanas.")])
    res = client.delete("/patients/cen6/consultation-data", headers=auth("gestor"))
    assert res.status_code == 200
    with db_session.SessionLocal() as db:
        assert db.scalars(select(JourneyTask).where(JourneyTask.consultation_id.isnot(None))).all() == []
    assert audit("consulta.dados_excluidos")[0]["tarefas"] >= 1


def test_cartao_antes_de_entrar_lista_a_tarefa_sem_a_fala_de_outra_consulta(client, auth, jstore):  # noqa: F811
    h = auth("medico")
    finalizar(client, h, "cen6", [("medico", "Retorno em 2 semanas.")])
    card = client.get("/patients/cen6/pre-consulta", headers=h).json()
    linhas = [x for x in card["pendencias"] if x["tipo"] == "tarefa"]
    assert linhas and all(not r.startswith("Fala/") for x in linhas for r in x["refs"])


def test_o_loader_do_fhir_nao_apaga_a_task_da_jornada(client, auth, jstore):  # noqa: F811
    """Decisão do #44: Task fica fora de PATIENT_SCOPED, senão o remove_stale apagaria as Tasks criadas aqui."""
    from app.fhir_client.client import PATIENT_SCOPED

    assert "Task" not in PATIENT_SCOPED
    h = auth("medico")
    cid = finalizar(client, h, "cen6", [("medico", "Retorno em 2 semanas.")])
    (t,) = da_consulta(client, h, cid)["tarefas"]
    assert jstore.get("Task", t["fhir_ref"].split("/")[1]) is not None
    assert clock.now() is not None
