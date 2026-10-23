"""#45: documentos derivados da consulta e resumo ao paciente (atestado, encaminhamento, contrarreferência, convênio,
receita, exames), com fonte em cada linha, aprovação do profissional e um DocumentReference por documento."""

import pytest
from sqlalchemy import select

from app.consult import documentos
from app.consult import service as consult_service
from app.db import session as db_session
from app.db.models import AuditLog
from tests.test_consult import accept_critical, approve, say, start
from tests.test_consult_encaminhamento import VIRO, hpb
from tests.test_consult_envio_prontuario import consulta_pronta
from tests.test_discharge import RISKY


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


@pytest.fixture
def writeback(monkeypatch):
    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)


def url(cid, tipo="", extra=""):
    return f"/consultations/{cid}/documentos" + (f"/{tipo}" if tipo else "") + extra


def gerar(client, h, cid, tipo, **body):
    res = client.post(url(cid, tipo), json=body or None, headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def itens(doc):
    return [it for s in doc["secoes"] for it in s["itens"]]


def aprovado(client, h, cid, tipo, **body):
    gerar(client, h, cid, tipo, **body)
    res = client.post(url(cid, tipo, "/aprovar"), headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def assinar_exame(client, h, cid):
    ins = client.get(f"/consultations/{cid}", headers=h).json()["insights"]
    ecg = next(d for d in ins["pedidos"] if d["item"] == "Eletrocardiograma")
    res = client.put(f"/consultations/{cid}/pedidos/{ecg['id']}", json={"estado": "assinado"}, headers=h)
    assert res.status_code == 200, res.text


# ---------- critério 1: resumo do cen6 com remédios e sinais de alarme com refs ----------
def test_resumo_do_cen6_traz_remedios_e_sinais_de_alarme_com_refs(client, auth, store):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    doc = gerar(client, h, cid, "resumo_paciente")
    por_secao = {s["titulo"]: s["itens"] for s in doc["secoes"]}
    remedios = " ".join(i["texto"] for i in por_secao["Seus remédios"])
    assert "Losartana" in remedios and "Horários: 08:00" in remedios and "Serve para" in remedios
    alarmes = por_secao["Sinais de alarme"]
    assert len(alarmes) >= 3 and all(i["refs"] and i["obrigatorio"] for i in alarmes)
    assert any("açúcar baixo" in i["texto"] for i in alarmes)  # glibenclamida/diabetes do cen6
    # toda linha tem fonte, e a fonte existe no prontuário (ou é uma fala da conversa)
    for it in itens(doc):
        assert it["refs"], it
        for ref in it["refs"]:
            if not ref.startswith("Fala/"):
                rtype, rid = ref.split("/")
                assert rid in store.resources[rtype], ref
    assert doc["loinc"]["codigo"] == "69730-0" and doc["aprovado"] is None


def test_orientacao_solta_da_nota_sem_fonte_e_descartada(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    nota = client.get(f"/consultations/{cid}", headers=h).json()["nota"]
    assert nota["orientacoes_paciente"]
    doc = gerar(client, h, cid, "resumo_paciente")
    assert {d["texto"] for d in doc["descartados"]} >= set(nota["orientacoes_paciente"])
    assert all(o not in {i["texto"] for i in itens(doc)} for o in nota["orientacoes_paciente"])
    assert any("de fora" in a for a in doc["avisos"])


def test_legibilidade_em_portugues_e_calculada():
    facil = documentos.legibilidade("Tome o remédio de manhã. Beba água. Volte em sete dias.")
    dificil = documentos.legibilidade(
        "A administração concomitante de inibidores da enzima conversora de angiotensina requer monitorização "
        "laboratorial periódica da creatinina sérica e da calemia."
    )
    assert facil["faixa"] in ("muito fácil", "fácil") and facil["indice"] > 60
    assert dificil["indice"] < 25 and dificil["faixa"] == "muito difícil"
    assert documentos.legibilidade("")["indice"] is None
    assert documentos.silabas("remédio") == 3 and documentos.silabas("sol") == 1


def test_resumo_traz_a_legibilidade(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    doc = gerar(client, h, cid, "resumo_paciente")
    assert doc["legibilidade"]["indice"] is not None and doc["legibilidade"]["palavras"] > 30
    if doc["legibilidade"]["indice"] < documentos.MIN_LEGIVEL:
        assert any("difícil" in a for a in doc["avisos"])


# ---------- critério 2: atestado sem CID por padrão ----------
def test_atestado_sai_sem_cid_por_padrao_e_so_com_autorizacao_do_paciente(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    nota = client.get(f"/consultations/{cid}", headers=h).json()["nota"]
    assert nota["cids"], "o cen6 gera CID na nota"
    padrao = gerar(client, h, cid, "atestado")
    assert not any("CID" in i["texto"] for i in itens(padrao))
    sem_auth = gerar(client, h, cid, "atestado", incluir_cid=True)  # pediu, mas o paciente não autorizou
    assert not any("CID" in i["texto"] for i in itens(sem_auth))
    assert any("não autorizou" in a for a in sem_auth["avisos"])
    com = gerar(client, h, cid, "atestado", incluir_cid=True, autorizacao_cid=True, dias=2)
    cids = [i for i in itens(com) if "CID-10" in i["texto"]]
    assert cids and cids[0]["refs"] and "autorização do paciente" in cids[0]["texto"]
    dias = next(i for i in itens(com) if "afastamento" in i["texto"])
    assert "2 dias" in dias["texto"] and dias["origem"] == "profissional"
    assert com["loinc"]["codigo"] == "51852-2"


def test_audit_do_documento_guarda_contagens_e_nunca_o_texto(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    doc = gerar(client, h, cid, "resumo_paciente")
    with db_session.SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "consulta.documento_gerado")).all()
        blob = str([r.payload_json for r in rows])
    assert rows and "Losartana" not in blob and doc["secoes"][0]["itens"][0]["texto"] not in blob
    assert "descartados" in blob and "itens" in blob


# ---------- critério 3: contrarreferência de paciente de alta ----------
def test_contrarreferencia_de_paciente_de_alta_traz_as_pendencias_do_waze(client, auth):
    h = auth("medico")
    ava = client.post("/patients/cen3/discharge-assessment", json={"social_checklist": RISKY}, headers=h).json()
    assert ava["sugestoes"]
    cid = start(client, h, "cen3")["id"]
    ins = say(client, h, cid, [{"falante": "medico", "texto": "Como o senhor está hoje?", "t": 0}])
    accept_critical(client, h, cid, ins)
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    approve(client, h, cid)
    doc = gerar(client, h, cid, "contrarreferencia")
    por_secao = {s["titulo"]: s["itens"] for s in doc["secoes"]}
    transicao = por_secao["Transição de cuidado (alta)"]
    textos = {i["texto"] for i in transicao}
    for s in ava["sugestoes"]:
        assert s["texto"] in textos
    assert any("LACE" in t for t in textos) and all(i["refs"] for i in transicao)
    assert "Reconciliação medicamentosa" in por_secao
    assert doc["loinc"]["codigo"] == "18761-7"


def test_contrarreferencia_sem_alta_nao_inventa_a_secao_de_transicao(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    doc = gerar(client, h, cid, "contrarreferencia")
    assert "Transição de cuidado (alta)" not in {s["titulo"] for s in doc["secoes"]}


# ---------- critério 4: um DocumentReference por documento ----------
def test_cada_documento_vira_um_documentreference_com_loinc_proprio(client, auth, store, writeback):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    assinar_exame(client, h, cid)
    refs, loincs, idents = [], [], []
    for tipo in ("resumo_paciente", "atestado", "contrarreferencia", "solicitacao_exames"):
        aprovado(client, h, cid, tipo)
        res = client.post(url(cid, tipo, "/send"), headers=h)
        assert res.status_code == 200 and res.json()["enviado"] is True, res.text
        refs.append(res.json()["fhir_ref"])
        recurso = store.resources["DocumentReference"][res.json()["fhir_ref"].split("/")[1]]
        loincs.append(recurso["type"]["coding"][0]["code"])
        idents.append(recurso["identifier"][0]["value"])
        assert recurso["docStatus"] == "final" and recurso["context"]["encounter"]
    assert len(set(refs)) == 4 and len(set(loincs)) == 4 and len(set(idents)) == 4
    assert idents[1] == f"{cid}:atestado"
    again = client.post(url(cid, "atestado", "/send"), headers=h)
    assert again.status_code == 409  # não duplica
    estado = {d["tipo"]: d for d in client.get(url(cid), headers=h).json()["documentos"]}
    assert estado["atestado"]["enviado"] is True and estado["receita"]["enviado"] is False


def test_sem_writeback_exporta_o_recurso_sem_gravar(client, auth, store):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    aprovado(client, h, cid, "atestado")
    antes = len(store.resources["DocumentReference"])
    res = client.post(url(cid, "atestado", "/send"), headers=h).json()
    assert res["enviado"] is False and res["recurso"]["resourceType"] == "DocumentReference"
    assert len(store.resources["DocumentReference"]) == antes
    pdf = client.get(url(cid, "atestado", "/export.pdf"), headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


# ---------- nada sai sem aprovação, revisão e nota atual ----------
def test_documento_nao_aprovado_nao_exporta_nem_envia(client, auth, writeback):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    gerar(client, h, cid, "atestado")
    for rota, metodo in (("/export", client.get), ("/export.pdf", client.get), ("/send", client.post)):
        res = metodo(url(cid, "atestado", rota), headers=h)
        assert res.status_code == 409 and res.json()["detail"]["codigo"] == "documento_nao_aprovado", rota


def test_nota_nao_revisada_bloqueia_aprovar(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen6")["id"]
    assert client.post(url(cid, "atestado"), headers=h).status_code == 409  # consulta ainda aberta
    say(client, h, cid, [{"falante": "medico", "texto": "Bom dia.", "t": 0}])
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    gerar(client, h, cid, "atestado")  # o rascunho pode ser gerado da nota ainda não revisada
    res = client.post(url(cid, "atestado", "/aprovar"), headers=h)
    assert res.status_code == 409 and res.json()["detail"]["codigo"] == "revisao_pendente"


def test_nota_alterada_depois_da_geracao_deixa_o_documento_desatualizado(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    aprovado(client, h, cid, "atestado")
    nota = client.get(f"/consultations/{cid}", headers=h).json()["nota"]
    nota["avaliacao"]["texto"] += " Reavaliar em 30 dias."
    assert client.put(f"/consultations/{cid}/note", json=nota | {"atestacao": True}, headers=h).status_code == 200
    estado = {d["tipo"]: d for d in client.get(url(cid), headers=h).json()["documentos"]}
    assert estado["atestado"]["desatualizado"] is True
    res = client.get(url(cid, "atestado", "/export"), headers=h)
    assert res.status_code == 409 and "mudou" in str(res.json()["detail"])
    assert client.post(url(cid, "atestado", "/aprovar"), headers=h).status_code == 409
    gerar(client, h, cid, "atestado")  # gerar de novo resolve
    assert client.post(url(cid, "atestado", "/aprovar"), headers=h).status_code == 200


# ---------- edição: item de segurança não sai, o que o profissional escreve fica marcado ----------
def test_edicao_marca_origem_profissional_e_tira_a_aprovacao(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    doc = aprovado(client, h, cid, "resumo_paciente")
    assert doc["aprovado"]
    secoes = doc["secoes"]
    alvo = secoes[1]["itens"][0]
    alvo["texto"] += " Tome com água."
    secoes[-1]["itens"].append({"texto": "Traga os exames na volta.", "refs": ["Fala/1"]})  # sem id: item novo
    res = client.put(url(cid, "resumo_paciente"), json={"secoes": secoes}, headers=h)
    assert res.status_code == 200, res.text
    novo = res.json()
    editado = next(i for i in itens(novo) if i["id"] == alvo["id"])
    assert editado["origem"] == "profissional" and editado["refs"] == alvo["refs"]
    escrito = next(i for i in itens(novo) if i["texto"] == "Traga os exames na volta.")
    assert escrito["origem"] == "profissional" and escrito["refs"] == []  # as refs não vêm do corpo do pedido
    assert novo["aprovado"] is None  # editar exige aprovar de novo


def test_edicao_nao_remove_alergia_nem_sinal_de_alarme(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    doc = gerar(client, h, cid, "resumo_paciente")
    for titulo in ("Alergias", "Sinais de alarme"):
        secoes = [s for s in doc["secoes"] if s["titulo"] != titulo]
        res = client.put(url(cid, "resumo_paciente"), json={"secoes": secoes}, headers=h)
        assert res.status_code == 422 and res.json()["detail"]["codigo"] == "item_obrigatorio", titulo
    vazio = [dict(s, itens=[i | {"texto": " "} if i["obrigatorio"] else i for i in s["itens"]]) for s in doc["secoes"]]
    assert client.put(url(cid, "resumo_paciente"), json={"secoes": vazio}, headers=h).status_code == 422
    # o documento guardado continua com os itens de segurança
    atual = client.get(url(cid, "resumo_paciente"), headers=h).json()
    assert {s["titulo"] for s in atual["secoes"]} >= {"Alergias", "Sinais de alarme"}


# ---------- receita, exames e encaminhamento só do que o profissional decidiu ----------
def test_receita_e_exames_so_existem_com_pedido_assinado(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    disp = {d["tipo"]: d["indisponivel"] for d in client.get(url(cid), headers=h).json()["documentos"]}
    assert disp["receita"] and disp["solicitacao_exames"] and disp["encaminhamento"]
    assert client.post(url(cid, "receita"), headers=h).status_code == 409
    assinar_exame(client, h, cid)
    doc = gerar(client, h, cid, "solicitacao_exames")
    assert any("Eletrocardiograma" in i["texto"] for i in itens(doc)) and all(i["refs"] for i in itens(doc))
    assert doc["loinc"]["codigo"] == "46209-3"


def test_encaminhamento_leva_o_card_decidido_com_resumo_da_consulta(client, auth, store):
    h = auth("medico")
    store.resources["Condition"]["cond-n40"] = hpb()[0]
    try:
        cid = start(client, h, "cen6")["id"]
        say(client, h, cid, [{"falante": "medico", "texto": VIRO, "t": 0}])
        ok = client.put(
            f"/consultations/{cid}/encaminhamentos/uro_hpb",
            json={"escolha": "encaminhar", "motivo": "Retenção urinária de repetição."},
            headers=h,
        )
        assert ok.status_code == 200, ok.text
        assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
        approve(client, h, cid)
        doc = gerar(client, h, cid, "encaminhamento")
        assert any(s["titulo"] == "Encaminhamento: Urologia" for s in doc["secoes"])
        assert doc["loinc"]["codigo"] == "57133-1"
        assert all(i["refs"] or i["origem"] == "profissional" for i in itens(doc))
    finally:
        store.resources["Condition"].pop("cond-n40", None)


# ---------- convênio: CID só com autorização ----------
def test_relatorio_de_convenio_sem_cid_ate_o_paciente_autorizar(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    sem = gerar(client, h, cid, "relatorio_convenio")
    com = gerar(client, h, cid, "relatorio_convenio", autorizacao_cid=True)
    assert not any("CID-10" in i["texto"] for i in itens(sem))
    assert any("CID-10" in i["texto"] for i in itens(com))
    assert all(i["refs"] or i["origem"] == "profissional" for i in itens(com))


# ---------- idioma do paciente: a conferência de fidelidade vale para cada item ----------
def test_versao_no_idioma_do_paciente_so_aceita_o_item_fiel():
    doc = {
        "secoes": [
            {
                "titulo": "Remédios",
                "itens": [
                    {"id": "s0.0", "texto": "Tome Losartana 50 mg de manhã.", "refs": ["a/1"], "origem": "regra"},
                    {"id": "s0.1", "texto": "Tome Metformina 850 mg de noite.", "refs": ["a/2"], "origem": "regra"},
                ],
            }
        ],
        "avisos": [],
        "idioma": "pt-BR",
    }

    def traduz(itens, idioma):
        return {
            "s0.0": "Take Losartan 50 mg in the morning.",
            "s0.1": "Take Metformin 500 mg at night.",  # dose trocada: tem de ser barrada
        }

    out = documentos.adaptar(doc, "en", traduz)
    por_id = {i["id"]: i for i in out["secoes"][0]["itens"]}
    assert por_id["s0.0"]["texto"].startswith("Take Losartan") and por_id["s0.0"]["texto_original"]
    assert por_id["s0.1"]["texto"] == "Tome Metformina 850 mg de noite."
    assert out["divergentes"][0]["id"] == "s0.1" and out["idioma"] == "en"
    sem = documentos.adaptar({**doc, "avisos": []}, "es", None)
    assert any("indisponível" in a for a in sem["avisos"])


# ---------- isolamento por organização e papéis ----------
def test_documentos_de_outra_organizacao_sao_404(client, auth):
    from tests.test_multiorg import cadastro_completo

    h = auth("medico")
    cid = consulta_pronta(client, h)
    gerar(client, h, cid, "atestado")
    hb, _ = cadastro_completo(client)
    assert client.get(url(cid), headers=hb).status_code == 404
    assert client.get(url(cid, "atestado"), headers=hb).status_code == 404
    assert client.post(url(cid, "atestado"), headers=hb).status_code == 404
    assert client.post(url(cid, "atestado", "/aprovar"), headers=hb).status_code == 404
    assert client.post(url(cid, "atestado", "/send"), headers=hb).status_code == 404


def test_tipo_desconhecido_e_404(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    assert client.post(url(cid, "laudo_inventado"), headers=h).status_code == 404
