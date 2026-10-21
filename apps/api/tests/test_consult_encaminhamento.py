"""Smart App "Antes de encaminhar" (#29): critérios, exames, manejo na APS, ServiceRequest em rascunho e métrica.

O motor roda sobre cópias do prontuário do cen6 (sexo trocado, CID e PSA acrescentados); a API usa um paciente
avulso sintético com o diagnóstico gravado no FHIR local. LLM_MODE=mock.
"""

import csv
import json
from copy import deepcopy
from datetime import timedelta

import pytest
from fhir.resources.R4B.servicerequest import ServiceRequest
from sqlalchemy import select

from app import clock
from app.config import get_settings
from app.consult import encaminhamento, knowledge
from app.consult import service as consult_service
from app.consult.engine import Engine
from app.consult.schemas import Segment
from app.db import session as db_session
from app.db.models import AuditLog
from app.fhir_client import codes
from app.fhir_client.client import get_fhir
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from tests.conftest import ANCHOR, CONSENT, login

ACTIVE = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]}
VIRO = "Pelo que o senhor conta, vou te encaminhar para o urologista."


@pytest.fixture(autouse=True)
def _fresh():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def condition(pid: str, code: str, text: str, cid="cond-enc") -> dict:
    return {
        "resourceType": "Condition",
        "id": cid,
        "clinicalStatus": ACTIVE,
        "code": {"coding": [{"system": codes.ICD10, "code": code, "display": text}], "text": text},
        "subject": {"reference": f"Patient/{pid}"},
        "onsetDateTime": (ANCHOR - timedelta(days=900)).isoformat(),
    }


def lab(pid: str, key: str, value: float, days_ago: int = 30, rid: str | None = None) -> dict:
    m = codes.LABS[key]
    return {
        "resourceType": "Observation",
        "id": rid or f"{key}-{pid}",
        "status": "final",
        "category": [{"coding": [{"system": codes.OBS_CATEGORY, "code": "laboratory", "display": "Laboratório"}]}],
        "code": {"coding": [{"system": m.system, "code": m.code, "display": m.label}], "text": m.label},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": (ANCHOR - timedelta(days=days_ago)).isoformat(),
        "valueQuantity": {"value": value, "unit": m.unit},
    }


def psa(pid: str, value: float, days_ago: int = 30) -> dict:
    return lab(pid, "psa", value, days_ago, rid=f"psa-{pid}")


def urina_tipo_1(pid: str) -> dict:
    return {
        "resourceType": "Procedure",
        "id": f"eas-{pid}",
        "status": "completed",
        "code": {"text": "Urina tipo 1 (EAS)"},
        "subject": {"reference": f"Patient/{pid}"},
        "performedDateTime": (ANCHOR - timedelta(days=20)).isoformat(),
    }


def engine(record, falas, *, male=True, add=(), patient="cen6") -> Engine:
    rec = record(patient)
    rec.patient = deepcopy(rec.patient)
    if male:
        rec.patient["gender"] = "male"
    rec.resources = {k: list(v) for k, v in rec.resources.items()}
    for r in add:
        rec.resources.setdefault(r["resourceType"], []).append(r)
    segs = [Segment(n=i, t=float(i), falante=who, texto=t) for i, (who, t) in enumerate(falas, 1)]  # type: ignore[arg-type]
    return Engine(rec, build_timeline(rec), build_context(rec), segs, "clinica_geral", "consulta", clock.now(), None)


def cards(eng: Engine, **filt):
    return [e for e in eng.run().encaminhamentos if all(getattr(e, k) == v for k, v in filt.items())]


def hpb(pid="cen6", **kw):
    return [condition(pid, "N40", "Hiperplasia prostática benigna", "cond-n40"), *kw.get("extra", [])]


# ---------- base curada ----------
def test_base_has_at_least_10_specialties_each_with_source_and_valid_rules():
    rows = knowledge.referrals()
    assert len({r.specialty for r in rows}) >= 10
    cid10 = {
        r["codigo"] for r in csv.DictReader(open(get_settings().data_dir / "reference" / "cid10.csv", encoding="utf-8"))
    }
    assert len({r.id for r in rows}) == len(rows)
    for r in rows:
        assert "Ministério da Saúde" in r.source and "TelessaúdeRS" in r.source, r.id
        assert r.criteria and r.exams and r.management and r.urgency and r.teleconsulting, r.id
        assert r.specialty_terms and r.icd, r.id
        for prefix in r.icd:
            assert any(c.startswith(prefix) for c in cid10), (r.id, prefix)  # só CID-10 real
        for _, rule in r.criteria:
            kind = rule.partition(":")[0]
            assert rule == "manual" or rule.startswith("tfg<") or kind in ("lab", "fala"), (r.id, rule)
            if kind == "lab":
                assert rule[4:].split("<")[0].split(">")[0] in codes.LABS, (r.id, rule)
        for _, src, days in r.exams:
            assert src.partition(":")[0] in ("lab", "texto") and days > 0, (r.id, src)
            if src.startswith("lab:"):
                assert src[4:] in codes.LABS, (r.id, src)


# ---------- critério 1: HPB sem PSA mostra o PSA como faltando ----------
def test_vou_te_encaminhar_para_o_urologista_em_hpb_sem_psa_mostra_psa_faltando(record):
    eng = engine(record, [("medico", VIRO)], add=hpb())
    (card,) = cards(eng, id="uro_hpb")
    assert card.gatilho == "fala" and card.especialidade == "Urologia"
    exam = {e.exame: e for e in card.exames}
    assert exam["PSA"].status == "faltando"
    assert "PSA" in card.exames_faltantes and "PSA" in card.recomendacao
    assert "Fala/1" in card.refs and "Condition/cond-n40" in card.refs
    assert card.manejo_aps and card.sinais_urgencia and card.teleconsultoria and card.fonte
    assert any(k.status == "confirmar" and "PSA" in k.evidencia for k in card.criterios)  # critério de PSA sem dado


def test_psa_alto_recente_atende_o_criterio_e_completa_o_exame(record):
    eng = engine(record, [("medico", VIRO)], add=hpb(extra=[psa("cen6", 9.4)]))
    (card,) = cards(eng, id="uro_hpb")
    assert {e.exame: e.status for e in card.exames}["PSA"] == "feito"
    crit = next(k for k in card.criterios if k.texto.startswith("PSA acima"))
    assert crit.status == "atendido" and "9,4" in crit.evidencia and crit.refs == ["Observation/psa-cen6"]
    assert "PSA" not in card.exames_faltantes
    assert "Observation/psa-cen6" in card.refs


def test_psa_antigo_fica_desatualizado_e_conta_como_faltante(record):
    eng = engine(record, [("medico", VIRO)], add=hpb(extra=[psa("cen6", 2.0, days_ago=700)]))
    (card,) = cards(eng, id="uro_hpb")
    assert {e.exame: e.status for e in card.exames}["PSA"] == "desatualizado"
    assert any(f.startswith("PSA (desatualizado") for f in card.exames_faltantes)
    crit = next(k for k in card.criterios if k.texto.startswith("PSA acima"))
    assert crit.status == "nao_atendido"  # o valor antigo, 2,0, não passa de 4


# ---------- critério 2: ServiceRequest em rascunho com justificativa e refs ----------
def test_rascunho_e_service_request_valido_com_justificativa_estruturada_e_refs(record):
    eng = engine(record, [("medico", VIRO)], add=hpb(extra=[psa("cen6", 9.4)]))
    (card,) = cards(eng, id="uro_hpb")
    sr = card.rascunho
    ServiceRequest.model_validate(sr)  # FHIR R4B válido
    assert sr["status"] == "draft" and sr["intent"] == "proposal"
    assert sr["code"]["text"] == "Encaminhamento para Urologia" and sr["performerType"]["text"] == "Urologia"
    refs = {i["reference"] for i in sr["supportingInfo"]}
    assert {"Condition/cond-n40", "Observation/psa-cen6"} <= refs
    assert not any(r.startswith("Fala/") for r in refs)  # Fala/N vai na extensão e na nota, não como referência FHIR
    note = sr["note"][0]["text"]
    assert "Condição: Hiperplasia benigna da próstata" in note and "PSA" in note and "Exames mínimos pendentes" in note
    assert "Fala/1" in sr["extension"][0]["valueString"]
    assert sr["reasonCode"][0]["text"].startswith("Hiperplasia benigna da próstata")


# ---------- critério 4: condição sem critério não abre o card ----------
@pytest.mark.parametrize(
    "texto",
    [
        "Vou te encaminhar para o podólogo.",  # especialidade sem linha na base
        "Vou te encaminhar para o fonoaudiólogo.",
        "Vou te encaminhar para a nutricionista.",
        "Bom dia, tudo bem com o senhor?",
    ],
)
def test_especialidade_sem_criterio_na_base_nao_abre_card(record, texto):
    assert engine(record, [("medico", texto)], add=hpb()).run().encaminhamentos == []


def test_especialidade_dita_sem_condicao_do_paciente_nao_abre_card(record):
    # mulher do cen6 (DM2, HAS, DRC) sem próstata nem tema urológico na conversa
    assert engine(record, [("medico", VIRO)], male=False).run().encaminhamentos == []


# ---------- falsos positivos: 14 falas adversariais ----------
ADVERSARIAS = [
    ("medico", "Não vou encaminhar o senhor para o urologista agora."),
    ("medico", "Não precisa encaminhar para o urologista, a gente resolve aqui."),
    ("medico", "Se piorar eu encaminho o senhor para o urologista."),
    ("medico", "Caso o PSA venha alto, vou te encaminhar para o urologista."),
    ("medico", "Talvez eu precise encaminhar para o urologista."),
    ("medico", "O senhor quer que eu encaminhe para o urologista?"),
    ("medico", "Será que devo encaminhar para o urologista?"),
    ("paciente", "O doutor vai me encaminhar para o urologista."),
    ("paciente", "Vou te encaminhar para o urologista."),
    ("medico", "O senhor já foi encaminhado para o urologista no ano passado."),
    ("medico", "Meu primo é urologista, vou te passar o contato dele."),
    ("medico", "Vamos conversar sobre a próstata e o jato fraco."),
    ("medico", "Vou pedir o PSA, depois a gente vê se encaminha."),
    ("acompanhante", "Ele disse que vai encaminhar para o urologista."),
    ("medico", "Vou te encaminhar para o laboratório fazer o PSA."),
]


@pytest.mark.parametrize("falante,texto", ADVERSARIAS)
def test_falas_que_nao_decidem_encaminhar_nao_abrem_card(record, falante, texto):
    assert engine(record, [(falante, texto)], add=hpb()).run().encaminhamentos == []


@pytest.mark.parametrize(
    "texto",
    [
        "Vou encaminhar o senhor para a urologia.",
        "vou te mandar pro urologista",  # sem pontuação, sem acento
        "Precisamos encaminhar o senhor ao urologista, tá bom?",
        "Eu encaminho o senhor para o Urologista.",
        "Doutor, então vou te encaminhar pra urologia sim.",
    ],
)
def test_variacoes_do_profissional_abrem_o_card(record, texto):
    assert [e.id for e in engine(record, [("medico", texto)], add=hpb()).run().encaminhamentos] == ["uro_hpb"]


def test_tema_dito_na_conversa_basta_sem_diagnostico_no_prontuario(record):
    falas = [("paciente", "Doutor, o jato da urina está fraco e levanto a noite para urinar."), ("medico", VIRO)]
    assert [e.id for e in engine(record, falas).run().encaminhamentos] == ["uro_hpb"]


def test_cardiologia_so_abre_as_linhas_que_valem_para_o_paciente(record):
    eng = engine(record, [("medico", "Vou te encaminhar para o cardiologista.")], male=False)
    assert [e.id for e in eng.run().encaminhamentos] == ["cardio_has"]  # HAS no prontuário; IC sem diagnóstico nem fala


def test_nefrologia_usa_a_tfg_estimada_do_prontuario(record):
    eng = engine(record, [("medico", "Vou te encaminhar para o nefrologista.")], male=False)
    (card,) = [e for e in eng.run().encaminhamentos if e.id == "nefro_drc"]
    tfg = next(k for k in card.criterios if k.texto.startswith("TFG"))
    assert tfg.status in ("atendido", "nao_atendido") and "TFG estimada" in tfg.evidencia and tfg.refs


# ---------- gatilho por hipótese ----------
def test_hipotese_com_criterio_atendido_abre_o_card_sem_fala(record):
    from app.consult.schemas import Hypothesis

    eng = engine(record, [("medico", "Bom dia.")], male=False)
    base = eng.run()
    hip = Hypothesis(
        hipotese="Diabetes descompensado",
        cid="E11.9",
        confianca="alta",
        a_favor=[],
        contra=[],
        proximos_passos="",
        sinais_de_alarme="",
        refs=["Condition/x"],
    )
    # HbA1c do cen6 acima de 9? usa o dado real; se não passar de 9, o card não abre (sem critério atendido)
    got = encaminhamento.build(eng.chart, eng.segments, [hip], {})
    last = eng.chart.latest_lab("hba1c", max_days=3650)
    assert base.encaminhamentos == []  # nenhuma fala, nenhuma hipótese do motor com critério
    if last is not None and float(last.value["valor"]) > 9:
        assert [e.id for e in got] == ["endo_dm"] and got[0].gatilho == "hipotese" and "E11.9" in got[0].gatilho_texto
    else:
        assert all(e.gatilho == "hipotese" for e in got)


def test_hipotese_descartada_ou_sem_criterio_nao_abre(record):
    from app.consult.schemas import Hypothesis

    eng = engine(record, [("medico", "Bom dia.")], male=False)
    hip = Hypothesis(
        hipotese="Cefaleia",
        cid="R51",
        confianca="media",
        a_favor=[],
        contra=[],
        proximos_passos="",
        sinais_de_alarme="",
        refs=[],
    )
    assert encaminhamento.build(eng.chart, eng.segments, [hip], {}) == []  # critérios de neurologia: nenhum atendido
    descartada = hip.model_copy(update={"decisao": "descartada"})
    assert encaminhamento.build(eng.chart, eng.segments, [descartada], {}) == []


# ---------- API: decisões, auditoria, exportação e métrica ----------
def paciente_hpb(client, h, com_psa: bool = False) -> str:
    novo = client.post(
        "/patients",
        json={"nome": "Jorge Teste Prado", "nascimento": "1955-05-02", "sexo": "M"},
        headers=h,
    )
    assert novo.status_code == 201, novo.text
    pid = novo.json()["id"]
    fhir = get_fhir()
    fhir.update(condition(pid, "N40", "Hiperplasia prostática benigna", f"{pid}-n40"))
    if com_psa:  # os três exames mínimos da urologia: PSA, creatinina e urina tipo 1
        for r in (psa(pid, 6.1), lab(pid, "creatinina", 1.0), urina_tipo_1(pid)):
            fhir.update(r)
    consult_service.clear_chart_cache()
    return pid


def abrir_e_falar(client, h, pid, texto=VIRO):
    res = client.post(f"/patients/{pid}/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=h)
    assert res.status_code == 200, res.text
    cid = res.json()["id"]
    seg = {"segmentos": [{"falante": "medico", "texto": texto, "t": 1, "origem": "demo"}]}
    out = client.post(f"/consultations/{cid}/segments", json=seg, headers=h)
    assert out.status_code == 200, out.text
    return cid, out.json()["insights"]


def aprovar(client, h, cid):
    """Revisa e atesta a nota; o paciente avulso tem pouca conversa, então a Avaliação é preenchida na revisão."""
    nota = client.get(f"/consultations/{cid}", headers=h).json()["nota"]
    nota["avaliacao"] = {"texto": "Hiperplasia prostática benigna em acompanhamento.", "refs": [], "frases": []}
    res = client.put(f"/consultations/{cid}/note", json=nota | {"atestacao": True}, headers=h)
    assert res.status_code == 200, res.text


def audit(action="consulta.encaminhamento"):
    with db_session.SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


def test_card_chega_pela_api_e_aparece_nos_smart_apps(client):
    h = login(client)
    pid = paciente_hpb(client, h)
    _, ins = abrir_e_falar(client, h, pid)
    assert [e["id"] for e in ins["encaminhamentos"]] == ["uro_hpb"]
    assert ins["encaminhamentos"][0]["exames_faltantes"][0] == "PSA"
    app_ = next(a for a in ins["apps"] if a["id"] == "encaminhamento")
    assert app_["itens"] and "Urologia" in app_["itens"][0]["texto"]
    assert any(a["id"] == "encaminhamento" for a in client.get("/smart-apps", headers=h).json())


def test_manejar_na_aps_registra_na_auditoria(client):
    h = login(client)
    pid = paciente_hpb(client, h)
    cid, _ = abrir_e_falar(client, h, pid)
    before = len(audit())
    res = client.put(
        f"/consultations/{cid}/encaminhamentos/uro_hpb",
        json={"escolha": "manejar_aps", "motivo": "Iniciar tansulosina e pedir PSA."},
        headers=h,
    )
    assert res.status_code == 200, res.text
    card = res.json()["insights"]["encaminhamentos"][0]
    assert card["decisao"]["escolha"] == "manejar_aps" and card["decisao"]["por"]
    rows = audit()
    assert len(rows) == before + 1
    last = rows[-1]
    assert last.entity == "consultation" and last.entity_id == str(cid)
    p = last.payload_json
    assert p["escolha"] == "manejar_aps" and p["encaminhamento"] == "uro_hpb" and p["especialidade"] == "Urologia"
    assert p["exames_faltantes"] and p["criterios_total"] >= 4 and p["gatilho"] == "fala"
    # a escolha continua valendo na próxima análise (ela está em extra_json)
    seg = {"segmentos": [{"falante": "paciente", "texto": "Tá bom doutor.", "t": 9, "origem": "demo"}]}
    again = client.post(f"/consultations/{cid}/segments", json=seg, headers=h).json()["insights"]
    assert again["encaminhamentos"][0]["decisao"]["escolha"] == "manejar_aps"


def test_encaminhar_com_exame_faltando_exige_justificativa(client):
    h = login(client)
    pid = paciente_hpb(client, h)
    cid, _ = abrir_e_falar(client, h, pid)
    url = f"/consultations/{cid}/encaminhamentos/uro_hpb"
    res = client.put(url, json={"escolha": "encaminhar"}, headers=h)
    assert res.status_code == 409 and res.json()["detail"]["codigo"] == "exames_faltando"
    assert "PSA" in res.json()["detail"]["mensagem"]
    assert client.put(url, json={"escolha": "encaminhar", "motivo": "curto"}, headers=h).status_code == 409
    ok = client.put(url, json={"escolha": "encaminhar", "motivo": "Retenção urinária de repetição."}, headers=h)
    assert ok.status_code == 200 and ok.json()["insights"]["encaminhamentos"][0]["decisao"]["escolha"] == "encaminhar"
    # desfazer volta a pendente
    undo = client.put(url, json={"escolha": "pendente"}, headers=h)
    assert undo.json()["insights"]["encaminhamentos"][0]["decisao"]["escolha"] == "pendente"


def test_escolha_em_card_inexistente_ou_consulta_finalizada_e_recusada(client):
    h = login(client)
    pid = paciente_hpb(client, h)
    cid, _ = abrir_e_falar(client, h, pid)
    assert (
        client.put(
            f"/consultations/{cid}/encaminhamentos/nao_existe", json={"escolha": "manejar_aps"}, headers=h
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/consultations/{cid}/encaminhamentos/uro_hpb", json={"escolha": "inventada"}, headers=h
        ).status_code
        == 422
    )
    client.post(f"/consultations/{cid}/finish", headers=h)
    res = client.put(f"/consultations/{cid}/encaminhamentos/uro_hpb", json={"escolha": "manejar_aps"}, headers=h)
    assert res.status_code == 409


def test_encaminhar_leva_o_service_request_em_rascunho_para_a_exportacao(client, monkeypatch):
    h = login(client)
    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    pid = paciente_hpb(client, h, com_psa=True)
    cid, _ = abrir_e_falar(client, h, pid)
    sent = client.put(f"/consultations/{cid}/encaminhamentos/uro_hpb", json={"escolha": "encaminhar"}, headers=h)
    assert sent.status_code == 200, sent.text  # exames mínimos completos: não exige justificativa
    client.post(f"/consultations/{cid}/finish", headers=h)
    aprovar(client, h, cid)
    bundle = client.get(f"/consultations/{cid}/export", headers=h).json()["bundle"]
    srs = [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "ServiceRequest"]
    enc = [s for s in srs if s["identifier"][0]["system"] == encaminhamento.SYSTEM]
    assert len(enc) == 1 and enc[0]["status"] == "draft" and enc[0]["identifier"][0]["value"] == f"{cid}/uro_hpb"
    assert enc[0]["requester"]["display"]
    ServiceRequest.model_validate(enc[0])


def test_manejar_na_aps_nao_exporta_service_request(client, monkeypatch):
    h = login(client)
    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    pid = paciente_hpb(client, h)
    cid, _ = abrir_e_falar(client, h, pid)
    client.put(f"/consultations/{cid}/encaminhamentos/uro_hpb", json={"escolha": "manejar_aps"}, headers=h)
    client.post(f"/consultations/{cid}/finish", headers=h)
    aprovar(client, h, cid)
    bundle = client.get(f"/consultations/{cid}/export", headers=h).json()["bundle"]
    assert encaminhamento.SYSTEM not in json.dumps(bundle)


def test_metrica_de_encaminhamentos_evitados_e_completos(client):
    h = login(client)
    # consulta A: manejo na APS; consulta B: encaminha com justificativa (PSA faltando); C: encaminha completo
    for escolha, com_psa, motivo in (
        ("manejar_aps", False, ""),
        ("encaminhar", False, "Retenção urinária de repetição."),
        ("encaminhar", True, "Critérios de PSA elevados."),
    ):
        pid = paciente_hpb(client, h, com_psa=com_psa)
        cid, _ = abrir_e_falar(client, h, pid)
        res = client.put(
            f"/consultations/{cid}/encaminhamentos/uro_hpb", json={"escolha": escolha, "motivo": motivo}, headers=h
        )
        assert res.status_code == 200, res.text
    res = client.get(
        "/audit/metrics", params={"since": str(clock.now().date() - timedelta(days=2))}, headers=login(client, "gestor")
    )
    m = res.json()["consulta_ia"]["metricas"]
    ev = m["encaminhamentos_evitados"]
    assert ev["denominador"] == 3 and ev["numerador"] == 1 and ev["valor"] == pytest.approx(33.3, abs=0.1)
    assert ev["n_linhas"] == 3 and "consulta.encaminhamento" in ev["formula"]
    comp = m["encaminhamentos_completos"]
    assert comp["denominador"] == 2 and comp["numerador"] == 1 and comp["valor"] == 50.0
