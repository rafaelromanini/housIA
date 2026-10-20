"""#33 (verificação cética): entradas adversariais contra o isolamento entre organizações."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from app import clock
from app import email as mailer
from app.auth.router import DEMO_ORG_NAME
from app.db import session as db_session
from app.db import tenancy
from app.db.models import Alert, AuditLog, Consultation, DischargeAssessment, JourneyTask, Organization, User
from tests.conftest import CONSENT
from tests.test_discharge import RISKY
from tests.test_multiorg import SENHA, cadastrar, cadastro_completo, entrar, token_do_email

HOSPITAL_SUL = dict(
    organizacao_tipo="hospital",
    conselho=None,
    conselho_numero=None,
    uf=None,
    organizacao_nome="Hospital Sul",
)


@pytest.fixture(autouse=True)
def _outbox():
    mailer.OUTBOX.clear()
    yield


# corpos que o OpenAPI mínimo não satisfaz (validadores de tamanho/combinação): sem eles a rota daria 422, não 404
CORPOS = {
    ("POST", "/consultations/{cid}/segments"): {"segmentos": [{"texto": "dor de cabeça"}]},
    ("PUT", "/consultations/{cid}/hipoteses"): {"cid": "I10", "decisao": "confirmada"},
    ("PUT", "/consultations/{cid}/opme-autorizado"): {"itens": []},
}


def _corpo_minimo(schema: dict, spec: dict) -> dict:
    """Corpo válido mínimo a partir do OpenAPI (só os campos obrigatórios)."""
    if "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    out = {}
    for nome in schema.get("required", []):
        p = schema["properties"][nome]
        if "$ref" in p:
            p = spec["components"]["schemas"][p["$ref"].rsplit("/", 1)[-1]]
        if "enum" in p:
            out[nome] = p["enum"][0]
        else:
            out[nome] = {
                "string": "x" * max(p.get("minLength", 0), 12),
                "integer": 1,
                "number": 1,
                "boolean": True,
                "array": [],
                "object": {},
            }.get(p.get("type"), "x" * 12)
    return out


def _rotas_com_dono(spec: dict) -> list[tuple[str, str, dict | None]]:
    rotas = []
    for path, ops in spec["paths"].items():
        if "{cid}" not in path:
            continue
        for metodo, op in ops.items():
            corpo = None
            content = op.get("requestBody", {}).get("content", {}).get("application/json")
            if content:
                corpo = _corpo_minimo(content["schema"], spec)
            corpo = CORPOS.get((metodo.upper(), path), corpo)
            rotas.append((metodo.upper(), path, corpo))
    return rotas


# ---------------------------------------------------------------- 1. acesso cruzado por id em cada rota com dono
def test_toda_rota_de_consulta_responde_404_para_outra_organizacao(client, auth):
    ha = auth("medico")
    cid = client.post(
        "/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=ha
    ).json()["id"]
    hb, _ = cadastro_completo(client)
    spec = client.get("/openapi.json").json()
    rotas = _rotas_com_dono(spec)
    assert len(rotas) >= 27  # a lista vem do OpenAPI: rota nova com {cid} entra sozinha
    sem_404 = []
    for metodo, path, corpo in rotas:
        url = path.replace("{cid}", str(cid)).replace("{n}", "1").replace("{draft_id}", "x").replace("{item_id}", "1")
        res = client.request(metodo, url, json=corpo, headers=hb)
        if res.status_code != 404:
            sem_404.append((metodo, path, res.status_code))
    assert not sem_404, sem_404
    # a consulta de A segue intacta
    with db_session.SessionLocal() as db:
        assert db.get(Consultation, cid) is not None


def _alerta_de_a(client, ha) -> int:
    saida = client.post("/patients/cen1/handoff", json={"audience": "enfermagem"}, headers=ha).json()
    return next(r["alert_id"] for r in saida["content"]["riscos_proximas_horas"] if r.get("alert_id"))


def test_alerta_avaliacao_de_alta_e_tarefa_de_outra_organizacao_sao_404(client, auth):
    ha = auth("medico")
    aid = _alerta_de_a(client, ha)
    ava = client.post("/patients/cen3/discharge-assessment", json={"social_checklist": RISKY}, headers=ha).json()
    tarefa = client.post(
        "/journey/tasks", json={"patient_id": "cen7", "regra": "alta_sem_contato_72h"}, headers=auth("enfermagem")
    )
    assert tarefa.status_code == 201, tarefa.text
    tid = tarefa.json()["id"]

    hb, _ = cadastro_completo(client, "gestora@hospital.test", **HOSPITAL_SUL)
    hm, _ = cadastro_completo(client, "dra.bia@clinica.test")
    for h in (hb, hm):
        r = client.post(f"/alerts/{aid}/status", json={"status": "descartado", "justificativa": "x" * 20}, headers=h)
        assert r.status_code == 404
        assert client.patch(f"/journey/tasks/{tid}", json={"status": "concluida"}, headers=h).status_code == 404
    # B é médico em consultório: papel permitido na confirmação/override, mas a avaliação não existe para ele
    assert client.post(f"/discharge-assessments/{ava['id']}/confirm", headers=hm).status_code == 404
    assert (
        client.post(
            f"/discharge-assessments/{ava['id']}/override", json={"justification": "x" * 30}, headers=hm
        ).status_code
        == 404
    )
    with db_session.SessionLocal() as db:
        assert db.get(Alert, aid).status == "aberto"
        assert db.get(JourneyTask, tid).status == "aberta"
        assert db.get(DischargeAssessment, ava["id"]).confirmed_at is None


# ---------------------------------------------------------------- 2. consultas que escapam do filtro
def test_agregacoes_joins_e_listagens_nao_vazam_dados_de_outra_organizacao(client, auth):
    ha = auth("gestor")
    hm = auth("medico")
    _alerta_de_a(client, hm)
    client.post("/patients/cen3/discharge-assessment", json={"social_checklist": RISKY}, headers=hm)
    client.post("/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=hm)
    painel_a = client.get("/patients", headers=ha).json()
    assert any(
        p["alertas_abertos"]["atencao"] or p["alertas_abertos"]["critico"] for p in painel_a
    )  # A tem alertas e risco de alta
    assert any(p.get("risco_alta") for p in painel_a)

    hb, _ = cadastro_completo(client, "gestora@hospital.test", **HOSPITAL_SUL)
    painel_b = client.get("/patients", headers=hb).json()
    assert not any(sum(p["alertas_abertos"].values()) for p in painel_b), "group_by/count de alertas escapou do filtro"
    assert not any(p.get("risco_alta") for p in painel_b), "subquery max(id) + join escapou do filtro"
    # auditoria com outerjoin(User): só o que B fez
    trilha = client.get("/audit", headers=hb).json()
    assert trilha["total"] >= 1 and {e["user"]["name"] for e in trilha["items"] if e["user"]} == {"Beatriz Souza"}
    assert not any(e["action"].startswith(("handoff", "discharge", "consulta")) for e in trilha["items"])
    # métricas e indicadores de B não contam as consultas/alertas de A
    met = client.get("/audit/metrics", headers=hb)
    assert met.status_code == 200
    m = met.json()
    assert m["alertas_criticos"] == 0 and m["alertas_por_status"] == {} and m["passagens_geradas"] == 0
    assert m["altas_alto_risco"] == 0 and m["reconciliacoes"] == 0
    m_a = client.get("/audit/metrics", headers=ha).json()
    assert m_a["passagens_geradas"] >= 1 and m_a["altas_alto_risco"] >= 1 and m_a["alertas_por_status"]
    assert client.get("/gestao", headers=hb).status_code == 200
    # contagem direta no banco (sessão sem organização enxerga tudo; a de B só o dela)
    with db_session.SessionLocal() as db:
        total = db.scalar(select(func.count()).select_from(Alert))
        org_b = db.scalar(select(User.organization_id).where(User.email == "gestora@hospital.test"))
        tenancy.set_org(db, org_b)
        assert total > 0
        assert db.scalar(select(func.count()).select_from(Alert)) == 0
        assert db.scalar(select(func.count(Consultation.id))) == 0
        assert list(db.execute(select(Consultation.id, Consultation.patient_id))) == []
        assert db.scalar(select(func.count()).select_from(select(DischargeAssessment).subquery())) == 0
        assert db.get(Consultation, 1) is None  # get por chave primária também passa pelo filtro
        assert db.scalars(select(AuditLog).where(AuditLog.action == "handoff.gerado")).first() is None


# ---------------------------------------------------------------- 3. gravação forçando organization_id alheio
def test_corpo_com_organization_id_alheio_e_ignorado(client, auth):
    ha = auth("medico")
    hb, ub = cadastro_completo(client)
    with db_session.SessionLocal() as db:
        org_a = db.scalar(select(User.organization_id).where(User.email == "medico@housia.dev"))
    res = client.post(
        "/patients/cen6/consultations",
        json={"tipo": "consulta", "consentimento": CONSENT, "organization_id": org_a, "organizacao_id": org_a},
        headers=hb,
    )
    assert res.status_code in (200, 201), res.text
    with db_session.SessionLocal() as db:
        c = db.get(Consultation, res.json()["id"])
        assert c.organization_id == ub["organizacao"]["id"] != org_a
    # cadastro com role/organização no corpo: nasce numa organização nova, com o papel derivado do tipo
    reg = cadastrar(client, "outra@clinica.test", organization_id=org_a, role="gestor", organizacao_id=org_a)
    assert reg.status_code == 201
    with db_session.SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == "outra@clinica.test"))
        assert u.organization_id != org_a and u.role == "medico"
    _ = ha


def test_sessao_com_organizacao_recusa_gravar_para_outra(client):
    cadastro_completo(client)
    with db_session.SessionLocal() as db:
        a, b = [o.id for o in db.scalars(select(Organization).order_by(Organization.id))][:2]
        tenancy.set_org(db, a)
        db.add(Alert(patient_id="p", severity="info", title="t", source="x", organization_id=b))
        with pytest.raises(PermissionError):
            db.flush()
        db.rollback()
        # reatribuir um registro visível para outra organização também é recusado
        db.add(Alert(patient_id="p", severity="info", title="t", source="x"))
        db.commit()
        alerta = db.scalar(select(Alert).where(Alert.title == "t"))
        assert alerta.organization_id == a
        alerta.organization_id = b
        with pytest.raises(PermissionError):
            db.flush()
        db.rollback()


def test_usuario_sem_organizacao_nao_enxerga_nada(client, auth):
    """Falha fechada: usuário órfão (organization_id nulo) não pode virar sessão sem filtro."""
    hm = auth("medico")
    client.post("/patients/cen6/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=hm)
    with db_session.SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == "enfermeira@housia.dev"))
        u.organization_id = None
        db.commit()
    h = auth("enfermagem")
    assert client.get("/patients/cen6/consultations", headers=h).status_code == 403
    assert client.get("/audit", headers=auth("gestor")).json()["total"] >= 0


# ---------------------------------------------------------------- 4. tokens
def test_token_de_um_tipo_nao_serve_para_outro(client):
    cadastrar(client)
    verify = token_do_email("dra.bia@clinica.test", "Confirme")
    assert client.post("/auth/reset", json={"token": verify, "senha": "nova-senha-77"}).status_code == 400
    assert client.get(f"/auth/invites/{verify}").status_code == 400
    assert (
        client.post("/auth/invites/accept", json={"token": verify, "nome": "Fulano Tal", "senha": SENHA}).status_code
        == 400
    )
    assert client.post("/auth/verify", json={"token": verify}).status_code == 200
    client.post("/auth/forgot", json={"email": "dra.bia@clinica.test"})
    reset = token_do_email("dra.bia@clinica.test", "Redefinição")
    assert client.post("/auth/verify", json={"token": reset}).status_code == 400
    assert client.get(f"/auth/invites/{reset}").status_code == 400


def test_token_de_redefinicao_expira_em_uma_hora_e_nao_se_reusa(client):
    cadastro_completo(client)
    client.post("/auth/forgot", json={"email": "dra.bia@clinica.test"})
    tok = token_do_email("dra.bia@clinica.test", "Redefinição")
    base = clock.now()
    try:
        clock.freeze(base + timedelta(hours=1, minutes=1))
        assert client.post("/auth/reset", json={"token": tok, "senha": "nova-senha-77"}).status_code == 400
    finally:
        clock.freeze(base)
    assert entrar(client, "dra.bia@clinica.test", SENHA).status_code == 200  # senha antiga intacta
    assert client.post("/auth/reset", json={"token": tok, "senha": "nova-senha-77"}).status_code == 200
    assert client.post("/auth/reset", json={"token": tok, "senha": "outra-senha-88"}).status_code == 400
    assert entrar(client, "dra.bia@clinica.test", "outra-senha-88").status_code == 401


def test_nova_redefinicao_ou_uso_invalida_links_antigos(client):
    cadastro_completo(client)
    client.post("/auth/forgot", json={"email": "dra.bia@clinica.test"})
    antigo = token_do_email("dra.bia@clinica.test", "Redefinição")
    client.post("/auth/forgot", json={"email": "dra.bia@clinica.test"})
    novo = token_do_email("dra.bia@clinica.test", "Redefinição")
    assert antigo != novo
    assert client.post("/auth/reset", json={"token": novo, "senha": "nova-senha-77"}).status_code == 200
    assert client.post("/auth/reset", json={"token": antigo, "senha": "invasor-senha-1"}).status_code == 400


def test_convite_expirado_e_token_adulterado(client):
    hg, gestor = cadastro_completo(client, "gestora@hospital.test", **HOSPITAL_SUL)
    oid = gestor["organizacao"]["id"]
    client.post(f"/orgs/{oid}/invites", json={"email": "enf@hospital.test", "papel": "enfermagem"}, headers=hg)
    tok = token_do_email("enf@hospital.test", "Convite")
    base = clock.now()
    try:
        clock.freeze(base + timedelta(days=7, minutes=1))
        assert client.get(f"/auth/invites/{tok}").status_code == 400
        assert (
            client.post("/auth/invites/accept", json={"token": tok, "nome": "Marta Enf", "senha": SENHA}).status_code
            == 400
        )
    finally:
        clock.freeze(base)
    assert client.get(f"/auth/invites/{tok[:-1]}x").status_code == 400
    assert client.get(f"/auth/invites/{tok.upper()}").status_code == 400
    # senha curta não queima o convite
    assert (
        client.post("/auth/invites/accept", json={"token": tok, "nome": "Marta Enf", "senha": "curta"}).status_code
        == 422
    )
    assert (
        client.post("/auth/invites/accept", json={"token": tok, "nome": "Marta Enf", "senha": SENHA}).status_code == 201
    )


def test_token_nao_fica_em_texto_aberto_no_banco(client):
    cadastrar(client)
    tok = token_do_email("dra.bia@clinica.test", "Confirme")
    with db_session.SessionLocal() as db:
        guardado = [r[0] for r in db.execute(text("select token_hash from auth_tokens"))]
    assert guardado and tok not in guardado


# ---------------------------------------------------------------- 5. convite para outra organização / papel
def test_convite_nao_escolhe_organizacao_nem_papel_no_aceite(client):
    hg, gestor = cadastro_completo(client, "gestora@hospital.test", **HOSPITAL_SUL)
    hm, medica = cadastro_completo(client)
    oid = gestor["organizacao"]["id"]
    client.post(f"/orgs/{oid}/invites", json={"email": "enf@hospital.test", "papel": "enfermagem"}, headers=hg)
    tok = token_do_email("enf@hospital.test", "Convite")
    ok = client.post(
        "/auth/invites/accept",
        json={
            "token": tok,
            "nome": "Marta Enf",
            "senha": SENHA,
            "role": "gestor",
            "papel": "gestor",
            "organization_id": medica["organizacao"]["id"],
            "email": "dra.bia@clinica.test",
        },
    )
    assert ok.status_code == 201
    u = entrar(client, "enf@hospital.test").json()["user"]
    assert u["role"] == "enfermagem" and u["organizacao"]["id"] == oid
    # a médica de B não convida para A, nem lista convites ou membros de A
    mid = medica["organizacao"]["id"]
    assert client.post(
        f"/orgs/{oid}/invites", json={"email": "z@z.test", "papel": "medico"}, headers=hm
    ).status_code in (
        403,
        404,
    )
    # gestora de A (hospital) não convida, lista nem confere conselho na organização B
    assert (
        client.post(f"/orgs/{mid}/invites", json={"email": "z@z.test", "papel": "medico"}, headers=hg).status_code
        == 404
    )
    assert client.get(f"/orgs/{mid}/invites", headers=hg).status_code == 404
    assert client.get(f"/orgs/{mid}/membros", headers=hg).status_code == 404
    assert client.get(f"/orgs/{mid}/onboarding", headers=hg).status_code == 404
    bia = medica["id"]
    assert client.post(f"/orgs/{mid}/membros/{bia}/conselho", json={"verificado": True}, headers=hg).status_code == 404
    # nem pela própria organização, mirando usuário de outra (id alheio)
    assert client.post(f"/orgs/{oid}/membros/{bia}/conselho", json={"verificado": True}, headers=hg).status_code == 404


def test_convite_para_e_mail_ja_cadastrado_em_outra_organizacao_nao_revela_a_conta(client):
    hg, gestor = cadastro_completo(client, "gestora@hospital.test", **HOSPITAL_SUL)
    cadastro_completo(client)  # dra.bia existe na organização B
    oid = gestor["organizacao"]["id"]
    existe = client.post(f"/orgs/{oid}/invites", json={"email": "dra.bia@clinica.test", "papel": "medico"}, headers=hg)
    nao_existe = client.post(f"/orgs/{oid}/invites", json={"email": "nova@x.test", "papel": "medico"}, headers=hg)
    assert existe.status_code == nao_existe.status_code == 201
    assert existe.json().keys() == nao_existe.json().keys()


# ---------------------------------------------------------------- 6. enumeração de e-mail
def test_cadastro_com_email_existente_responde_igual_ao_novo(client):
    assert cadastrar(client).status_code == 201
    novo = cadastrar(client, "outra.pessoa@clinica.test")
    repetido = cadastrar(client)
    demo = cadastrar(client, "Medico@Housia.dev ")
    assert novo.status_code == repetido.status_code == demo.status_code == 201
    assert novo.json().keys() == repetido.json().keys() == demo.json().keys()
    assert novo.json()["detail"] == repetido.json()["detail"] == demo.json()["detail"]
    with db_session.SessionLocal() as db:
        # nada foi criado para o e-mail repetido, e o dono da conta recebe o aviso
        assert db.scalar(select(func.count()).select_from(User).where(User.email == "dra.bia@clinica.test")) == 1
        assert db.scalar(select(func.count()).select_from(Organization)) == 3  # demonstração + 2 novas
    avisos = [m for m in mailer.OUTBOX if m["to"] == "dra.bia@clinica.test" and "já tem" in m["subject"]]
    assert avisos and "token=" not in avisos[-1]["body"]


def test_login_nao_distingue_conta_inexistente_de_senha_errada(client):
    cadastro_completo(client)
    errada = client.post("/auth/login", json={"email": "dra.bia@clinica.test", "password": "senha-errada-1"})
    inexistente = client.post("/auth/login", json={"email": "ninguem@clinica.test", "password": "senha-errada-1"})
    assert errada.status_code == inexistente.status_code == 401 and errada.json() == inexistente.json()
    # sem confirmar o e-mail, quem erra a senha também recebe 401 (o 403 só aparece com a senha certa)
    cadastrar(client, "pendente@clinica.test")
    pend = client.post("/auth/login", json={"email": "pendente@clinica.test", "password": "senha-errada-1"})
    assert pend.status_code == 401


def test_esqueci_senha_de_conta_nao_confirmada_ou_inexistente_responde_igual(client):
    cadastrar(client, "pendente@clinica.test")
    a = client.post("/auth/forgot", json={"email": "pendente@clinica.test"})
    b = client.post("/auth/forgot", json={"email": "nao.existe@clinica.test"})
    c = client.post("/auth/forgot", json={"email": " PENDENTE@clinica.test "})
    assert a.status_code == b.status_code == c.status_code == 200 and a.json() == b.json() == c.json()


# ---------------------------------------------------------------- 7. administrador da plataforma
def test_nome_reservado_da_organizacao_padrao_nao_vira_administrador(client):
    """Quem se cadastra com o nome da organização de demonstração não ganha acesso de administrador."""
    for nome in (DEMO_ORG_NAME, DEMO_ORG_NAME.upper(), f"  {DEMO_ORG_NAME}  "):
        res = cadastrar(client, f"x{abs(hash(nome))}@hospital.test", **(HOSPITAL_SUL | {"organizacao_nome": nome}))
        assert res.status_code == 422, (nome, res.text)


def test_gestor_de_hospital_comum_nao_confere_conselho_de_outra_organizacao(client, auth):
    hg, gestor = cadastro_completo(client, "gestora@hospital.test", **HOSPITAL_SUL)
    _, medica = cadastro_completo(client)
    mid = medica["organizacao"]["id"]
    res = client.post(f"/orgs/{mid}/membros/{medica['id']}/conselho", json={"verificado": True}, headers=hg)
    assert res.status_code == 404
    with db_session.SessionLocal() as db:
        assert db.get(User, medica["id"]).conselho_verificado is False
    _ = gestor


# ---------------------------------------------------------------- 8. nota exportada
def test_selo_de_verificado_nao_vem_do_corpo_nem_do_cadastro(client):
    hm, medica = cadastro_completo(client, conselho_verificado=True)
    assert medica["conselho_verificado"] is False
    me = client.get("/auth/me", headers=hm).json()
    assert me["conselho_verificado"] is False
