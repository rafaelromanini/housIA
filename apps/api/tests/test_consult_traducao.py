"""Tradução no nível da Lya (#22): 95+ idiomas, idioma do FHIR, regras sobre a tradução e fidelidade."""

import pytest

from app.consult import ai as consult_ai
from app.consult import fidelidade, idiomas
from app.consult import service as consult_service
from app.consult.knowledge import demo_scripts
from app.discharge.documents import Language as DischargeLanguage
from tests.conftest import CONSENT
from tests.test_consult import approve, start

HISPANICA = demo_scripts()["cen_hispanica"]


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def falas(client, h, cid, itens):
    payload = {"segmentos": [{"origem": "demo", **i} for i in itens]}
    res = client.post(f"/consultations/{cid}/segments", json=payload, headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def play_script(client, h, cid):
    res = falas(
        client,
        h,
        cid,
        [
            {"falante": f["falante"], "texto": f["texto"], "t": f["t"], "idioma": f["idioma"]}
            for f in HISPANICA["falas"]
        ],
    )
    return res["insights"]


def tag(ins, rotulo):
    return next((t for t in ins["queixas"] if t["rotulo"] == rotulo), None)


# ---------- (1) idiomas ----------
def test_config_lists_at_least_95_languages_with_speech_codes(client, auth):
    cfg = client.get("/consult/config", headers=auth("medico")).json()
    langs = cfg["idiomas"]
    assert len(langs) >= 95
    codes = {i["value"] for i in langs}
    assert {"pt-BR", "es", "en", "fr", "it", "de", "zh", "ar", "ht"} <= codes  # os 9 de antes continuam
    assert len(codes) == len(langs)  # sem repetição
    es = next(i for i in langs if i["value"] == "es")
    assert es["label"] == "Español" and es["web_speech"] == "es-ES" and es["whisper"] == "es"
    assert all(i["label"] for i in langs)


def test_language_is_validated_against_the_csv(client, auth):
    h = auth("medico")
    bad = client.post(
        "/patients/cen6/consultations",
        json={"tipo": "consulta", "idioma_paciente": "klingon", "consentimento": CONSENT},
        headers=h,
    )
    assert bad.status_code == 422
    ok = start(client, h, idioma_paciente="sw")  # suaíli: antes não existia
    assert ok["idioma_paciente"] == "sw"
    regional = start(client, h, idioma_paciente="es-MX")  # regional cai no idioma-base
    assert regional["idioma_paciente"] == "es"


def test_discharge_documents_accept_the_same_languages():
    assert DischargeLanguage.__metadata__[0].func("ru") == "ru"
    with pytest.raises(ValueError):
        DischargeLanguage.__metadata__[0].func("zzz")


def test_discharge_documents_fall_back_to_ptbr_in_mock(client, auth):
    h = auth("medico")
    res = client.post("/patients/cen3/discharge-documents", json={"idioma": "ru"}, headers=h)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["idioma"] == "ru" and body["meta"]["traducao_disponivel"] is False
    assert body["orientacoes"]["titulo"]  # conteúdo em pt-BR no mock


# ---------- (2) idioma vindo do FHIR ----------
def test_patient_communication_preselects_the_language(client, auth):
    h = auth("medico")
    got = client.get("/patients/cen8/consult/idioma", headers=h).json()
    assert got == {"idioma": "es", "rotulo": "Español", "fonte": "Patient.communication"}
    assert client.get("/patients/cen6/consult/idioma", headers=h).json()["idioma"] == "pt-BR"
    # sem idioma no corpo, a consulta nasce em espanhol
    res = client.post("/patients/cen8/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=h)
    assert res.status_code == 200, res.text
    assert res.json()["idioma_paciente"] == "es"
    # idioma explícito do profissional vale mais que o do cadastro
    assert start(client, h, "cen8", idioma_paciente="en")["idioma_paciente"] == "en"


def test_patient_communication_prefers_the_preferred_entry():
    patient = {
        "communication": [
            {"language": {"coding": [{"code": "pt"}]}},
            {"language": {"coding": [{"system": "urn:ietf:bcp:47", "code": "ht"}]}, "preferred": True},
        ]
    }
    assert idiomas.do_paciente(patient) == ("ht", "Patient.communication")
    assert idiomas.do_paciente({}) == (None, "")


# ---------- (4) regras sobre a tradução ----------
def test_rules_read_spanish_before_any_translation(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    res = falas(client, h, cid, [{"falante": "paciente", "texto": "me duele el pecho desde ayer", "t": 1}])
    t = tag(res["insights"], "Dor no peito")
    assert t and t["status"] == "presente" and t["refs"] == ["Fala/1"]
    assert res["novos"][0]["idioma"] == "es"


def test_translated_utterance_feeds_the_engine_keeping_fala_n(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    falas(
        client,
        h,
        cid,
        [
            {"falante": "medico", "texto": "Bom dia! Como a senhora está?", "t": 0},
            {"falante": "paciente", "texto": "Buenos días, doctora. Me duele el pecho desde ayer.", "t": 3},
        ],
    )
    out = client.post(f"/consultations/{cid}/translate", json={}, headers=h)
    assert out.status_code == 200, out.text
    seg = client.get(f"/consultations/{cid}", headers=h).json()["segmentos"]
    assert seg[1]["traducao"] == "Bom dia, doutora. Estou com dor no peito desde ontem."  # gravada no roteiro
    assert seg[1]["traducao_divergente"] == []
    t = tag(out.json()["insights"], "Dor no peito")
    assert t and t["status"] == "presente" and t["refs"] == ["Fala/2"]
    # o resumo e o alerta de "Relata: dor no peito" citam a mesma fala
    assert any(
        "dor no peito" in a["texto"].lower() and a["refs"] == ["Fala/2"]
        for a in out.json()["insights"]["resumo"]["destaques"]
    )


@pytest.mark.parametrize(
    ("texto", "status"),
    [
        ("No me duele el pecho, doctora.", "negado"),
        ("Nunca he tenido dolor de pecho.", "negado"),
        ("Sin dolor de pecho desde que empecé el tratamiento.", "negado"),
        ("I have chest pain since yesterday", "presente"),
        ("I do not have chest pain", "negado"),
        ("Never had chest pain.", "negado"),
        ("Tengo dolor en el pecho cuando camino.", "presente"),
    ],
)
def test_foreign_negation_does_not_create_false_positives(client, auth, texto, status):
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    idioma = "en" if texto.split()[0] in ("I", "Never") else "es"
    ins = falas(client, h, cid, [{"falante": "paciente", "texto": texto, "t": 1, "idioma": idioma}])["insights"]
    queixa = [t for t in ins["queixas"] if t["rotulo"] == "Dor no peito"]
    assert queixa and queixa[0]["status"] == status
    if status == "negado":
        assert not any(a["texto"] == "Relata: dor no peito" for a in ins["resumo"]["destaques"])


def test_portuguese_keeps_working_with_the_words_no_and_sem(client, auth):
    """ "no" é "em o" em português: a troca de negação só vale para fala estrangeira."""
    h = auth("medico")
    cid = start(client, h, "cen6")["id"]
    ins = falas(client, h, cid, [{"falante": "paciente", "texto": "Sinto uma dor no peito.", "t": 1}])["insights"]
    assert tag(ins, "Dor no peito")["status"] == "presente"


def test_spanish_synonyms_for_symptoms_emotions_and_adherence(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    ins = falas(
        client,
        h,
        cid,
        [
            {"falante": "paciente", "texto": "Estoy mareada y tengo mucho cansancio.", "t": 1},
            {"falante": "paciente", "texto": "Tengo miedo, doctora.", "t": 5},
            {"falante": "paciente", "texto": "A veces se me olvida tomar la pastilla de la presión.", "t": 9},
            {"falante": "paciente", "texto": "I feel dizzy and I have a bad headache", "t": 13, "idioma": "en"},
        ],
    )["insights"]
    labels = {t["rotulo"] for t in ins["queixas"] if t["status"] == "presente"}
    assert {"Tontura", "Cansaço", "Dor de cabeça"} <= labels
    assert any(e["emocao"] == "ansiedade" and "Fala/2" in e["refs"] for e in ins["emocoes"])
    assert any("Fala/3" in r for r in consult_ai_refs(ins, "adesao"))


def consult_ai_refs(ins, _kind):
    """Refs de qualquer item de adesão (alerta de medicamento ou pergunta/lembrete) citando a fala."""
    found = []
    for k in ("sugestoes_medicamentos", "alertas", "lembretes", "medicamentos"):
        for item in ins.get(k, []):
            found.append(item.get("refs", []))
    return [r for refs in found for r in refs]


# ---------- (3) idioma por participante ----------
def test_speaker_language_chip_is_editable_per_utterance(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    res = falas(
        client,
        h,
        cid,
        [
            {"falante": "paciente", "texto": "Me duele el pecho.", "t": 1},
            {"falante": "acompanhante", "texto": "She has had chest pain since yesterday.", "t": 4, "idioma": "en"},
        ],
    )
    assert [s["idioma"] for s in res["novos"]] == ["es", "en"]
    client.post(f"/consultations/{cid}/translate", json={}, headers=h)
    # o chip da fala 2 muda para francês: a tradução antiga some e a fala pode ser traduzida de novo
    patched = client.patch(f"/consultations/{cid}/segments/2", json={"idioma": "fr"}, headers=h)
    assert patched.status_code == 200, patched.text
    seg = patched.json()["segmentos"][1]
    assert seg["idioma"] == "fr" and seg["traducao"] is None
    assert client.patch(f"/consultations/{cid}/segments/2", json={"idioma": "zzz"}, headers=h).status_code == 422


def test_doctor_utterances_are_translated_to_the_patient_language(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    falas(
        client,
        h,
        cid,
        [
            {
                "falante": "medico",
                "texto": "Bom dia, dona Carmen! Que bom ver a senhora. O Luis veio junto hoje.",
                "t": 0,
            }
        ],
    )
    out = client.post(f"/consultations/{cid}/translate", json={}, headers=h).json()
    assert out["traducoes"]["1"].startswith("¡Buenos días, doña [PACIENTE]!")


# ---------- (6) fidelidade ----------
def test_fidelity_detects_changed_numbers_units_and_drugs():
    assert fidelidade.divergencias("Tomo metformina de 500 mg", "Tomo metformina de 500 mg") == []
    assert fidelidade.divergencias("Tomo losartán de 50 mg", "Tomo losartana de 50 mg") == []  # es → pt: mesmo remédio
    assert fidelidade.divergencias("Hace dos días", "Há 2 dias") == []  # por extenso = dígito
    assert fidelidade.divergencias("Tomo 38,5 grados", "Tomo 38.5 graus") == []
    assert any("500" in d for d in fidelidade.divergencias("Tomo metformina de 500 mg", "Tomo metformina de 50 mg"))
    assert any("unidade" in d for d in fidelidade.divergencias("Tomo 500 mg", "Tomo 500 mcg"))
    assert any("medicamentos" in d for d in fidelidade.divergencias("Tomo ibuprofeno", "Tomo dipirona"))
    assert any("medicamentos" in d for d in fidelidade.divergencias("Tomo ibuprofeno y losartán", "Tomo ibuprofeno"))
    assert fidelidade.divergencias("Me duele la cabeza", "Dói a cabeça") == []


def test_wrong_dose_in_translation_raises_translation_alert(client, auth, monkeypatch):
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    falas(client, h, cid, [{"falante": "paciente", "texto": "Tomo metformina de 500 mg con la cena.", "t": 1}])

    def troca_dose(segments, targets, pseudo):
        return {n: "Tomo metformina de 50 mg no jantar." for n in targets}

    monkeypatch.setattr(consult_ai, "run_translation", troca_dose)
    out = client.post(f"/consultations/{cid}/translate", json={}, headers=h)
    assert out.status_code == 200, out.text
    seg = client.get(f"/consultations/{cid}", headers=h).json()["segmentos"][0]
    assert seg["traducao_divergente"] and "500" in seg["traducao_divergente"][0]
    alert = next(a for a in out.json()["insights"]["alertas"] if a["categoria"] == "traducao")
    assert "Tradução divergente" in alert["texto"] and alert["refs"] == ["Fala/1"] and alert["severidade"] == "atencao"


def test_faithful_translation_has_no_translation_alert(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    play_script(client, h, cid)
    out = client.post(f"/consultations/{cid}/translate", json={}, headers=h).json()
    assert not [a for a in out["insights"]["alertas"] if a["categoria"] == "traducao"]


# ---------- (10) roteiro cen_hispanica no modo mock ----------
def test_cen_hispanica_translations_in_mock_mode(client, auth):
    h = auth("medico")
    assert any(s["id"] == "cen_hispanica" for s in client.get("/consult/demo-scripts", headers=h).json())
    cid = start(client, h, "cen8")["id"]
    assert client.get(f"/consultations/{cid}", headers=h).json()["idioma_paciente"] == "es"
    ins = play_script(client, h, cid)
    assert tag(ins, "Dor no peito")["refs"]
    out = client.post(f"/consultations/{cid}/translate", json={}, headers=h)
    assert out.status_code == 200, out.text
    segs = client.get(f"/consultations/{cid}", headers=h).json()["segmentos"]
    # a tradução trafega pseudonimizada (como com o LLM real): os nomes viram [PACIENTE]/[FAMILIAR]
    esperado = {
        f["texto"]: f["traducao"].replace("Carmen", "[PACIENTE]").replace("Luis", "[FAMILIAR]")
        for f in HISPANICA["falas"]
        if f.get("traducao")
    }
    assert len(esperado) >= 12
    for s in segs:
        if s["texto"] in esperado:
            assert s["traducao"] == esperado[s["texto"]], s  # a gravada, não o aviso do mock
            assert s["traducao_divergente"] == []
    final = out.json()["insights"]
    assert tag(final, "Dor no peito")["status"] == "presente"
    assert any(e["emocao"] == "ansiedade" for e in final["emocoes"])  # "Tengo miedo" lido pela tradução
    assert final["queixas"] and all(t["refs"] for t in final["queixas"])


def test_unrecorded_utterance_gets_the_mock_notice_and_original_is_read(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    falas(client, h, cid, [{"falante": "paciente", "texto": "Tengo una tos seca desde hace tres días.", "t": 1}])
    out = client.post(f"/consultations/{cid}/translate", json={}, headers=h).json()
    assert "tradução com LLM real" in out["traducoes"]["1"]
    assert tag(out["insights"], "Tosse")["status"] == "presente"  # o aviso do mock não vira texto clínico


# ---------- consentimento ----------
def test_translation_respects_consent_flags(client, auth):
    h = auth("medico")
    consent = CONSENT | {"finalidades": CONSENT["finalidades"] | {"traducao": False}}
    res = client.post("/patients/cen8/consultations", json={"tipo": "consulta", "consentimento": consent}, headers=h)
    cid = res.json()["id"]
    falas(client, h, cid, [{"falante": "paciente", "texto": "Me duele el pecho desde ayer.", "t": 1}])
    assert client.post(f"/consultations/{cid}/translate", json={}, headers=h).status_code == 403
    # sem tradução por IA externa, as regras ainda leem o espanhol pelos sinônimos
    ins = client.get(f"/consultations/{cid}", headers=h).json()["insights"]
    assert tag(ins, "Dor no peito")["status"] == "presente"
    no_ai = CONSENT | {"finalidades": CONSENT["finalidades"] | {"ia_externa": False, "traducao": False}}
    cid2 = client.post(
        "/patients/cen8/consultations", json={"tipo": "consulta", "consentimento": no_ai}, headers=h
    ).json()["id"]
    assert client.post(f"/consultations/{cid2}/translate", json={}, headers=h).status_code == 403


# ---------- (9) nota e DocumentReference ----------
def test_exported_note_records_ai_interpretation(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    play_script(client, h, cid)
    client.post(f"/consultations/{cid}/translate", json={}, headers=h)
    client.post(f"/consultations/{cid}/finish", headers=h)
    approve(client, h, cid)
    res = client.get(f"/consultations/{cid}/export", headers=h)
    if res.status_code == 409:  # pendência crítica aberta: o envio pede justificativa
        sent = client.post(f"/consultations/{cid}/send", json={"justificativa": "Teste da interpretação"}, headers=h)
        assert sent.status_code == 200, sent.text
        res = client.get(f"/consultations/{cid}/export", headers=h)
    assert res.status_code == 200, res.text
    body = res.json()
    assert "Consulta com interpretação por IA (es→pt-BR, pt-BR→es)" in body["texto"]
    assert "interpretação por IA (es→pt-BR" in body["recurso"]["description"]


def test_note_without_translation_has_no_interpretation_line(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen6")["id"]
    falas(client, h, cid, [{"falante": "paciente", "texto": "Estou tonta quando levanto.", "t": 1}])
    client.post(f"/consultations/{cid}/finish", headers=h)
    approve(client, h, cid)
    res = client.get(f"/consultations/{cid}/export", headers=h)
    if res.status_code == 409:
        sent = client.post(
            f"/consultations/{cid}/send", json={"justificativa": "Risco discutido com a paciente"}, headers=h
        )
        assert sent.status_code == 200, sent.text
        res = client.get(f"/consultations/{cid}/export", headers=h)
    assert res.status_code == 200, res.text
    assert "interpretação por IA" not in res.json()["texto"]
