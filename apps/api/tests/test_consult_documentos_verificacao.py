"""#45 verificado: casos adversariais dos documentos derivados (CID por fora da autorização, pedido que muda depois da
aprovação, papel que aprova, reenvio, tradução infiel, exclusão LGPD)."""

import re

import pytest

from app.consult import documentos
from app.consult import service as consult_service
from tests.test_consult import start
from tests.test_consult_documentos import aprovado, assinar_exame, gerar, itens, url
from tests.test_consult_documentos import writeback as writeback  # fixture
from tests.test_consult_encaminhamento import VIRO, hpb
from tests.test_consult_envio_prontuario import consulta_pronta, falas

CODIGO_CID = re.compile(r"\b[A-TV-Z]\d{2}(?:\.\d{1,2})?\b")
AMOXI = ("medico", "Vou começar amoxicilina 500 mg de 8 em 8 horas por 7 dias.")


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def pedido(client, h, cid, nome):
    ins = client.get(f"/consultations/{cid}", headers=h).json()["insights"]
    return next(d for d in ins["pedidos"] if d["item"] == nome)


def decidir(client, h, cid, nome, **body):
    return client.put(f"/consultations/{cid}/pedidos/{pedido(client, h, cid, nome)['id']}", json=body, headers=h)


# ---------- CID: o código não pode vazar por outra seção quando o paciente não autorizou ----------
def test_relatorio_de_convenio_sem_autorizacao_nao_traz_codigo_de_cid_em_nenhuma_linha(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    doc = gerar(client, h, cid, "relatorio_convenio")
    vazou = [i["texto"] for i in itens(doc) if CODIGO_CID.search(i["texto"])]
    assert not vazou, vazou
    assert any(s["titulo"] == "Avaliação" for s in doc["secoes"])  # o diagnóstico em palavras continua
    com = gerar(client, h, cid, "relatorio_convenio", autorizacao_cid=True)
    assert any(CODIGO_CID.search(i["texto"]) for i in itens(com))


def test_atestado_e_exames_sem_autorizacao_nao_trazem_codigo_de_cid(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    assinar_exame(client, h, cid)
    for tipo in ("atestado", "solicitacao_exames"):
        doc = gerar(client, h, cid, tipo, incluir_cid=True)
        assert not [i["texto"] for i in itens(doc) if CODIGO_CID.search(i["texto"])], tipo


# ---------- receita e exames: só o que o médico assinou, e o crítico só com justificativa ----------
def test_receita_de_item_critico_so_existe_depois_da_assinatura_com_justificativa(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h, extra=(AMOXI,))
    assert client.post(url(cid, "receita"), headers=h).status_code == 409  # rascunho pendente não vira receita
    assert decidir(client, h, cid, "Amoxicilina", estado="assinado").status_code == 409  # crítico sem justificativa
    assert client.post(url(cid, "receita"), headers=h).status_code == 409
    ok = decidir(client, h, cid, "Amoxicilina", estado="assinado", justificativa="Dessensibilização acompanhada.")
    assert ok.status_code == 200, ok.text
    doc = gerar(client, h, cid, "receita")
    assert any("Amoxicilina" in i["texto"] and i["refs"] for i in itens(doc))


def test_recusar_o_pedido_nao_o_deixa_na_receita(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h, extra=(AMOXI,))
    assert decidir(client, h, cid, "Amoxicilina", estado="recusado").status_code == 200
    assert client.post(url(cid, "receita"), headers=h).status_code == 409


def test_pedido_que_volta_a_pendente_depois_da_aprovacao_derruba_o_documento(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    assinar_exame(client, h, cid)
    aprovado(client, h, cid, "solicitacao_exames")
    assert client.get(url(cid, "solicitacao_exames", "/export"), headers=h).status_code == 200
    assert decidir(client, h, cid, "Eletrocardiograma", estado="pendente").status_code == 200
    estado = {d["tipo"]: d for d in client.get(url(cid), headers=h).json()["documentos"]}
    assert estado["solicitacao_exames"]["desatualizado"] is True
    res = client.get(url(cid, "solicitacao_exames", "/export"), headers=h)
    assert res.status_code == 409, res.text
    assert client.post(url(cid, "solicitacao_exames", "/aprovar"), headers=h).status_code == 409


def test_pedido_assinado_depois_nao_derruba_o_atestado_que_nao_depende_dele(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    aprovado(client, h, cid, "atestado")
    assinar_exame(client, h, cid)
    assert client.get(url(cid, "atestado", "/export"), headers=h).status_code == 200


# ---------- encaminhamento: só o card decidido ----------
def test_encaminhamento_de_card_nao_decidido_ou_manejado_na_aps_nao_sai(client, auth, store):
    h = auth("medico")
    store.resources["Condition"]["cond-n40"] = hpb()[0]
    try:
        cid = start(client, h, "cen6")["id"]
        assert falas(client, h, cid, ("medico", VIRO)).status_code == 200
        assert client.post(url(cid, "encaminhamento"), headers=h).status_code == 409  # card pendente
        manejar = client.put(
            f"/consultations/{cid}/encaminhamentos/uro_hpb", json={"escolha": "manejar_aps"}, headers=h
        )
        assert manejar.status_code == 200, manejar.text
        assert client.post(url(cid, "encaminhamento"), headers=h).status_code == 409
    finally:
        store.resources["Condition"].pop("cond-n40", None)


# ---------- nada sai com nota não atestada, pendência crítica ou aprovação de quem não pode ----------
def test_nota_que_perde_a_atestacao_bloqueia_exportar_e_enviar(client, auth, writeback):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    aprovado(client, h, cid, "atestado")
    nota = client.get(f"/consultations/{cid}", headers=h).json()["nota"]
    assert client.put(f"/consultations/{cid}/note", json=nota, headers=h).status_code == 200  # salva sem atestar
    for rota, metodo in (("/export", client.get), ("/export.pdf", client.get), ("/send", client.post)):
        res = metodo(url(cid, "atestado", rota), headers=h)
        assert res.status_code == 409, rota
        assert res.json()["detail"]["codigo"] in ("atestacao_pendente", "revisao_pendente"), rota


def test_pendencia_critica_aberta_bloqueia_exportar_e_enviar_o_documento(client, auth, writeback):
    h = auth("medico")
    cid = start(client, h, "cen6")["id"]
    res = falas(client, h, cid, ("medico", "Bom dia."), ("paciente", "Tenho alergia a iodo, contraste."))
    assert res.status_code == 200
    assert client.post(f"/consultations/{cid}/finish", headers=h).status_code == 200
    nota = client.get(f"/consultations/{cid}", headers=h).json()["nota"]
    nota["alergias"] = {"texto": "Sem registro de alergia.", "refs": [], "frases": []}
    assert client.put(f"/consultations/{cid}/note", json=nota | {"atestacao": True}, headers=h).status_code == 200
    aprovado(client, h, cid, "atestado")
    for rota, metodo in (("/export", client.get), ("/send", client.post)):
        res = metodo(url(cid, "atestado", rota), headers=h)
        assert res.status_code == 409 and res.json()["detail"]["codigo"] == "pendencia_critica", rota


def test_enfermagem_nao_aprova_receita_atestado_nem_pedido_de_exames(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    assinar_exame(client, h, cid)
    enf = auth("enfermagem")
    for tipo in ("atestado", "solicitacao_exames"):
        gerar(client, h, cid, tipo)
        res = client.post(url(cid, tipo, "/aprovar"), headers=enf)
        assert res.status_code == 403, (tipo, res.text)
        assert client.post(url(cid, tipo, "/aprovar"), headers=h).status_code == 200
    gerar(client, h, cid, "resumo_paciente")  # o resumo ao paciente a enfermagem pode aprovar
    assert client.post(url(cid, "resumo_paciente", "/aprovar"), headers=enf).status_code == 200


# ---------- envio: idempotente, um DocumentReference por documento, sem identifier repetido ----------
def test_reenvio_e_idempotente_e_o_prontuario_fica_com_um_documentreference_por_documento(
    client, auth, store, writeback
):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    antes = set(store.resources.get("DocumentReference", {}))
    for tipo in ("atestado", "resumo_paciente"):
        aprovado(client, h, cid, tipo)
        assert client.post(url(cid, tipo, "/send"), headers=h).status_code == 200
        outra = client.post(url(cid, tipo, "/send"), headers=h)
        assert outra.status_code == 409 and "já enviado" in outra.json()["detail"]
        assert client.post(url(cid, tipo), headers=h).status_code == 409  # gerar de novo não duplica
        assert client.put(url(cid, tipo), json={"secoes": []}, headers=h).status_code == 409
    novos = [store.resources["DocumentReference"][i] for i in set(store.resources["DocumentReference"]) - antes]
    ids = [d["identifier"][0]["value"] for d in novos if d["identifier"][0]["system"] == documentos.SYSTEM]
    assert sorted(ids) == sorted([f"{cid}:atestado", f"{cid}:resumo_paciente"])


# ---------- idioma do paciente: dose, alergia e negação trocadas não passam ----------
def _doc(texto):
    item = {"id": "s0.0", "texto": texto, "refs": ["x/1"], "origem": "regra"}
    return {"secoes": [{"titulo": "T", "itens": [item]}], "avisos": [], "idioma": "pt-BR"}


@pytest.mark.parametrize(
    ("original", "traduzido"),
    [
        (
            "Você tem alergia a: Penicilinas (amoxicilina). Avise sempre que for atendido.",
            "You are allergic to: sulfonamides (sulfa). Tell staff.",
        ),
        ("Tome Losartana 50 mg de manhã.", "Take Losartan 150 mg in the morning."),
        ("Tome Losartana 50 mg de manhã.", "Take Losartan 50 mg in the evening."),
        ("Pare de tomar Glibenclamida.", "Take Glibenclamide."),
        ("Não tome ibuprofeno.", "Take ibuprofen."),
        ("Tome Metformina 850 mg, 2 vezes ao dia.", "Take Metformin 850 mg once a day."),
    ],
)
def test_traducao_que_muda_dose_alergia_ou_negacao_fica_em_portugues(original, traduzido):
    out = documentos.adaptar(_doc(original), "en", lambda itens_, idioma: {"s0.0": traduzido})
    assert out["secoes"][0]["itens"][0]["texto"] == original, out
    assert out["divergentes"]


# ---------- códigos, legibilidade, cirurgia e LGPD ----------
def test_loinc_dos_tipos_sao_codigos_distintos_e_com_o_nome_oficial():
    codigos = [t["loinc"][0] for t in documentos.TIPOS.values()]
    assert len(set(codigos)) == len(codigos)
    assert documentos.TIPOS["relatorio_convenio"]["loinc"] == ("34133-9", "Summarization of episode note")
    assert documentos.TIPOS["resumo_paciente"]["loinc"] == ("69730-0", "Instructions")
    assert documentos.TIPOS["receita"]["loinc"][0] == "57833-6"


def test_legibilidade_sem_pontuacao_e_com_numero_decimal():
    assert documentos.legibilidade("Tome 1,5 comprimido de manhã")["frases"] == 1
    assert documentos.legibilidade("   ...  ")["indice"] is None
    assert 0 <= documentos.legibilidade("palavra " * 400)["indice"] <= 100


def test_exclusao_lgpd_apaga_os_documentos_junto_com_a_consulta(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    aprovado(client, h, cid, "atestado")
    assert client.delete("/patients/cen6/consultation-data", headers=auth("gestor")).status_code == 200
    assert client.get(url(cid, "atestado"), headers=h).status_code == 404
    assert client.get(url(cid), headers=h).status_code == 404


def test_outra_organizacao_nao_edita_nem_exporta(client, auth):
    from tests.test_multiorg import cadastro_completo

    h = auth("medico")
    cid = consulta_pronta(client, h)
    aprovado(client, h, cid, "atestado")
    hb, _ = cadastro_completo(client)
    assert client.put(url(cid, "atestado"), json={"secoes": []}, headers=hb).status_code == 404
    assert client.get(url(cid, "atestado", "/export"), headers=hb).status_code == 404
    assert client.get(url(cid, "atestado", "/export.pdf"), headers=hb).status_code == 404


def test_sem_codigo_cid_tira_so_o_codigo_e_preserva_o_resto():
    f = documentos.sem_codigo_cid
    assert f("Hipóteses: Hipercalemia (E87.5, confiança media) ; Gripe (J11, confiança baixa)") == (
        "Hipóteses: Hipercalemia (confiança media); Gripe (confiança baixa)"
    )
    assert f("Piora renal (N17.9)") == "Piora renal"
    assert f("Vitamina B12 baixa; CID N39.0 anotado") == "Vitamina B12 baixa; CID anotado"
    assert f("Tomar 2 comprimidos A1 por dia") == "Tomar 2 comprimidos A1 por dia"


def test_edicao_nao_tira_o_alergeno_da_linha_de_alergia(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    doc = gerar(client, h, cid, "resumo_paciente")
    alergia = next(i for i in itens(doc) if i["texto"].startswith("Você tem alergia a"))
    secoes = [
        dict(s, itens=[i | {"texto": "Você tem alergia a: nada."} if i is alergia else i for i in s["itens"]])
        for s in doc["secoes"]
    ]
    secoes = [
        dict(
            s,
            itens=[
                i | ({"texto": "Você tem alergia a: nada."} if i["id"] == alergia["id"] else {}) for i in s["itens"]
            ],
        )
        for s in doc["secoes"]
    ]
    res = client.put(url(cid, "resumo_paciente"), json={"secoes": secoes}, headers=h)
    assert res.status_code == 422 and res.json()["detail"]["codigo"] == "item_obrigatorio"
    ok = [
        dict(
            s,
            itens=[
                i | ({"texto": alergia["texto"] + " Mostre a receita."} if i["id"] == alergia["id"] else {})
                for i in s["itens"]
            ],
        )
        for s in doc["secoes"]
    ]
    assert client.put(url(cid, "resumo_paciente"), json={"secoes": ok}, headers=h).status_code == 200


def test_alergeno_so_passa_na_traducao_se_aparece_como_cognato():
    assert documentos.alergeno_presente("Iodo e contraste iodado", "Allergic to iodine and iodinated contrast")
    assert documentos.alergeno_presente("Penicilinas (amoxicilina)", "Penicillins (amoxicillin)")
    assert not documentos.alergeno_presente("Iodo e contraste iodado", "Allergic to latex")
    assert not documentos.alergeno_presente("Penicilinas (amoxicilina)", "Allergic to penicillins")


def test_envio_que_caiu_depois_de_gravar_nao_duplica_o_documentreference(client, auth, store, writeback):
    from app.db import session as db_session
    from app.db.models import Consultation

    h = auth("medico")
    cid = consulta_pronta(client, h)
    aprovado(client, h, cid, "atestado")
    primeiro = client.post(url(cid, "atestado", "/send"), headers=h).json()["fhir_ref"]
    with db_session.SessionLocal() as db:  # como se a gravação do estado local tivesse falhado depois do FHIR
        c = db.get(Consultation, cid)
        docs = dict(c.extra_json["documentos"])
        docs["atestado"] = {k: v for k, v in docs["atestado"].items() if k != "fhir_ref"}
        c.extra_json = c.extra_json | {"documentos": docs}
        db.commit()
    segundo = client.post(url(cid, "atestado", "/send"), headers=h).json()["fhir_ref"]
    assert segundo == primeiro
    mesmos = [
        d
        for d in store.resources["DocumentReference"].values()
        if d.get("identifier", [{}])[0].get("value") == f"{cid}:atestado"
    ]
    assert len(mesmos) == 1
