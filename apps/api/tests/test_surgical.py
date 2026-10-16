"""Centro cirúrgico: modelos por procedimento em dados (backlog 23) e verificações cirúrgicas (backlog 24).

Tudo passa pelo caminho real (API → Engine → surgical.py/surgical_checks.py) sobre os cenários sintéticos, em
LLM_MODE=mock.
"""

import csv
import re

import pytest
import yaml
from pydantic import ValidationError

from app.config import get_settings
from app.consult import service as consult_service
from app.consult import surgical
from app.consult.knowledge import SPECIALTIES, demo_scripts, prophylaxis
from app.consult.schemas import Segment
from app.consult.templates import BaseTemplate, ProcedureTemplate, problems, surgical_templates

SURGICAL_SPECIALTIES = {
    "ortopedia",
    "cirurgia_cardiovascular",
    "cirurgia_oncologica",
    "ginecologia_obstetricia",
    "cirurgia_geral",
    "urologia",
    "cirurgia_vascular",
}
PROCEDURES = {
    "artroplastia_joelho",
    "artroplastia_quadril",
    "osteossintese",
    "revascularizacao_miocardio",
    "mastectomia",
    "colectomia",
    "cesarea",
    "histerectomia",
    "apendicectomia",
    "colecistectomia",
    "herniorrafia",
    "rtu_prostata",
    "nefrolitotripsia",
    "safenectomia",
    "angioplastia",
}
GOOD_CHECKLIST = dict(
    mora_sozinho=False,
    tem_cuidador=True,
    cuidador_capacitado=True,
    escadas_em_casa=False,
    mobilidade_reduzida=False,
    acesso_medicamentos=True,
    precisa_dispositivos=False,
    consulta_retorno_agendada=True,
    teach_back_realizado=True,
)


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def start(client, h, pid="cen4b", especialidade="ortopedia"):
    res = client.post(
        f"/patients/{pid}/consultations", json={"tipo": "cirurgia", "especialidade": especialidade}, headers=h
    )
    assert res.status_code == 200, res.text
    return res.json()


def dictate(client, h, cid, falas):
    """falas: [(falante, texto)] ou [{"falante", "texto"}]."""
    items = [f if isinstance(f, dict) else {"falante": f[0], "texto": f[1]} for f in falas]
    payload = {"segmentos": [{"falante": f["falante"], "texto": f["texto"], "origem": "demo"} for f in items]}
    res = client.post(f"/consultations/{cid}/segments", json=payload, headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def report_of(ins: dict) -> dict:
    return ins["relatorio_cirurgico"]


def check(rep: dict, chave: str) -> dict:
    return next(v for v in rep["verificacoes"] if v["chave"] == chave)


# ---------- 23: modelos em dados ----------
def test_yaml_templates_schema():
    folder = get_settings().data_dir / "reference" / "surgical_templates"
    with open(folder / "_base.yaml", encoding="utf-8") as fh:
        base = BaseTemplate.model_validate(yaml.safe_load(fh))
    procs = {}
    for path in sorted(folder.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        with open(path, encoding="utf-8") as fh:
            procs[path.stem] = ProcedureTemplate.model_validate(yaml.safe_load(fh))
    assert problems(base, procs) == []
    assert PROCEDURES <= set(procs) and len(procs) >= 14
    assert {p.especialidade for p in procs.values()} == SURGICAL_SPECIALTIES
    assert SURGICAL_SPECIALTIES <= set(SPECIALTIES)
    for p in procs.values():
        assert re.match(r"^\d+\.\d+\.\d+$", p.versao)
        resolved = surgical_templates().resolve(p.id)
        # todo campo tem sinônimos, obrigatoriedade e motivo com fonte resolvida no catálogo
        for f in resolved.fields:
            assert f.words and f.motivo and f.fonte and f.fonte not in base.fontes, (p.id, f.campo)
        assert len({f.campo for f in resolved.fields}) == len(resolved.fields)
        # radioscopia (tempo, dose/DAP, incidências) obrigatória em ortopedia e vascular
        fluoro = {f.campo: f.obrigatorio for f in resolved.fields if f.campo.startswith("radioscopia_")}
        if p.especialidade in ("ortopedia", "cirurgia_vascular"):
            assert fluoro == {"radioscopia_tempo": True, "radioscopia_dose": True, "radioscopia_incidencias": True}
    # o código não tem mais lista fixa de campos
    assert not hasattr(surgical, "FIELDS")


def test_broken_template_is_rejected():
    base = surgical_templates().base
    minimal = {"versao": "1.0.0", "id": "x", "rotulo": "X", "especialidade": "ortopedia", "detectar": ["x"]}
    with pytest.raises(ValidationError):  # chave desconhecida no YAML é erro, não é ignorada
        ProcedureTemplate.model_validate(minimal | {"campo_inventado": True})
    bad = ProcedureTemplate.model_validate(
        {
            "versao": "1.0.0",
            "id": "teste",
            "rotulo": "Teste",
            "especialidade": "astrologia",
            "detectar": ["artroplastia total de joelho"],
            "profilaxia": ["amoxicilina"],
            "campos": [
                {
                    "campo": "novo",
                    "rotulo": "Novo campo",
                    "sinonimos": ["novo"],
                    "obrigatorio": True,
                    "motivo": "Motivo suficientemente longo.",
                    "fonte": "fonte_inexistente",
                }
            ],
            "obrigatoriedade": {
                "campo_que_nao_existe": {"obrigatorio": True, "motivo": "Motivo longo.", "fonte": "cfm_1638"}
            },
        }
    )
    errors = " | ".join(problems(base, {"teste": bad, **surgical_templates().procedures}))
    for expected in ("astrologia", "amoxicilina", "fonte_inexistente", "campo_que_nao_existe", "repetido"):
        assert expected in errors


def test_prophylaxis_table_has_dose_window_redose_and_source():
    path = get_settings().data_dir / "reference" / "surgical_prophylaxis.csv"
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert {"farmaco", "dose_g", "dose_mg_kg", "janela_min", "redose_h", "fonte"} <= set(rows[0])
    for r in rows:
        assert "ASHP/IDSA" in r["fonte"] and "2013" in r["fonte"]
        assert r["dose_g"] or r["dose_mg_kg"]
    cefazolin = prophylaxis()["cefazolina"]
    assert (cefazolin.dose_g, cefazolin.dose_g_heavy, cefazolin.heavy_kg) == (2, 3, 120)
    assert (cefazolin.window_min, cefazolin.redose_h) == (60, 4)
    assert prophylaxis()["vancomicina"].window_min == 120


def test_knee_dictation_picks_orthopedic_template_and_requires_laterality_and_tourniquet(client, auth):
    h = auth("medico")
    # especialidade escolhida "cirurgia geral": o procedimento ditado decide o modelo
    c = start(client, h, especialidade="cirurgia_geral")
    rep = report_of(
        dictate(client, h, c["id"], [("medico", "Artroplastia total de joelho direito, garrote 90 minutos.")])[
            "insights"
        ]
    )
    assert rep["modelo"]["id"] == "artroplastia_joelho" and rep["modelo"]["especialidade"] == "ortopedia"
    assert rep["modelo"]["origem"] == "ditado" and rep["modelo"]["refs"] == ["Fala/1"]
    fields = {f["campo"]: f for f in rep["campos"]}
    for key in ("lateralidade", "garrote"):
        assert fields[key]["obrigatorio"] is True and fields[key]["motivo"] and fields[key]["fonte"]
        assert fields[key]["refs"] == ["Fala/1"]
    assert "direito" in fields["lateralidade"]["texto"] and "90 minutos" in fields["garrote"]["texto"]
    assert check(rep, "garrote")["status"] == "ok"

    # sem o lado e sem o garrote: viram pendência com o motivo
    c2 = start(client, h)
    rep2 = report_of(
        dictate(client, h, c2["id"], [("medico", "Procedimento realizado: artroplastia total de joelho.")])["insights"]
    )
    missing = " ".join(p["texto"] for p in rep2["pendencias"])
    assert "Campo obrigatório não ditado: Lateralidade e sítio" in missing
    assert "Campo obrigatório não ditado: Garrote pneumático" in missing and "2 horas" in missing


def test_template_comes_from_schedule_before_dictation_and_specialty_selector(client, auth):
    h = auth("medico")
    rep = report_of(start(client, h, especialidade="cirurgia_geral")["insights"])
    assert rep["modelo"]["id"] == "artroplastia_joelho" and rep["modelo"]["origem"] == "agendamento"
    assert rep["agendado"]["lado"] == "direito" and rep["agendado"]["sitio"] == "Joelho direito"
    assert rep["agendado"]["refs"] == ["ServiceRequest/cen4b-sr-atj", "Appointment/cen4b-ag-atj"]
    assert rep["perioperatorio"], "checagens do prontuário aparecem no painel cirúrgico"

    config = client.get("/consult/config", headers=h).json()
    surgical_ = {e["value"] for e in config["especialidades"] if e["cirurgica"]}
    assert surgical_ == SURGICAL_SPECIALTIES
    # sem agendamento nem ditado, vale a especialidade escolhida (vascular: radioscopia obrigatória)
    vasc = report_of(start(client, h, pid="cen1", especialidade="cirurgia_vascular")["insights"])
    assert vasc["modelo"]["origem"] == "especialidade" and vasc["modelo"]["especialidade"] == "cirurgia_vascular"
    assert {f["campo"]: f["obrigatorio"] for f in vasc["campos"]}["radioscopia_dose"] is True


def test_fluoroscopy_fields(client, auth):
    h = auth("medico")
    c = start(client, h)
    rep = report_of(dictate(client, h, c["id"], [("medico", "Radioscopia 45 segundos.")])["insights"])
    fields = {f["campo"]: f for f in rep["campos"]}
    assert (
        fields["radioscopia_tempo"]["texto"] == "Radioscopia 45 segundos."
        and fields["radioscopia_tempo"]["obrigatorio"]
    )
    assert fields["radioscopia_dose"]["texto"] == ""  # ainda falta a dose (DAP)

    c2 = start(client, h)
    rep2 = report_of(dictate(client, h, c2["id"], [("medico", "Procedimento realizado sem radioscopia.")])["insights"])
    for key in ("radioscopia_tempo", "radioscopia_dose", "radioscopia_incidencias"):
        assert {f["campo"]: f for f in rep2["campos"]}[key]["texto"].startswith("Radioscopia não utilizada")


def test_cesarean_script_fills_apgar_and_placenta_delivery(client, auth):
    h = auth("medico")
    c = start(client, h, pid="cen4c", especialidade="ginecologia_obstetricia")
    assert c["roteiro_demo"] == "cen4c_cesarea"
    out = dictate(client, h, c["id"], demo_scripts()["cen4c_cesarea"]["falas"])
    rep = report_of(out["insights"])
    assert rep["modelo"]["id"] == "cesarea"
    fields = {f["campo"]: f for f in rep["campos"]}
    assert "Apgar 8" in fields["apgar"]["texto"] and "9 no quinto minuto" in fields["apgar"]["texto"]
    assert "Dequitação dirigida" in fields["dequitacao"]["texto"]
    assert fields["apgar"]["obrigatorio"] and fields["dequitacao"]["obrigatorio"]
    assert not any(p["texto"].startswith("Campo obrigatório não ditado") for p in rep["pendencias"])
    assert {v["chave"]: v["status"] for v in rep["verificacoes"] if v["grupo"] == "cirurgia_segura"} == {
        "sign_in": "ok",
        "time_out": "ok",
        "sign_out": "ok",
        "consentimento": "ok",
    }
    assert check(rep, "profilaxia_janela")["status"] == "ok" and check(rep, "contagem")["status"] == "ok"
    assert rep["cids"][0]["codigo"] == "O34.2"
    falantes = {s["falante"] for s in out["novos"]}
    assert {"anestesista", "circulante", "instrumentador", "medico"} <= falantes


def test_demo_scripts_endpoint_lists_surgical_scripts(client, auth):
    h = auth("medico")
    listed = {s["id"]: s for s in client.get("/consult/demo-scripts", headers=h).json()}
    for sid, pid in (("cen4b_artroplastia", "cen4b"), ("cen4c_cesarea", "cen4c"), ("cen4_cirurgia", "cen4")):
        assert listed[sid]["tipo"] == "cirurgia" and listed[sid]["patient_id"] == pid and listed[sid]["falas"] > 5
    assert client.get("/consult/demo-scripts/cen4b_artroplastia", headers=h).json()["especialidade"] == "ortopedia"
    assert start(client, h)["roteiro_demo"] == "cen4b_artroplastia"


def test_control_xray_is_proposed_and_becomes_pending_in_handoff_and_discharge(client, auth, store, monkeypatch):
    h = auth("medico")
    c = start(client, h)
    rep = report_of(dictate(client, h, c["id"], demo_scripts()["cen4b_artroplastia"]["falas"])["insights"])
    assert [o["status"] for o in rep["opme"]] == ["conferido"] * 4
    order = rep["pedidos"][0]
    sr = order["recurso"]
    assert sr["resourceType"] == "ServiceRequest" and sr["intent"] == "proposal" and sr["status"] == "draft"
    assert sr["category"][0]["coding"][0]["code"] == "363679005"  # SNOMED: imagem
    assert order["descricao"].startswith("Radiografia do joelho direito") and order["refs"] == ["Fala/14"]
    assert sr["subject"] == {"reference": "Patient/cen4b"}

    done = client.post(f"/consultations/{c['id']}/finish", headers=h).json()
    assert done["nota"]["relatorio_cirurgico"]["pedidos"][0]["descricao"] == order["descricao"]
    export = client.get(f"/consultations/{c['id']}/export", headers=h).json()
    assert export["pedidos"][0]["intent"] == "proposal" and "Verificações perioperatórias" in export["texto"]

    monkeypatch.setattr(consult_service.get_settings(), "ehr_writeback", True)
    created: list[tuple[str, str]] = []
    try:
        sent = client.post(f"/consultations/{c['id']}/send", headers=h).json()
        assert sent["enviado"] is True and len(sent["pedidos"]) == 1
        created = [tuple(r.split("/")) for r in [sent["fhir_ref"], *sent["pedidos"]]]
        stored = store.get(*created[1])
        assert stored["status"] == "active" and stored["supportingInfo"] == [{"reference": sent["fhir_ref"]}]

        handoff = client.post("/patients/cen4b/handoff", json={"audience": "enfermagem"}, headers=h).json()
        pend = [p for p in handoff["content"]["pendencias"] if p["texto"].startswith("Exame pendente")]
        assert pend and pend[0]["refs"] == [sent["pedidos"][0]] and "Radiografia do joelho direito" in pend[0]["texto"]

        alta = client.post(
            "/patients/cen4b/discharge-assessment", json={"social_checklist": GOOD_CHECKLIST}, headers=h
        ).json()
        sug = [s for s in alta["sugestoes"] if s["texto"].startswith("Exame pendente antes da alta")]
        assert sug and sug[0]["refs"] == [sent["pedidos"][0]]
    finally:
        for rtype, rid in created:
            store.resources[rtype].pop(rid, None)


# ---------- 24: verificações cirúrgicas ----------
def test_count_semantics(client, auth):
    h = auth("medico")
    c = start(client, h)
    ins = dictate(client, h, c["id"], [("instrumentador", "Contagem de compressas não confere.")])["insights"]
    rep = report_of(ins)
    assert check(rep, "contagem")["status"] == "critico"
    assert any(a["severidade"] == "critico" and "NÃO confere" in a["texto"] for a in ins["alertas"])
    ins = dictate(client, h, c["id"], [("instrumentador", "Revisada a cavidade, contagem de compressas correta.")])[
        "insights"
    ]
    counted = check(report_of(ins), "contagem")
    assert counted["status"] == "ok" and "resolvida" in counted["texto"] and counted["refs"] == ["Fala/1", "Fala/2"]


def test_prophylaxis_window_dose_and_redose(client, auth):
    h = auth("medico")
    c = start(client, h)  # cen4b: 130 kg no prontuário
    rep = report_of(
        dictate(client, h, c["id"], [("anestesista", "Cefazolina 2 g às 8h, incisão às 9h30.")])["insights"]
    )
    window = check(rep, "profilaxia_janela")
    assert window["status"] == "atencao" and "90 min" in window["texto"] and "janela de 60 min" in window["texto"]
    assert "ASHP/IDSA" in window["fonte"]
    dose = check(rep, "profilaxia_dose_cefazolina")
    assert dose["status"] == "atencao" and "130 kg" in dose["texto"] and "3 g" in dose["texto"]
    assert any(r.startswith("Observation/cen4b-sv0-peso") for r in dose["refs"])
    assert any(p["texto"].startswith("Profilaxia: dose de cefazolina") for p in rep["pendencias"])

    c2 = start(client, h)
    falas = [
        ("anestesista", "Cefazolina 3 g às 7h."),
        ("medico", "Incisão às 7h30."),
        ("medico", "Duração da cirurgia de 5 horas."),
    ]
    rep2 = report_of(dictate(client, h, c2["id"], falas)["insights"])
    assert check(rep2, "profilaxia_dose_cefazolina")["status"] == "ok"
    assert check(rep2, "profilaxia_janela")["status"] == "ok"
    redose = check(rep2, "profilaxia_redose")
    assert redose["status"] == "atencao" and "sem redose" in redose["texto"] and "4 h" in redose["texto"]
    rep3 = report_of(dictate(client, h, c2["id"], [("anestesista", "Redose de cefazolina às 11h.")])["insights"])
    assert check(rep3, "profilaxia_redose")["status"] == "ok"


def test_laterality_against_schedule_and_team_speakers(client, auth):
    h = auth("medico")
    c = start(client, h)
    out = dictate(
        client,
        h,
        c["id"],
        [
            ("anestesista", "Raquianestesia realizada, paciente estável."),
            ("medico", "Procedimento realizado: artroplastia total de joelho esquerdo."),
        ],
    )
    assert out["novos"][0]["falante"] == "anestesista" and out["novos"][0]["falante_auto"] is False
    rep = report_of(out["insights"])
    side = check(rep, "lateralidade")
    assert side["status"] == "critico" and "esquerdo" in side["texto"] and "joelho direito" in side["texto"]
    assert set(side["refs"]) == {"Fala/2", "ServiceRequest/cen4b-sr-atj", "Appointment/cen4b-ag-atj"}
    assert any(a["texto"].startswith("Lateralidade × agendamento") for a in out["insights"]["alertas"])
    stored = client.get(f"/consultations/{c['id']}", headers=h).json()["segmentos"]
    assert [s["falante"] for s in stored] == ["anestesista", "medico"]


def test_who_checklist_by_voice_and_button(client, auth):
    h = auth("medico")
    c = start(client, h)
    rep = report_of(dictate(client, h, c["id"], [("circulante", "Time-out realizado.")])["insights"])
    assert check(rep, "time_out")["status"] == "ok" and check(rep, "sign_in")["status"] == "pendente"
    assert check(rep, "consentimento")["refs"] == ["Consent/cen4b-tcle-atj"]

    res = client.post(f"/consultations/{c['id']}/checklist", json={"etapa": "sign_in"}, headers=h)
    assert res.status_code == 200, res.text
    sign_in = check(report_of(res.json()["insights"]), "sign_in")
    assert sign_in["status"] == "ok" and sign_in["marcado_por"] and sign_in["em"]
    # a marcação fica guardada e vale nas próximas falas
    rep = report_of(dictate(client, h, c["id"], [("medico", "Sign-out não realizado.")])["insights"])
    assert check(rep, "sign_in")["status"] == "ok" and check(rep, "sign_out")["status"] == "critico"
    assert client.post(f"/consultations/{c['id']}/checklist", json={"etapa": "outra"}, headers=h).status_code == 422

    consult = client.post("/patients/cen6/consultations", json={"tipo": "consulta"}, headers=h).json()
    assert (
        client.post(f"/consultations/{consult['id']}/checklist", json={"etapa": "sign_in"}, headers=h).status_code
        == 409
    )


def test_stored_segments_from_before_the_team_speakers_still_load():
    old = {"n": 1, "t": 0.0, "falante": "acompanhante", "texto": "Ele tomou o remédio.", "idioma": "pt-BR"}
    assert Segment.model_validate(old).falante == "acompanhante"
    assert Segment.model_validate(old | {"falante": "instrumentador"}).falante == "instrumentador"
