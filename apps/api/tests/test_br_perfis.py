"""#52 — Perfis brasileiros: identificadores validados, perfis BR Core no Bundle e exportação e-SUS (LEDI)."""

import copy
from datetime import datetime

import pytest
from sqlalchemy import select

from app.br import identificadores as ids
from app.br import ledi, perfis
from app.consult import service as consult_service
from app.db import session as db_session
from app.db.models import AuditLog
from app.fhir_client.client import get_fhir
from tests.test_consult import SCRIPT, accept_critical, approve, say, start

CPF_OK = "529.982.247-25"
CNS_DEF = ids.cns_definitivo("10000000001")  # sintético, calculado
CNES = "2269481"
CBO = "225125"


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def _prov_ok() -> str:
    for dv in range(10):
        n = f"70000000000000{dv}"
        if sum(int(n[i]) * (15 - i) for i in range(15)) % 11 == 0:
            return n
    raise AssertionError("sem CNS provisório sintético")


# ---------- identificadores ----------
def test_cpf_com_e_sem_mascara():
    assert ids.validar_cpf(CPF_OK) == "52998224725" == ids.validar_cpf("52998224725")


@pytest.mark.parametrize(
    "valor,trecho",
    [
        ("111.111.111-11", "repetidos"),
        ("529.982.247-24", "dígito verificador"),
        ("1234", "11 dígitos"),
        ("", "não informado"),
        ("   ", "não informado"),
        ("abc.def.ghi-jk", "só números"),
        (None, "não informado"),
        (52998224725, "não informado"),
    ],
)
def test_cpf_invalido_tem_mensagem_clara(valor, trecho):
    with pytest.raises(ValueError, match=trecho):
        ids.validar_cpf(valor)


def test_cns_definitivo_e_provisorio_validos():
    assert ids.validar_cns(CNS_DEF) == CNS_DEF
    assert ids.validar_cns(f"{CNS_DEF[:3]} {CNS_DEF[3:]}") == CNS_DEF  # espaços são aceitos
    assert ids.validar_cns(_prov_ok())


@pytest.mark.parametrize(
    "valor,trecho",
    [
        ("100000000010001", "dígito verificador"),
        ("123", "15 dígitos"),
        ("300000000000000", "deve começar"),
        ("700000000000001", "provisório inválido"),
        ("1000000000100010", "15 dígitos"),
    ],
)
def test_cns_invalido(valor, trecho):
    with pytest.raises(ValueError, match=trecho):
        ids.validar_cns(valor)


def test_cnes_e_cbo():
    assert ids.validar_cnes("2269481") == "2269481" and ids.validar_cbo("225125") == "225125"
    for fn, v in (
        (ids.validar_cnes, "0000000"),
        (ids.validar_cnes, "123"),
        (ids.validar_cbo, "025125"),
        (ids.validar_cbo, "22512"),
    ):
        with pytest.raises(ValueError):
            fn(v)


def test_do_paciente_ignora_identificador_invalido():
    p = {"identifier": [{"system": ids.CNS_SYSTEM, "value": "1"}, {"system": ids.CPF_SYSTEM, "value": CPF_OK}]}
    assert ids.do_paciente(p) == {"cpf": "52998224725", "cns": None}


def test_ledi_helpers():
    assert ledi.cid_ledi("n17.9") == "N179"
    assert ledi.ciap_ledi("K86 Hipertensão") == "K86" and ledi.ciap_ledi("") is None
    assert [ledi.turno(h) for h in (6, 13, 20)] == [1, 2, 3]


def test_declarar_perfil_nao_duplica():
    r = perfis.declarar(perfis.declarar({"resourceType": "Practitioner"}))
    assert r["meta"]["profile"] == [perfis.PERFIS["Practitioner"]]
    assert perfis.declarar({"resourceType": "Binary"}) == {"resourceType": "Binary"}


# ---------- API ----------
def ids_do(client, h):
    me = client.get("/auth/me", headers=h).json()
    return me["organizacao"]["id"], me["id"]


def cadastrar(client, auth, cns=CNS_DEF, cbo=CBO, cnes=CNES):
    gestor = auth("gestor")
    org, uid = ids_do(client, auth("medico"))
    r1 = client.put(f"/orgs/{org}/cadastro", json={"cnes": cnes}, headers=gestor)
    r2 = client.put(f"/orgs/{org}/membros/{uid}/cadastro", json={"cns": cns, "cbo": cbo}, headers=gestor)
    return r1, r2


def paciente_com(cns=None, cpf=CPF_OK):
    fhir = get_fhir()
    p = copy.deepcopy(fhir.resources["Patient"]["cen6"])
    p["identifier"] = [{"system": ids.CPF_SYSTEM, "value": cpf}] if cpf else []
    if cns:
        p["identifier"].append({"system": ids.CNS_SYSTEM, "value": cns})
    fhir.resources["Patient"]["cen6"] = p  # substitui (o fixture restaura o original)


def finalizar(client, h, cid, aprovar=True):
    accept_critical(client, h, cid, say(client, h, cid, SCRIPT))
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    if aprovar:
        approve(client, h, cid)


def audit(action):
    with db_session.SessionLocal() as db:
        return [r.payload_json for r in db.scalars(select(AuditLog).where(AuditLog.action == action))]


def test_cadastro_valida_e_so_o_gestor_altera(client, auth):
    r1, r2 = cadastrar(client, auth)
    assert r1.status_code == 200 and r1.json()["cnes"] == CNES
    assert r2.status_code == 200 and r2.json()["cns"] == CNS_DEF and r2.json()["cbo"] == CBO
    org, uid = ids_do(client, auth("medico"))
    medico = auth("medico")
    assert client.put(f"/orgs/{org}/cadastro", json={"cnes": CNES}, headers=medico).status_code == 403
    assert client.put(f"/orgs/{org}/membros/{uid}/cadastro", json={"cbo": CBO}, headers=medico).status_code == 403
    gestor = auth("gestor")
    ruim = client.put(f"/orgs/{org}/membros/{uid}/cadastro", json={"cns": "100000000010001"}, headers=gestor)
    assert ruim.status_code == 422 and "dígito verificador" in ruim.json()["detail"]
    assert client.put(f"/orgs/{org}/cadastro", json={"cnes": "12"}, headers=gestor).status_code == 422
    assert client.put(f"/orgs/{org}/cadastro", json={"cnes": ""}, headers=gestor).json()["cnes"] is None  # apaga
    assert client.put("/orgs/999/cadastro", json={"cnes": CNES}, headers=gestor).status_code == 404
    assert audit("org.cadastro_br")


def test_ledi_caminho_feliz_com_cns_e_auditoria(client, auth):
    cadastrar(client, auth)
    paciente_com(cns=CNS_DEF)
    h = auth("medico")
    cid = start(client, h)["id"]
    finalizar(client, h, cid)
    res = client.get(f"/consultations/{cid}/export/ledi", headers=h)
    assert res.status_code == 200, res.text
    out = res.json()
    f = out["arquivo"]
    head = f["headerTransport"]
    assert (head["profissionalCNS"], head["cboCodigo_2002"], head["cnes"]) == (CNS_DEF, CBO, CNES)
    at = f["atendimentosIndividuais"][0]
    assert at["cnsCidadao"] == CNS_DEF and "cpfCidadao" not in at
    assert at["procedimentosSigtap"] == [] and isinstance(at["problemaCondicaoAvaliada"]["cid10"], list)
    assert all("." not in c for c in at["problemaCondicaoAvaliada"]["cid10"])
    assert f["uuidFicha"].startswith(f"{CNES}-") and out["pendencias"] and "7.3" in out["versao_ledi"]
    # idempotente: mesmo uuid ao reexportar
    assert client.get(f"/consultations/{cid}/export/ledi", headers=h).json()["arquivo"]["uuidFicha"] == f["uuidFicha"]
    a = audit("consulta.ledi_exportado")[0]
    assert a["patient_id"] == "cen6" and CNS_DEF not in str(a) and "52998224725" not in str(a)
    dl = client.get(f"/consultations/{cid}/export/ledi?baixar=true", headers=h)
    assert "attachment" in dl.headers["content-disposition"] and dl.json() == f


def test_ledi_usa_cpf_quando_nao_ha_cns(client, auth):
    cadastrar(client, auth)
    paciente_com(cns=None)
    h = auth("medico")
    cid = start(client, h)["id"]
    finalizar(client, h, cid)
    at = client.get(f"/consultations/{cid}/export/ledi", headers=h).json()["arquivo"]["atendimentosIndividuais"][0]
    assert at["cpfCidadao"] == "52998224725" and "cnsCidadao" not in at


def test_paciente_da_demo_cen6_tem_cpf_valido_para_o_e_sus(client):
    """O CPF sintético do cenário 6 precisa passar no dígito verificador, senão a exportação LEDI da demo falha."""
    assert ids.do_paciente(get_fhir().resources["Patient"]["cen6"])["cpf"] == "28461593782"


def test_ledi_cns_e_cpf_invalidos_do_paciente_pedem_cadastro(client, auth):
    cadastrar(client, auth)
    paciente_com(cns="100000000010001", cpf="111.111.111-11")  # os dois inválidos
    h = auth("medico")
    cid = start(client, h)["id"]
    finalizar(client, h, cid)
    res = client.get(f"/consultations/{cid}/export/ledi", headers=h)
    assert res.status_code == 422 and res.json()["detail"]["codigo"] == "cadastro_br_incompleto"
    assert any("paciente" in f for f in res.json()["detail"]["faltas"])


def test_ledi_sem_cadastro_lista_o_que_falta(client, auth):
    paciente_com(cns=CNS_DEF)
    h = auth("medico")
    cid = start(client, h)["id"]
    finalizar(client, h, cid)
    res = client.get(f"/consultations/{cid}/export/ledi", headers=h)
    assert res.status_code == 422
    faltas = res.json()["detail"]["faltas"]
    assert len(faltas) == 3 and any("CNES" in f for f in faltas) and any("CBO" in f for f in faltas)
    assert "Faltam dados" in res.json()["detail"]["mensagem"]
    assert not audit("consulta.ledi_exportado")


def test_ledi_consulta_nao_finalizada_ou_sem_atestacao_409(client, auth):
    cadastrar(client, auth)
    paciente_com(cns=CNS_DEF)
    h = auth("medico")
    cid = start(client, h)["id"]
    assert client.get(f"/consultations/{cid}/export/ledi", headers=h).status_code == 409  # em andamento
    finalizar(client, h, cid, aprovar=False)
    r = client.get(f"/consultations/{cid}/export/ledi", headers=h)
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == "revisao_pendente"


def test_ledi_papel_sem_permissao_sem_login_e_inexistente(client, auth):
    cadastrar(client, auth)
    h = auth("medico")
    cid = start(client, h)["id"]
    assert client.get(f"/consultations/{cid}/export/ledi", headers=auth("gestor")).status_code == 403
    assert client.get(f"/consultations/{cid}/export/ledi").status_code == 401
    assert client.get("/consultations/99999/export/ledi", headers=h).status_code == 404


def test_ledi_nao_exporta_procedimento_demo_nem_hipotese():
    nota = {
        "cids": [
            {"codigo": "I10", "tipo": "principal", "ciap2": "K86 Hipertensão"},
            {"codigo": "E11.9", "tipo": "secundario", "ciap2": ""},
            {"codigo": "N17.9", "tipo": "secundario", "ciap2": "U99 x"},
            {"codigo": "J18", "tipo": "hipotese", "ciap2": "R81 y"},
        ],
        "relatorio_cirurgico": {"procedimento_sus": {"codigo": "DEMO-SIGTAP-001"}},
    }
    quando = datetime(2026, 1, 5, 9, 0)
    base = {"consulta_id": 7, "quando": quando, "cns_profissional": CNS_DEF, "cbo": CBO, "cnes": CNES}
    out = ledi.montar(**base, nota=nota, paciente={"cns": CNS_DEF, "cpf": None, "nascimento": "1980-01-01"}, sexo="F")
    at = out["arquivo"]["atendimentosIndividuais"][0]
    assert at["problemaCondicaoAvaliada"] == {"ciaps": ["K86"], "cid10": ["I10", "E119"]}
    assert at["procedimentosSigtap"] == [] and at["sexo"] == 1 and at["turno"] == 1
    texto = " ".join(out["pendencias"])
    assert "DEMO-SIGTAP-001" in texto and "N17.9" in texto and "E11.9" in texto and "J18" not in texto
    nota["relatorio_cirurgico"]["procedimento_sus"]["codigo"] = "0301010072"
    out = ledi.montar(**base, nota=nota, paciente={"cns": CNS_DEF}, sexo="X")
    at = out["arquivo"]["atendimentosIndividuais"][0]
    assert at["procedimentosSigtap"] == ["0301010072"] and at["sexo"] is None


# ---------- perfis no Bundle ----------
def test_bundle_declara_perfis_e_leva_cnes_cns_cbo_do_cadastro(client, auth):
    cadastrar(client, auth)
    h = auth("medico")
    cid = start(client, h)["id"]
    finalizar(client, h, cid)
    bundle = client.get(f"/consultations/{cid}/export", headers=h).json()["bundle"]
    por_tipo: dict[str, list[dict]] = {}
    for e in bundle["entry"]:
        por_tipo.setdefault(e["resource"]["resourceType"], []).append(e["resource"])
    assert perfis.PERFIS["Composition"] in por_tipo["Composition"][0]["meta"]["profile"]
    assert perfis.PERFIS["DocumentReference"] in por_tipo["DocumentReference"][0]["meta"]["profile"]
    prac = por_tipo["Practitioner"][0]
    assert {"system": ids.CNS_SYSTEM, "value": CNS_DEF} in prac["identifier"]
    org = por_tipo["Organization"][0]
    assert org["identifier"] == [{"system": ids.CNES_SYSTEM, "value": CNES}]
    assert por_tipo["Composition"][0]["custodian"]["reference"] == "urn:housia:br:organization"
    assert por_tipo["PractitionerRole"][0]["code"][0]["coding"][0] == {"system": ids.CBO_SYSTEM, "code": CBO}


def test_bundle_sem_cadastro_so_declara_perfil_sem_inventar_identificador(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    finalizar(client, h, cid)
    bundle = client.get(f"/consultations/{cid}/export", headers=h).json()["bundle"]
    tipos = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert "Organization" not in tipos and "PractitionerRole" not in tipos
    prac = next(e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Practitioner")
    assert all(i["system"] != ids.CNS_SYSTEM for i in prac["identifier"])


# ---------- verificação adversarial (#52) ----------
@pytest.mark.parametrize(
    "valor",
    [
        "700000000000000",  # passa na conta do dígito, mas é só zeros
        "888888888888888",
        "000000000000000",
        "111111111111111",
        "١٢٣٤٥٦٧٨٩٠١٢٣٤٥",  # dígitos árabes: isdigit() é verdadeiro, mas não é número de CNS
        "1000000000010001\n",
        "<script>alert(1)</script>",
    ],
)
def test_cns_adversarial_nao_passa(valor):
    with pytest.raises(ValueError):
        ids.validar_cns(valor)


@pytest.mark.parametrize(
    "valor", ["000.000.000-00", "99999999999", "52998224725; DROP TABLE users", "５２９９８２２４７２５"]
)
def test_cpf_adversarial_nao_passa(valor):
    with pytest.raises(ValueError):
        ids.validar_cpf(valor)


def test_cnes_e_cbo_com_injecao_ou_unicode_nao_passam():
    for fn, v in (
        (ids.validar_cnes, "2269481; DROP TABLE"),
        (ids.validar_cnes, "２２６９４８１"),
        (ids.validar_cbo, "225125\x00"),
        (ids.validar_cbo, "<b>225125</b>"),
    ):
        with pytest.raises(ValueError):
            fn(v)


def test_cadastro_com_injecao_e_tipos_errados_e_422_e_nada_grava(client, auth):
    gestor = auth("gestor")
    org, uid = ids_do(client, auth("medico"))
    for corpo in ({"cnes": "2269481'; DROP TABLE organizations;--"}, {"cnes": 2269481}, {"cnes": ["2269481"]}):
        assert client.put(f"/orgs/{org}/cadastro", json=corpo, headers=gestor).status_code == 422
    for corpo in ({"cns": "<img src=x onerror=1>"}, {"cbo": 225125}, {"cns": "9" * 31}, {"cbo": {"a": 1}}):
        assert client.put(f"/orgs/{org}/membros/{uid}/cadastro", json=corpo, headers=gestor).status_code == 422
    assert not audit("org.cadastro_br")


def test_cadastro_parcial_nao_apaga_o_outro_campo(client, auth):
    cadastrar(client, auth)
    gestor = auth("gestor")
    org, uid = ids_do(client, auth("medico"))
    r = client.put(f"/orgs/{org}/membros/{uid}/cadastro", json={"cbo": "225133"}, headers=gestor)
    assert r.json() == {"id": uid, "cns": CNS_DEF, "cbo": "225133"}  # CNS continua
    r = client.put(f"/orgs/{org}/membros/{uid}/cadastro", json={"cns": None}, headers=gestor)
    assert r.json() == {"id": uid, "cns": None, "cbo": "225133"}  # null explícito apaga


def test_gestor_de_outra_organizacao_e_o_admin_da_plataforma_nao_mexem_no_cadastro_alheio(client, auth):
    from app.db.models import Organization, User
    from tests.test_multiorg import cadastro_completo

    org_a, uid_a = ids_do(client, auth("medico"))
    hb, gestor_b = cadastro_completo(
        client, "gestora@hospital.test", organizacao_tipo="hospital", conselho=None, conselho_numero=None, uf=None
    )
    org_b, uid_b = gestor_b["organizacao"]["id"], gestor_b["id"]
    assert org_a != org_b
    # gestor B tentando a organização A e o profissional de A
    assert client.put(f"/orgs/{org_a}/cadastro", json={"cnes": CNES}, headers=hb).status_code == 404
    assert client.put(f"/orgs/{org_a}/membros/{uid_a}/cadastro", json={"cbo": CBO}, headers=hb).status_code == 404
    # IDOR com a organização própria na URL e o usuário de A
    assert client.put(f"/orgs/{org_b}/membros/{uid_a}/cadastro", json={"cbo": CBO}, headers=hb).status_code == 404
    # o gestor da organização padrão (admin da plataforma) também não altera o cadastro de B
    admin = auth("gestor")
    assert client.put(f"/orgs/{org_b}/cadastro", json={"cnes": CNES}, headers=admin).status_code == 404
    assert client.put(f"/orgs/{org_b}/membros/{uid_b}/cadastro", json={"cbo": CBO}, headers=admin).status_code == 404
    # o próprio gestor B altera o seu
    assert client.put(f"/orgs/{org_b}/cadastro", json={"cnes": CNES}, headers=hb).status_code == 200
    with db_session.SessionLocal() as db:
        assert db.get(Organization, org_a).cnes is None and db.get(User, uid_a).cbo is None
        assert db.get(Organization, org_b).cnes == CNES


def test_ledi_de_consulta_de_outra_organizacao_e_404_mesmo_com_baixar(client, auth):
    from tests.test_multiorg import cadastro_completo

    cadastrar(client, auth)
    paciente_com(cns=CNS_DEF)
    ha = auth("medico")
    cid = start(client, ha)["id"]
    finalizar(client, ha, cid)
    hb, _ = cadastro_completo(client)  # médica de outra organização
    for q in ("", "?baixar=true"):
        r = client.get(f"/consultations/{cid}/export/ledi{q}", headers=hb)
        assert r.status_code == 404 and CNS_DEF not in r.text and "52998224725" not in r.text
    assert not audit("consulta.ledi_exportado")


def test_ledi_baixar_sem_permissao_e_com_no_store(client, auth):
    cadastrar(client, auth)
    paciente_com(cns=CNS_DEF)
    h = auth("medico")
    cid = start(client, h)["id"]
    finalizar(client, h, cid)
    for h_ruim in ({}, auth("gestor")):
        r = client.get(f"/consultations/{cid}/export/ledi?baixar=true", headers=h_ruim)
        assert r.status_code in (401, 403) and CNS_DEF not in r.text
    ok = client.get(f"/consultations/{cid}/export/ledi?baixar=true", headers=h)
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    assert client.get(f"/consultations/{cid}/export/ledi", headers=h).headers["cache-control"] == "no-store"


def test_ledi_recusa_sandbox_e_cirurgia_sem_gravar_auditoria(client, auth):
    cadastrar(client, auth)
    paciente_com(cns=CNS_DEF)
    h = auth("medico")
    sb = start(client, h, sandbox=True)["id"]
    finalizar(client, h, sb)
    r = client.get(f"/consultations/{sb}/export/ledi", headers=h)
    assert r.status_code == 422 and "sandbox" in r.json()["detail"]
    cir = start(client, h, tipo="cirurgia")["id"]
    r = client.get(f"/consultations/{cir}/export/ledi", headers=h)
    assert r.status_code == 422 and "cirurgia" in r.json()["detail"]
    assert not audit("consulta.ledi_exportado")


def test_ledi_usa_o_cadastro_de_quem_atendeu_e_nao_de_quem_exporta(client, auth):
    cadastrar(client, auth)
    paciente_com(cns=CNS_DEF)
    h = auth("medico")
    cid = start(client, h)["id"]
    finalizar(client, h, cid)
    enf = auth("enfermagem")  # colega da mesma organização, sem CNS/CBO cadastrados
    r = client.get(f"/consultations/{cid}/export/ledi", headers=enf)
    assert r.status_code == 200, r.text
    assert r.json()["arquivo"]["headerTransport"]["profissionalCNS"] == CNS_DEF


def test_ledi_nota_com_campos_nulos_ou_texto_livre_nao_quebra_nem_vaza_para_o_arquivo():
    quando = datetime(2026, 1, 5, 22, 0)
    base = {"consulta_id": 3, "quando": quando, "cns_profissional": CNS_DEF, "cbo": CBO, "cnes": CNES}
    paciente = {"cns": CNS_DEF, "cpf": None, "nascimento": None}
    for nota in (
        {},
        {"cids": None, "relatorio_cirurgico": None},
        {"cids": [None, 5, "I10", {"tipo": "principal", "codigo": None}, {"tipo": "principal", "codigo": 10}]},
        {"cids": [{"tipo": "principal", "codigo": "x"}], "relatorio_cirurgico": {"procedimento_sus": "0301010072"}},
    ):
        out = ledi.montar(**base, nota=nota, paciente=paciente, sexo=None)
        at = out["arquivo"]["atendimentosIndividuais"][0]
        assert at["problemaCondicaoAvaliada"]["cid10"] == [] and at["procedimentosSigtap"] == []
        assert at["turno"] == 3 and at["dataNascimento"] is None
    injecao = {"cids": [{"tipo": "principal", "codigo": "I10'); DROP TABLE x;--", "ciap2": "<b>K86</b>"}]}
    out = ledi.montar(**base, nota=injecao, paciente=paciente, sexo="M")
    assert out["arquivo"]["atendimentosIndividuais"][0]["problemaCondicaoAvaliada"]["cid10"] == []
    assert any("formato de CID-10" in p for p in out["pendencias"])


def test_nenhuma_auditoria_da_52_guarda_cns_cpf_ou_cadastro(client, auth):
    cadastrar(client, auth)
    paciente_com(cns=CNS_DEF)
    h = auth("medico")
    cid = start(client, h)["id"]
    finalizar(client, h, cid)
    assert client.get(f"/consultations/{cid}/export/ledi", headers=h).status_code == 200
    with db_session.SessionLocal() as db:
        linhas = [
            str(r.payload_json) + str(r.entity_id)
            for r in db.scalars(
                select(AuditLog).where(AuditLog.action.in_(["consulta.ledi_exportado", "org.cadastro_br"]))
            )
        ]
    assert linhas
    assert not any(x in " ".join(linhas) for x in (CNS_DEF, "52998224725", CNES, CBO, "529.982"))
