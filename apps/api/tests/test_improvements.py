"""Melhorias do benchmark (docs/benchmark-lya.md): conferência verbal, documentos de alta e feedback da IA."""

from app.discharge.documents import home_meds

GOOD_CHECKLIST = dict(
    mora_sozinho=True,
    tem_cuidador=True,
    cuidador_capacitado=True,
    escadas_em_casa=False,
    mobilidade_reduzida=True,
    acesso_medicamentos=True,
    precisa_dispositivos=False,
    consulta_retorno_agendada=True,
    teach_back_realizado=True,
)


def test_verbal_check_flags_divergence_and_omissions(client, auth):
    h = auth("enfermagem")
    text = (
        "Seu Antônio veio da UTI, pós-op de colecistectomia, PA estável, sem intercorrências. "
        "Tem dreno em flanco direito. Ceftriaxona às 20h."
    )
    res = client.post("/patients/cen1/handoff/check", json={"audience": "enfermagem", "texto": text}, headers=h)
    assert res.status_code == 200, res.text
    out = res.json()
    by_status = {i["status"] for i in out["itens"]}
    assert "divergente" in by_status and "omitido" in by_status
    assert any(i["status"] == "divergente" and "PA" in i["fato"] for i in out["itens"])
    assert any(i["status"] == "omitido" and "Punção venosa" in i["fato"] for i in out["itens"])
    assert all(i["refs"] for i in out["itens"])
    assert out["contagem"]["divergente"] >= 1


def test_verbal_check_full_report_has_good_coverage(client, auth):
    h = auth("enfermagem")
    text = (
        "PA subindo até 184/102, comunicado plantonista. Não urinou desde a retirada da sonda vesical. "
        "Risco de queda alto, grades elevadas. AVP com 72h, avaliar troca. "
        "Losartana nova às 20h no lugar do anlodipino. Enoxaparina às 18h com dupla checagem."
    )
    out = client.post("/patients/cen1/handoff/check", json={"audience": "enfermagem", "texto": text}, headers=h).json()
    assert out["contagem"]["omitido"] == 0
    assert out["cobertura"] >= 0.8


def test_verbal_check_does_not_store_free_text_in_audit(client, auth):
    h = auth("enfermagem")
    client.post(
        "/patients/cen1/handoff/check",
        json={"audience": "enfermagem", "texto": "Texto clínico secreto aqui."},
        headers=h,
    )
    audit = client.get("/audit?action=handoff.conferencia", headers=auth("gestor")).json()
    assert audit["items"] and "secreto" not in str(audit["items"])


def test_discharge_documents_cite_sources_and_keep_home_meds_only(client, auth, record):
    h = auth("medico")
    client.post("/patients/cen3/discharge-assessment", json={"social_checklist": GOOD_CHECKLIST}, headers=h)
    res = client.post("/patients/cen3/discharge-documents", json={"idioma": "pt-BR"}, headers=h)
    assert res.status_code == 200, res.text
    doc = res.json()
    assert set(doc["soap"]) == {"subjetivo", "objetivo", "avaliacao", "plano"}
    assert all(sec["refs"] for sec in doc["soap"].values())
    ori = doc["orientacoes"]
    names = {m["medicamento"] for m in ori["medicamentos"]}
    assert {"Insulina NPH", "Carvedilol"} <= names
    assert all(m["refs"] for m in ori["medicamentos"])
    alerts = " ".join(s["texto"] for s in ori["sinais_de_alerta"])
    assert "açúcar baixo" in alerts and "2 kg" in alerts  # insulina nova + insuficiência cardíaca
    assert "agendado" in ori["retorno"]["texto"]
    assert "Sebastião" not in str(doc)  # conteúdo pseudonimizado
    latest = client.get("/patients/cen3/discharge-documents/latest", headers=h).json()
    assert latest["id"] == doc["id"]


def test_home_meds_exclude_intravenous(record):
    rec = record("cen1")
    routes = {m.route for m in home_meds(rec)}
    assert "IV" not in routes


def test_feedback_and_quality_metrics(client, auth):
    h = auth("medico")
    assert (
        client.post("/feedback", json={"kind": "handoff", "useful": True, "patient_id": "cen1"}, headers=h).status_code
        == 200
    )
    no_comment = client.post("/feedback", json={"kind": "medrec", "useful": False}, headers=h)
    assert no_comment.status_code == 422
    assert (
        client.post(
            "/feedback", json={"kind": "medrec", "useful": False, "comment": "faltou a dose"}, headers=h
        ).status_code
        == 200
    )
    m = client.get("/audit/metrics", headers=auth("gestor")).json()
    assert m["feedback_ia"]["handoff"]["util"] == 1 and m["feedback_ia"]["medrec"]["nao_util"] == 1
    assert m["taxa_utilidade_ia"] == 0.5
