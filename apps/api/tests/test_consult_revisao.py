"""Revisão segura (#14): rascunho da IA preservado, diff por seção, aceite com atestação e trava no servidor.

Tudo pelo caminho real (API → service → banco) sobre o cenário cen6 (consulta) e cen4 (cirurgia), em LLM_MODE=mock.
"""

import json

import pytest
from sqlalchemy import select

from app.consult import antiglosa
from app.consult import service as consult_service
from app.consult.export import document_reference
from app.consult.knowledge import demo_scripts
from app.db import session as db_session
from app.db.models import AuditLog
from tests.test_consult import SCRIPT, say, start
from tests.test_surgical import dictate
from tests.test_surgical import start as start_surgery

MARK = "ZXQ conduta combinada com a filha, retorno em duas semanas"


@pytest.fixture(autouse=True)
def _fresh_state():
    consult_service.clear_chart_cache()
    antiglosa.clear_cache()
    yield
    consult_service.clear_chart_cache()
    antiglosa.clear_cache()


def audit(action: str) -> list[dict]:
    with db_session.SessionLocal() as db:
        return [
            r.payload_json for r in db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id))
        ]


def consult_finished(client, h, accept_critical: bool = True) -> tuple[int, dict]:
    """Consulta cen6 inteira, finalizada. `accept_critical`: o profissional aceita os alertas críticos."""
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    if accept_critical:
        for m in ins["sugestoes_medicamentos"]:
            if m["severidade"] == "critico":
                res = client.post(
                    f"/consultations/{cid}/insight-actions",
                    json={"item_key": m["item_key"], "acao": "aceitar"},
                    headers=h,
                )
                assert res.status_code == 200, res.text
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200, done.text
    return cid, done.json()["nota"]


def review(client, h, cid, note, attest=True, expect=200):
    res = client.put(f"/consultations/{cid}/note", json=note | ({"atestacao": True} if attest else {}), headers=h)
    assert res.status_code == expect, res.text
    return res


def detail(res) -> dict:
    body = res.json()["detail"]
    return body if isinstance(body, dict) else {"mensagem": body}


# ======================= trava: revisão + atestação =======================
def test_send_without_review_and_without_attestation_is_409(client, auth):
    h = auth("medico")
    cid, note = consult_finished(client, h)
    # sem PUT: nem enviar nem exportar
    blocked = client.post(f"/consultations/{cid}/send", headers=h)
    assert blocked.status_code == 409 and detail(blocked)["codigo"] == "revisao_pendente"
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 409
    # com PUT, mas sem atestacao: continua 409
    review(client, h, cid, note, attest=False)
    no_attest = client.post(f"/consultations/{cid}/send", headers=h)
    assert no_attest.status_code == 409 and detail(no_attest)["codigo"] == "atestacao_pendente"
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 409
    # "atestacao": "sim" (texto) ou 1 não vale: só o booleano true
    for fake in ("sim", 1, "true"):
        res = client.put(f"/consultations/{cid}/note", json=note | {"atestacao": fake}, headers=h)
        assert res.status_code == 200 and res.json()["nota_meta"]["atestacao"] is False
    assert client.post(f"/consultations/{cid}/send", headers=h).status_code == 409
    # com a atestação, passa (EHR_WRITEBACK desligado: exporta)
    review(client, h, cid, note)
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 200
    sent = client.post(f"/consultations/{cid}/send", headers=h)
    assert sent.status_code == 200, sent.text
    assert sent.json()["recurso"]["docStatus"] == "final"


def test_editing_after_attestation_needs_a_new_attestation(client, auth):
    h = auth("medico")
    cid, note = consult_finished(client, h)
    review(client, h, cid, note)
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 200
    fora = client.get(f"/consultations/{cid}", headers=h).json()["cobertura"]["fora"]
    if fora:  # a nota mudou depois da atestação (fala incluída por botão): a atestação não vale para o texto novo
        inc = client.post(f"/consultations/{cid}/nota/incluir", json={"n": fora[0]["n"], "destino": "hda"}, headers=h)
        assert inc.status_code == 200, inc.text
        blocked = client.post(f"/consultations/{cid}/send", headers=h)
        assert blocked.status_code == 409 and detail(blocked)["codigo"] == "atestacao_pendente"
    # editar de novo sem reatestar também derruba a atestação
    review(client, h, cid, note, attest=False)
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 409


def test_document_reference_is_preliminary_until_approval():
    kwargs = dict(
        kind="consulta",
        patient_id="cen6",
        patient_name="Joana",
        encounter_id=None,
        author="Dra. A (Médico(a))",
        when=__import__("datetime").datetime(2026, 1, 1),
        text="texto",
        consultation_id=1,
    )
    assert document_reference(**kwargs, approved=False)["docStatus"] == "preliminary"
    assert document_reference(**kwargs)["docStatus"] == "final"


# ======================= rascunho da IA, versões e diff =======================
def test_edit_plano_generates_diff_and_audit_without_text(client, auth):
    h = auth("medico")
    cid, note = consult_finished(client, h)
    original_plano = note["plano"]["texto"]
    shown = client.get(f"/consultations/{cid}", headers=h).json()
    assert "rascunho_ia" not in shown["nota_meta"]  # a cópia da nota inteira não viaja na resposta

    edited = json.loads(json.dumps(note))
    edited["plano"]["texto"] = MARK
    res = review(client, h, cid, edited)
    meta = res.json()["nota_meta"]
    assert meta["revisada_por"] and meta["atestacao"] is True and len(meta["versoes"]) == 1

    diff = client.get(f"/consultations/{cid}/note/diff", headers=h)
    assert diff.status_code == 200, diff.text
    body = diff.json()
    sec = {s["secao"]: s for s in body["secoes"]}
    assert sec["plano"]["pct_editado"] > 0 and body["editado_pct"] > 0
    assert sec["plano"]["antes"] == original_plano and sec["plano"]["depois"] == MARK  # rascunho preservado
    assert sec["hda"]["pct_editado"] == 0.0 and sec["queixa_principal"]["pct_editado"] == 0.0
    assert body["revisada_por"] and body["atestacao"] is True

    # audit: % por seção, nunca o texto
    row = audit("consulta.nota_revisada")[-1]
    assert row["secoes_editadas"]["plano"] > 0 and row["atestada"] is True and row["editado_pct"] > 0
    raw = json.dumps(row, ensure_ascii=False)
    assert "ZXQ" not in raw and "conduta" not in raw and "Glibenclamida" not in raw

    # a segunda revisão cria outra versão e o rascunho continua o mesmo
    edited["plano"]["texto"] = MARK + " e ajuste da glibenclamida"
    again = review(client, h, cid, edited).json()["nota_meta"]
    assert [v["n"] for v in again["versoes"]] == [1, 2]
    second = client.get(f"/consultations/{cid}/note/diff", headers=h).json()
    assert {s["secao"]: s for s in second["secoes"]}["plano"]["antes"] == original_plano


def test_diff_before_finish_is_409(client, auth):
    h = auth("medico")
    cid = start(client, h)["id"]
    assert client.get(f"/consultations/{cid}/note/diff", headers=h).status_code == 409


# ======================= pendência crítica bloqueia o envio =======================
def test_critical_alert_not_accepted_blocks_send_until_justified(client, auth):
    h = auth("medico")
    cid, note = consult_finished(client, h, accept_critical=False)
    review(client, h, cid, note)
    blocked = client.post(f"/consultations/{cid}/send", headers=h)
    assert blocked.status_code == 409
    body = detail(blocked)
    assert body["codigo"] == "pendencia_critica" and body["pendencias"]
    assert {p["codigo"] for p in body["pendencias"]} == {"alerta_critico_nao_aceito"}
    short = client.post(f"/consultations/{cid}/send", json={"justificativa": "urgente"}, headers=h)
    assert short.status_code == 409
    text = "Risco discutido com a paciente e a filha"
    ok = client.post(f"/consultations/{cid}/send", json={"justificativa": text}, headers=h)
    assert ok.status_code == 200, ok.text
    row = audit("consulta.envio_com_pendencia")[-1]
    assert (
        row["justificativa"] == text and row["pendencias"] == ["alerta_critico_nao_aceito"] and row["quantidade"] >= 1
    )
    assert client.get(f"/consultations/{cid}", headers=h).json()["justificativa_pendencias"]["texto"] == text


def test_accepted_critical_alerts_do_not_block(client, auth):
    h = auth("medico")
    cid, note = consult_finished(client, h, accept_critical=True)
    review(client, h, cid, note)
    ok = client.post(f"/consultations/{cid}/send", headers=h)
    assert ok.status_code == 200, ok.text
    assert not audit("consulta.envio_com_pendencia")


def test_reported_allergy_missing_from_the_note_blocks_send(client, auth):
    h = auth("medico")
    cid, note = consult_finished(client, h)
    assert "camar" in note["alergias"]["texto"].lower()
    gone = json.loads(json.dumps(note))
    gone["alergias"]["texto"] = "Sem registro na consulta."
    review(client, h, cid, gone)
    blocked = client.post(f"/consultations/{cid}/send", headers=h)
    assert blocked.status_code == 409
    assert [p["codigo"] for p in detail(blocked)["pendencias"]] == ["alergia_nao_registrada"]
    review(client, h, cid, note)  # o profissional registra a alergia de novo
    assert client.post(f"/consultations/{cid}/send", headers=h).status_code == 200


# ======================= cirurgia: campo obrigatório vazio, OPME sem autorização =======================
def surgery_finished(client, h):
    cid = start_surgery(client, h, pid="cen4", especialidade="cirurgia_geral")["id"]
    dictate(client, h, cid, [(f["falante"], f["texto"]) for f in demo_scripts()["cen4_cirurgia"]["falas"]])
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200, done.text
    return cid, done.json()["nota"]


def test_surgical_required_field_empty_needs_justification(client, auth):
    h = auth("medico")
    cid, note = surgery_finished(client, h)
    empties = [f for f in note["relatorio_cirurgico"]["campos"] if f["obrigatorio"] and not f["texto"].strip()]
    assert empties, "o ditado do cen4 deixa campo obrigatório sem texto"
    review(client, h, cid, note)
    assert client.post(f"/consultations/{cid}/send", headers=h).status_code == 409
    text = "Campos complementados no prontuário físico"
    ok = client.post(f"/consultations/{cid}/send", json={"justificativa": text}, headers=h)
    assert ok.status_code == 200, ok.text
    row = audit("consulta.envio_com_pendencia")[-1]
    assert row["justificativa"] == text and "campo_obrigatorio_vazio" in row["pendencias"]


def test_surgical_clinical_gate_alone_uses_the_10_char_rule(client, auth, monkeypatch):
    """Sem a trava de faturamento (que pede 15), a trava de segurança libera com 10 caracteres e audita."""
    monkeypatch.setattr(consult_service, "_gate_billing", lambda *a, **k: None)
    h = auth("medico")
    cid, note = surgery_finished(client, h)
    review(client, h, cid, note)
    blocked = client.post(f"/consultations/{cid}/send", headers=h)
    assert blocked.status_code == 409 and detail(blocked)["codigo"] == "pendencia_critica"
    codes = {p["codigo"] for p in detail(blocked)["pendencias"]}
    assert "campo_obrigatorio_vazio" in codes
    assert client.post(f"/consultations/{cid}/send", json={"justificativa": "curto"}, headers=h).status_code == 409
    ok = client.post(f"/consultations/{cid}/send", json={"justificativa": "dez chars!"}, headers=h)
    assert ok.status_code == 200, ok.text
    assert audit("consulta.envio_com_pendencia")[-1]["justificativa"] == "dez chars!"


def test_surgical_opme_without_authorization_is_pending_until_justified(client, auth, monkeypatch):
    monkeypatch.setattr(consult_service, "_gate_billing", lambda *a, **k: None)
    h = auth("medico")
    cid, note = surgery_finished(client, h)
    report = note["relatorio_cirurgico"]
    unauthorized = [o for o in report["opme"] if o["status"] == "nao_autorizado"]
    assert unauthorized
    review(client, h, cid, note)
    blocked = client.post(f"/consultations/{cid}/send", headers=h)
    assert "opme_sem_autorizacao" in {p["codigo"] for p in detail(blocked)["pendencias"]}
    # o cirurgião justifica a linha na revisão: a pendência de OPME sai (sobra a do campo, que ele também resolve)
    edited = json.loads(json.dumps(note))
    for o in edited["relatorio_cirurgico"]["opme"]:
        if o["status"] == "nao_autorizado":
            o["justificativa"] = "Material autorizado por telefone pelo auditor do convênio"
    for f in edited["relatorio_cirurgico"]["campos"]:
        if f["obrigatorio"] and not f["texto"].strip():
            f["texto"] = "Descrito no prontuário físico."
    review(client, h, cid, edited)
    ok = client.post(f"/consultations/{cid}/send", headers=h)
    assert ok.status_code == 200, ok.text


def test_surgical_recompute_follows_the_edited_report(client, auth):
    h = auth("medico")
    cid, note = surgery_finished(client, h)
    report = note["relatorio_cirurgico"]
    empty = next(f for f in report["campos"] if f["obrigatorio"] and not f["texto"].strip())
    prefix = f"Campo obrigatório não ditado: {empty['rotulo']}"
    assert any(p["texto"].startswith(prefix) for p in report["pendencias"])
    edited = json.loads(json.dumps(note))
    for f in edited["relatorio_cirurgico"]["campos"]:
        if f["campo"] == empty["campo"]:
            f["texto"] = "Preenchido na revisão."
    out = client.post(f"/consultations/{cid}/surgical/recompute", json=edited, headers=h)
    assert out.status_code == 200, out.text
    rep = out.json()["relatorio_cirurgico"]
    assert not any(p["texto"].startswith(prefix) for p in rep["pendencias"])
    assert rep["prontidao"]["score"] >= report["prontidao"]["score"]
    # nada foi salvo: a nota do servidor segue com a pendência até a revisão ser salva
    saved = client.get(f"/consultations/{cid}", headers=h).json()["nota"]["relatorio_cirurgico"]
    assert any(p["texto"].startswith(prefix) for p in saved["pendencias"])
    # e voltando a esvaziar o campo, a pendência reaparece
    for f in edited["relatorio_cirurgico"]["campos"]:
        if f["campo"] == empty["campo"]:
            f["texto"] = ""
    again = client.post(f"/consultations/{cid}/surgical/recompute", json=edited, headers=h).json()[
        "relatorio_cirurgico"
    ]
    assert any(p["texto"].startswith(prefix) for p in again["pendencias"])


def test_surgical_diff_names_the_edited_field(client, auth):
    h = auth("medico")
    cid, note = surgery_finished(client, h)
    edited = json.loads(json.dumps(note))
    field = next(f for f in edited["relatorio_cirurgico"]["campos"] if f["obrigatorio"] and not f["texto"].strip())
    field["texto"] = "Texto escrito pelo cirurgião na revisão"
    review(client, h, cid, edited)
    sec = {s["secao"]: s for s in client.get(f"/consultations/{cid}/note/diff", headers=h).json()["secoes"]}
    assert sec[f"campo:{field['campo']}"]["pct_editado"] == 100.0


# ======================= CID: autocomplete, adicionar, trocar, principal =======================
def test_cid_autocomplete_add_swap_and_main(client, auth):
    h = auth("medico")
    found = client.get("/consult/cid10?q=hipoglicemia", headers=h).json()
    assert found and all({"codigo", "descricao"} <= set(x) for x in found)
    assert client.get("/consult/cid10?q=E16.0", headers=h).json()[0]["codigo"] == "E16.0"
    assert client.get("/consult/cid10?q=x", headers=h).json() == []  # busca curta não lista a tabela inteira

    cid, note = consult_finished(client, h)
    edited = json.loads(json.dumps(note))
    edited["cids"] = [{"codigo": "e11.9", "descricao": "qualquer coisa", "tipo": "principal", "refs": []}] + [
        c | {"tipo": "secundario"} for c in edited["cids"] if c["codigo"] != "E11.9"
    ]
    res = review(client, h, cid, edited)
    saved = {c["codigo"]: c for c in res.json()["nota"]["cids"]}
    assert (
        saved["E11.9"]["tipo"] == "principal" and saved["E11.9"]["descricao"] != "qualquer coisa"
    )  # descrição oficial
    assert sum(c["tipo"] == "principal" for c in saved.values()) == 1

    bad = json.loads(json.dumps(edited))
    bad["cids"].append({"codigo": "J99.9", "descricao": "inventado", "tipo": "secundario", "refs": []})
    assert review(client, h, cid, bad, expect=422).status_code == 422
    two = json.loads(json.dumps(edited))
    two["cids"] = [c | {"tipo": "principal"} for c in two["cids"]]
    assert review(client, h, cid, two, expect=422).status_code == 422


def test_draft_stays_comparable_after_export(client, auth):
    h = auth("medico")
    cid, note = consult_finished(client, h)
    review(client, h, cid, note)
    assert client.post(f"/consultations/{cid}/send", headers=h).status_code == 200
    diff = client.get(f"/consultations/{cid}/note/diff", headers=h).json()
    assert diff["editado_pct"] == 0.0 and diff["atestacao"] is True  # revisou sem mudar nada: 0% editado, mas atestou


# ======================= verificação: saídas que contornariam a trava =======================
def test_export_is_blocked_by_critical_pending_until_the_send_was_justified(client, auth):
    h = auth("medico")
    cid, note = consult_finished(client, h, accept_critical=False)
    review(client, h, cid, note)
    blocked = client.get(f"/consultations/{cid}/export", headers=h)  # copiar/baixar também é sair da nota
    assert blocked.status_code == 409 and detail(blocked)["codigo"] == "pendencia_critica"
    ok = client.post(f"/consultations/{cid}/send", json={"justificativa": "Risco discutido com a paciente"}, headers=h)
    assert ok.status_code == 200, ok.text
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 200


@pytest.mark.parametrize("invisible", ["​" * 12, "  　 " * 4, "\t\n\r" * 5, "​⁠﻿" * 4])
def test_invisible_characters_are_not_a_justification(client, auth, invisible):
    h = auth("medico")
    cid, note = consult_finished(client, h, accept_critical=False)
    review(client, h, cid, note)
    res = client.post(f"/consultations/{cid}/send", json={"justificativa": invisible}, headers=h)
    assert res.status_code == 409 and detail(res)["codigo"] == "pendencia_critica"
    assert not audit("consulta.envio_com_pendencia")


def test_critical_alert_cannot_be_hidden_without_a_reason(client, auth):
    """ "Não se aplica" tirava o alerta crítico da lista (e da trava de envio) sem motivo nenhum."""
    h = auth("medico")
    cid = start(client, h)["id"]
    ins = say(client, h, cid, SCRIPT)
    critical = [m for m in ins["sugestoes_medicamentos"] if m["severidade"] == "critico"]
    assert critical
    for reason in ("", "   ", "curto", "​" * 12):
        for action in ("nao_se_aplica", "descartar"):
            res = client.post(
                f"/consultations/{cid}/insight-actions",
                json={"item_key": critical[0]["item_key"], "acao": action, "motivo": reason},
                headers=h,
            )
            assert res.status_code == 422, (action, reason, res.text)
    ok = client.post(
        f"/consultations/{cid}/insight-actions",
        json={
            "item_key": critical[0]["item_key"],
            "acao": "nao_se_aplica",
            "motivo": "Paciente não usa mais este fármaco",
        },
        headers=h,
    )
    assert ok.status_code == 200, ok.text


def test_negated_allergy_in_the_note_is_not_a_registration(client, auth):
    h = auth("medico")
    cid, note = consult_finished(client, h)
    denied = json.loads(json.dumps(note))
    denied["alergias"]["texto"] = "Nega alergia a camarão."
    review(client, h, cid, denied)
    blocked = client.post(f"/consultations/{cid}/send", headers=h)
    assert blocked.status_code == 409
    assert "alergia_nao_registrada" in [p["codigo"] for p in detail(blocked)["pendencias"]]
