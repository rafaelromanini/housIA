"""#53 — vínculo do médico com a consulta e quebra de vidro justificada."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import clock
from app.auth.security import hash_password
from app.db import session as db_session
from app.db.models import Alert, AuditLog, Organization, User
from tests.conftest import login
from tests.test_consult import start


def segundo_medico(client, org_id=None, email="dr.outro@housia.dev") -> dict:
    """Outro médico da mesma organização (ou de `org_id`), sem nenhuma consulta própria."""
    with db_session.SessionLocal() as db:
        org = org_id or db.scalar(select(User.organization_id).where(User.email == "medico@housia.dev"))
        db.add(
            User(
                name="Dr. Outro",
                email=email,
                role="medico",
                password_hash=hash_password("housia123"),
                organization_id=org,
                email_verificado=True,
            )
        )
        db.commit()
    res = client.post("/auth/login", json={"email": email, "password": "housia123"})
    assert res.status_code == 200, res.text
    client.cookies.clear()
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def linhas(action):
    with db_session.SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


@pytest.fixture
def cenario(client, auth):
    dono = auth("medico")
    cid = start(client, dono)["id"]
    return client, auth, dono, segundo_medico(client), cid


def test_o_dono_da_consulta_abre_normalmente(cenario):
    client, _, dono, _, cid = cenario
    assert client.get(f"/consultations/{cid}", headers=dono).status_code == 200
    assert client.get(f"/consultations/{cid}/note/diff", headers=dono).status_code in (200, 409)
    assert not linhas("consulta.acesso_negado")


def test_medico_sem_vinculo_recebe_403_e_a_tentativa_e_auditada(cenario):
    client, _, _, outro, cid = cenario
    for verbo, url in (
        ("get", f"/consultations/{cid}"),
        ("get", f"/consultations/{cid}/export"),
        ("get", f"/consultations/{cid}/tarefas"),
        ("post", f"/consultations/{cid}/finish"),
        ("post", f"/consultations/{cid}/segments"),
        ("put", f"/consultations/{cid}/note"),
    ):
        res = getattr(client, verbo)(url, headers=outro)
        assert res.status_code == 403 and "quebra de vidro" in res.json()["detail"], (url, res.text)
    negadas = linhas("consulta.acesso_negado")
    assert len(negadas) == 1 and negadas[0].entity_id == str(cid)  # várias tentativas seguidas: um registro só


def test_motivo_curto_ou_vazio_nao_quebra_o_vidro(cenario):
    client, _, _, outro, cid = cenario
    for motivo in ("", "curto", "123456789", "   a b c   ", "​" * 12, "x" * 301):
        res = client.post(f"/consultations/{cid}/quebra-de-vidro", json={"motivo": motivo}, headers=outro)
        assert res.status_code == 422, (motivo, res.text)
    assert client.post(f"/consultations/{cid}/quebra-de-vidro", json={}, headers=outro).status_code == 422
    assert client.get(f"/consultations/{cid}", headers=outro).status_code == 403
    assert not linhas("consulta.quebra_de_vidro")


def test_quebra_de_vidro_com_motivo_libera_audita_e_avisa_o_gestor(cenario):
    client, auth, _, outro, cid = cenario
    res = client.post(
        f"/consultations/{cid}/quebra-de-vidro",
        json={"motivo": "Plantão cobrindo a colega, paciente instável"},
        headers=outro,
    )
    assert res.status_code == 200 and res.json()["quebra_de_vidro"] is True
    assert client.get(f"/consultations/{cid}", headers=outro).status_code == 200
    (q,) = linhas("consulta.quebra_de_vidro")
    assert q.payload_json["motivo"].startswith("Plantão") and q.entity_id == str(cid)
    with db_session.SessionLocal() as db:
        alerta = db.scalar(select(Alert).where(Alert.source == "consulta", Alert.title.contains("quebra de vidro")))
    assert alerta is not None and alerta.severity == "atencao" and "Plantão" not in alerta.detail
    vistos = client.get("/patients/cen6/alerts", headers=auth("gestor")).json()
    assert any("quebra de vidro" in a["title"] for a in vistos)
    metricas = client.get("/compliance/dossie", headers=auth("gestor")).json()["calculado"]
    assert metricas["acessos_quebra_de_vidro"]["total"] == 1


def test_a_quebra_vale_so_para_aquela_consulta_e_so_para_quem_pediu(cenario):
    client, auth, dono, outro, cid = cenario
    segunda = start(client, dono)["id"]
    client.post(
        f"/consultations/{cid}/quebra-de-vidro", json={"motivo": "Atender intercorrência do leito 4"}, headers=outro
    )
    assert client.get(f"/consultations/{segunda}", headers=outro).status_code == 403
    terceiro = segundo_medico(client, email="dr.terceiro@housia.dev")
    assert client.get(f"/consultations/{cid}", headers=terceiro).status_code == 403


def test_a_quebra_expira_depois_de_8_horas(cenario):
    client, _, _, outro, cid = cenario
    client.post(
        f"/consultations/{cid}/quebra-de-vidro", json={"motivo": "Atender intercorrência do leito 4"}, headers=outro
    )
    antes = clock._frozen
    try:
        clock.freeze(antes + timedelta(hours=7, minutes=59))
        assert client.get(f"/consultations/{cid}", headers=outro).status_code == 200
        clock.freeze(antes + timedelta(hours=8, minutes=1))
        assert client.get(f"/consultations/{cid}", headers=outro).status_code == 403
    finally:
        clock.freeze(antes)


def test_so_medico_quebra_o_vidro_e_gestor_nao_abre_consulta(cenario):
    client, auth, _, _, cid = cenario
    corpo = {"motivo": "Preciso conferir a conduta do plantão"}
    assert client.post(f"/consultations/{cid}/quebra-de-vidro", json=corpo, headers=auth("gestor")).status_code == 403
    assert (
        client.post(f"/consultations/{cid}/quebra-de-vidro", json=corpo, headers=auth("enfermagem")).status_code == 403
    )
    assert client.post(f"/consultations/{cid}/quebra-de-vidro", json=corpo).status_code == 401
    assert client.get(f"/consultations/{cid}", headers=auth("gestor")).status_code == 403
    assert not linhas("consulta.quebra_de_vidro")


def test_dono_pedindo_quebra_de_vidro_nao_gera_registro_nem_alerta(cenario):
    client, _, dono, _, cid = cenario
    res = client.post(
        f"/consultations/{cid}/quebra-de-vidro", json={"motivo": "Revisão da minha própria nota"}, headers=dono
    )
    assert res.status_code == 200 and res.json()["quebra_de_vidro"] is False
    assert not linhas("consulta.quebra_de_vidro")


def test_consulta_inexistente_ou_de_outra_organizacao_e_404_mesmo_com_quebra(cenario):
    client, _, _, outro, cid = cenario
    corpo = {"motivo": "Motivo suficientemente longo"}
    assert client.post("/consultations/99999/quebra-de-vidro", json=corpo, headers=outro).status_code == 404
    with db_session.SessionLocal() as db:
        org = Organization(nome="Clínica B", tipo="consultorio")
        db.add(org)
        db.commit()
        org_id = org.id
    estranho = segundo_medico(client, org_id=org_id, email="dr.b@clinicab.test")
    assert client.post(f"/consultations/{cid}/quebra-de-vidro", json=corpo, headers=estranho).status_code == 404
    assert client.get(f"/consultations/{cid}", headers=estranho).status_code == 404  # 404, nunca 403
    assert not linhas("consulta.quebra_de_vidro")


def test_ws_stream_context_tambem_exige_o_vinculo(cenario):
    from fastapi import HTTPException

    from app.consult import service
    from app.fhir_client.client import get_fhir

    client, _, _, _, cid = cenario
    with db_session.SessionLocal() as db:
        outro = db.scalar(select(User).where(User.email == "dr.outro@housia.dev"))
        from app.db.tenancy import set_org

        set_org(db, outro.organization_id)
        with pytest.raises(HTTPException) as exc:
            service.stream_context(db, get_fhir(), outro, cid, "medico")
        assert exc.value.status_code == 403


def test_login_helper_continua_valendo(client):
    assert login(client, "medico")
