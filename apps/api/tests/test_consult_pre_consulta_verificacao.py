"""#44 verificado: entradas adversariais do cartão "Antes de entrar"."""

import copy
from types import SimpleNamespace

import pytest

from app import clock
from app.consult import pre_consulta_service
from app.consult.chart import ChartView
from app.consult.pre_consulta import PreLine, PreResumo, Sources, build, mark_treated
from app.consult.pre_consulta_service import NUMBER
from app.consult.schemas import Segment
from app.fhir_client.client import LocalFhirStore, set_fhir
from app.seed.builder import build_all
from app.seed.scenarios import build_demo_resources, load_scenarios
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from app.timeline.record import PatientRecord
from tests.conftest import ANCHOR
from tests.test_consult import SCRIPT, say, start
from tests.test_consult_pre_consulta import card, lines
from tests.test_multiorg import cadastro_completo


def _chart(mexer=None, sid="cen6"):
    scen = copy.deepcopy(next(s for s in load_scenarios() if s["id"] == sid))
    if mexer:
        mexer(scen)
    store = LocalFhirStore.from_resources(build_all([scen], clock.now()))
    record = PatientRecord.load(store, sid)
    record.now = clock.now()
    return ChartView(record, build_timeline(record), build_context(record), clock.now())


def _seg(n, falante, texto):
    return Segment(n=n, t=float(n), falante=falante, texto=texto)


# ---------- organização, paciente sem histórico ----------
def test_paciente_avulso_de_outra_organizacao_da_404(client, auth):
    h2, _ = cadastro_completo(client)
    novo = client.post(
        "/patients",
        json={"nome": "Maria Souza Lima", "nascimento": "1980-01-02", "sexo": "F", "sem_alergias": True},
        headers=h2,
    )
    assert novo.status_code in (200, 201), novo.text
    pid = novo.json()["id"]
    assert client.get(f"/patients/{pid}/pre-consulta", headers=h2).status_code == 200
    assert client.get(f"/patients/{pid}/pre-consulta", headers=auth("medico")).status_code == 404
    assert (
        client.get(f"/patients/{pid}/pre-consulta", params={"resumo": "true"}, headers=auth("medico")).status_code
        == 404
    )


def test_paciente_sem_historico_devolve_cartao_vazio_sem_erro(client):
    h2, _ = cadastro_completo(client)
    pid = client.post(
        "/patients",
        json={"nome": "Joao Pereira Alves", "nascimento": "1990-05-05", "sexo": "M", "sem_alergias": True},
        headers=h2,
    ).json()["id"]
    c = client.get(f"/patients/{pid}/pre-consulta", params={"resumo": "true"}, headers=h2)
    assert c.status_code == 200, c.text
    body = c.json()
    assert body["mudou"] == [] and body["ultima_alta"] == [] and body["ultima_consulta"] is None
    assert body["resumo_ia"] == []


def test_consulta_de_outro_paciente_da_404(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen6")["id"]
    assert client.get("/patients/cen2/pre-consulta", params={"consulta": cid}, headers=h).status_code == 404
    assert client.get("/patients/cen6/pre-consulta", params={"consulta": 9999}, headers=h).status_code == 404


def test_paciente_inexistente_da_404(client, auth):
    assert client.get("/patients/nao-existe/pre-consulta", headers=auth("medico")).status_code == 404


# ---------- janela de remédios e exames ----------
def _nomes(pre, tipo):
    return " | ".join(x.texto for x in pre.mudou if x.tipo == tipo).lower()


def test_remedio_iniciado_antes_da_ultima_consulta_nao_entra():
    def mexer(scen):
        for p in scen["prescricoes"]:
            if p["id"] == "hidroclorotiazida":
                p["inicio"] = "-200d"  # começou antes da última consulta (-91d)
            if p["id"] == "sinvastatina":
                p["inicio"] = "-5d"  # começou depois dela

    chart = _chart(mexer)
    meds = _nomes(build(chart, "cen6", Sources()), "medicamento")
    assert (
        "hidroclorotiazida" not in meds.replace("suspenso", "")
        or "iniciado" not in meds.split("hidroclorotiazida")[1][:40]
    )
    assert "sinvastatina iniciado" in meds


def test_remedio_suspenso_antes_da_ultima_consulta_nao_entra():
    def mexer(scen):
        for p in scen["prescricoes"]:
            if p["id"] == "anlodipino":
                p["fim"] = "-200d"  # suspenso muito antes da última consulta

    meds = _nomes(build(_chart(mexer), "cen6", Sources()), "medicamento")
    assert "anlodipino" not in meds


def test_remedio_sem_data_de_inicio_nao_quebra_o_cartao():
    def mexer(scen):
        for p in scen["prescricoes"]:
            if p["id"] == "sinvastatina":
                p.pop("inicio", None)

    pre = build(_chart(mexer), "cen6", Sources())
    assert all(x.fonte for x in pre.mudou)


def test_encontro_sem_fuso_e_data_sem_hora_nao_quebram():
    chart = _chart()
    for e in chart.r.encounters.values():
        per = e.get("period", {})
        if per.get("start"):
            per["start"] = per["start"].replace("Z", "").split("+")[0]  # sem fuso
        if per.get("end"):
            per["end"] = per["end"][:10]  # só a data
    pre = build(chart, "cen6", Sources())
    assert pre.ultima_consulta and any(x.tipo == "exame" for x in pre.mudou)


def test_internacao_anterior_nao_faz_do_ambulatorial_internado_no_cartao():
    chart = _chart(
        lambda s: s.update(
            encontros_anteriores=[
                {"tipo": "internacao", "urgente": True, "inicio": "-40d", "duracao": "72h", "motivo": "Desidratação"}
            ]
        )
    )
    pre = build(chart, "cen6", Sources())
    assert pre.encounter_id == "cen6-amb-hoje"  # a consulta de hoje continua sendo o ambulatório
    stays = [x for x in pre.mudou if x.tipo == "internacao"]
    assert len(stays) == 1 and "3 dia(s)" in stays[0].texto and "em curso" not in stays[0].texto


def test_internacao_antes_da_ultima_consulta_nao_entra_no_que_mudou():
    chart = _chart(
        lambda s: s.update(
            encontros_anteriores=[
                {"tipo": "internacao", "urgente": True, "inicio": "-300d", "duracao": "48h", "motivo": "Pneumonia"}
            ]
        )
    )
    pre = build(chart, "cen6", Sources())
    assert not [x for x in pre.mudou if x.tipo == "internacao"]
    assert any("Pneumonia" in h.texto for h in chart.history())  # mas fica no histórico da consulta


# ---------- pendência tratada ----------
def _pend(texto):
    return PreLine(texto=texto, refs=["ServiceRequest/x"], fonte="f", tipo="pedido")


def test_pendencia_negada_pelo_paciente_nao_some():
    linha = _pend("Exame pedido sem resultado: Retinografia (fundo de olho) (em 12/07).")
    mark_treated([linha], [_seg(1, "paciente", "Não fiz a retinografia, o fundo de olho, ainda.")])
    assert not linha.tratada


def test_pendencia_citada_pelo_medico_some_mesmo_com_negacao_de_conduta():
    linha = _pend("Exame pedido sem resultado: Retinografia (fundo de olho) (em 12/07).")
    mark_treated([linha], [_seg(1, "medico", "Não precisa repetir a retinografia, o fundo de olho veio normal.")])
    assert linha.tratada and linha.tratada_refs == ["Fala/1"]


def test_termos_em_falas_diferentes_nao_somam_para_tratar():
    linha = _pend("Exame pedido sem resultado: Relação albumina/creatinina urinária (em 12/07).")
    segs = [_seg(1, "medico", "A creatinina subiu um pouco."), _seg(2, "medico", "A urina está normal.")]
    mark_treated([linha], segs)
    assert not linha.tratada


def test_pendencia_nao_some_com_assunto_so_generico_na_conversa(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, [{"falante": "medico", "texto": "Vamos pedir um exame de sangue hoje.", "t": 1.0}])
    c = card(client, h, consulta=cid)
    assert not any(x["tratada"] for x in c["pendencias"])


# ---------- resumo de 3 frases ----------
class _Fake:
    def __init__(self, frases):
        self.frases = frases

    def __call__(self, *_a, **_k):
        out = SimpleNamespace(frases=[PreResumo(texto=t, refs=r) for t, r in self.frases])
        return SimpleNamespace(output=out, mode="real")


def _resumo_real(monkeypatch, frases):
    monkeypatch.setattr(pre_consulta_service, "generate_structured", _Fake(frases))
    chart = _chart()
    pre = build(chart, "cen6", Sources())
    pre_consulta_service._resumo(pre, chart.r, "clinica_medica")
    return pre


def test_resumo_descarta_ref_inventada_numero_inventado_e_frase_sem_ref(monkeypatch):
    pre = _resumo_real(
        monkeypatch,
        [
            ("Creatinina subiu para 9,9.", ["Observation/inventada"]),
            ("Creatinina em 12,3 mg/dL.", ["Encounter/cen6-amb-hoje"]),
            ("Sem fonte nenhuma.", []),
            ("Tontura ao levantar.", ["Encounter/cen6-amb-hoje"]),
        ],
    )
    assert [f.texto for f in pre.resumo_ia] == ["Tontura ao levantar."] and pre.modo_resumo == "ia"


def test_numero_dentro_do_id_da_referencia_nao_vale_como_numero_do_cartao(monkeypatch):
    """O número tem de estar no texto do cartão, não em um id de recurso."""
    chart = _chart()
    pre = build(chart, "cen6", Sources())
    todas = pre.mudou + pre.pendencias + pre.motivo
    no_texto = set(NUMBER.findall(" ".join(x.texto for x in todas)))
    no_id = set(NUMBER.findall(" ".join(r for x in todas for r in x.refs)))
    so_no_id = sorted(no_id - no_texto)
    assert so_no_id, "o cen6 tem número em id que não está em texto"
    fake = _Fake([(f"Há {so_no_id[0]} pendências.", [pre.motivo[0].refs[0]])])
    monkeypatch.setattr(pre_consulta_service, "generate_structured", fake)
    pre_consulta_service._resumo(pre, chart.r, "clinica_medica")
    assert pre.modo_resumo == "regras"


def test_resumo_so_leva_mais_de_3_frases_cortado_em_3(monkeypatch):
    refs = ["Encounter/cen6-amb-hoje"]
    pre = _resumo_real(monkeypatch, [(f"Frase {w}.", refs) for w in "abcdef"])
    assert len(pre.resumo_ia) == 3


def test_resumo_sem_modulo_de_alta_nao_leva_a_alta(client):
    h2, _ = cadastro_completo(client)
    c = client.get("/patients/cen3/pre-consulta", params={"resumo": "true"}, headers=h2).json()
    assert c["ultima_alta"] == [] and "alta" not in " ".join(f["texto"].lower() for f in c["resumo_ia"])


def test_cartao_do_cen6_cita_so_refs_existentes(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT)
    c = card(client, h, consulta=cid, resumo="true")
    validos = {r for x in lines(c) for r in x["refs"]}
    assert all(set(f["refs"]) <= validos for f in c["resumo_ia"])


@pytest.fixture
def jstore(client):
    """FHIR local próprio do teste: a jornada escreve Task no prontuário e isso não pode vazar para os outros."""
    store = LocalFhirStore.from_resources(build_demo_resources(anchor=ANCHOR))
    set_fhir(store)
    return store


# ---------- Task: as tarefas vêm de JourneyTask (a fonte da verdade da jornada) ----------
def test_tarefa_de_jornada_aberta_aparece_com_fonte_e_some_ao_concluir(client, auth, jstore):
    h = auth("enfermagem")
    t = client.post("/journey/tasks", json={"patient_id": "cen7", "regra": "alta_sem_contato_72h"}, headers=h).json()
    pend = [x for x in card(client, h, "cen7")["pendencias"] if x["tipo"] == "tarefa"]
    assert len(pend) == 1 and pend[0]["fonte"] == "Jornada do paciente (HousIA)" and pend[0]["refs"]
    done = client.patch(f"/journey/tasks/{t['id']}", json={"status": "concluida", "observacao": "Feito."}, headers=h)
    assert done.status_code == 200
    assert not [x for x in card(client, h, "cen7")["pendencias"] if x["tipo"] == "tarefa"]


def test_tarefa_de_jornada_de_outra_organizacao_nao_aparece(client, auth, jstore):
    h = auth("enfermagem")
    client.post("/journey/tasks", json={"patient_id": "cen7", "regra": "alta_sem_contato_72h"}, headers=h)
    h2, _ = cadastro_completo(client)
    assert not [x for x in card(client, h2, "cen7")["pendencias"] if x["tipo"] == "tarefa"]
