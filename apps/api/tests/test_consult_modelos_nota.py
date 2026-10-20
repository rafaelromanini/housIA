"""Backlog #16: modelos de nota por especialidade e tipo de atendimento (YAML versionados), seções dinâmicas,
aprovação travada por seção obrigatória e LOINC do documento exportado (visita médica x enfermagem)."""

from copy import deepcopy

import pytest

from app import clock
from app.consult import ai as consult_ai
from app.consult import note_templates
from app.consult import service as consult_service
from app.consult.ai import mock_note
from app.consult.engine import Engine
from app.consult.knowledge import demo_scripts
from app.consult.schemas import Segment
from app.db import session as db_session
from app.llm.client import LLMResult
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from tests.conftest import CONSENT
from tests.test_consult import SCRIPT, accept_critical, say, start

FIXED = set(note_templates.FIXED_SECTIONS)


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def finish(client, h, **body) -> dict:
    cid = start(client, h, **body)["id"]
    accept_critical(client, h, cid, say(client, h, cid, SCRIPT))  # sem isso o envio pede justificativa (#15)
    done = client.post(f"/consultations/{cid}/finish", headers=h)
    assert done.status_code == 200, done.text
    return done.json()


def keys(nota: dict) -> list[str]:
    return [s["key"] for s in nota["modelo"]["secoes"]]


def child_engine(record, specialty: str, falas: list[tuple[str, str]], birth: str, kind: str = "consulta") -> Engine:
    rec = record("cen6")
    rec.patient = deepcopy(rec.patient) | {"birthDate": birth, "gender": "male"}
    segs = [Segment(n=i, t=float(i), falante=who, texto=t) for i, (who, t) in enumerate(falas, 1)]  # type: ignore[arg-type]
    return Engine(rec, build_timeline(rec), build_context(rec), segs, specialty, kind, clock.now())


# ---------- modelos em dados ----------
def test_os_oito_modelos_sao_versionados_e_validos():
    tpls = note_templates.templates()
    assert set(tpls) == {
        "consulta_clinica",
        "pediatria",
        "enfermagem",
        "cardiologia",
        "urologia",
        "evolucao_internacao",
        "ps",
        "aps_esus",
    }
    for t in tpls.values():
        assert t.versao.count(".") == 2 and t.fonte and t.loinc.codigo
        assert all(s.rotulo and s.soap for s in t.secoes)
        assert {"queixa_principal", "hda", "avaliacao", "plano"} <= {s.key for s in t.secoes}
    assert {s.key for s in tpls["cardiologia"].secoes} >= {"classe_nyha"}
    assert {s.key for s in tpls["urologia"].secoes} >= {"ipss"}


def test_modelo_e_escolhido_por_especialidade_e_tipo():
    pick = lambda kind, spec: note_templates.select(kind, spec).id  # noqa: E731
    assert pick("consulta", "pediatria") == "pediatria"
    assert pick("consulta", "cardiologia") == "cardiologia"
    assert pick("consulta", "urologia") == "urologia"
    assert pick("consulta", "enfermagem") == pick("visita", "enfermagem") == "enfermagem"
    assert pick("visita", "clinica_medica") == "evolucao_internacao"
    assert pick("consulta", "clinica_medica") == pick("consulta", "ginecologia_obstetricia") == "consulta_clinica"


def test_get_note_templates_lista_e_marca_o_sugerido(client, auth):
    h = auth("medico")
    todos = client.get("/note-templates", headers=h).json()
    assert len(todos) == 8 and not any(m["sugerido"] for m in todos)
    sug = client.get("/note-templates?especialidade=pediatria&tipo=consulta", headers=h).json()
    assert [m["id"] for m in sug if m["sugerido"]] == ["pediatria"]
    assert client.get("/note-templates").status_code == 401


# ---------- critério 1: pediatria gera peso, vacinas e DNPM ----------
def test_consulta_de_pediatria_gera_peso_vacinas_e_dnpm(client, auth):
    h = auth("medico")
    nota = finish(client, h, pid="cen4b", especialidade="pediatria")["nota"]
    assert nota["modelo"]["id"] == "pediatria" and nota["modelo"]["versao"] == "1.0.0"
    assert {"peso", "vacinas", "dnpm"} <= set(keys(nota))
    got = {s["key"]: s for s in nota["secoes"]}
    assert {"peso", "vacinas", "dnpm"} <= set(got)
    # o peso vem do prontuário (FHIR, cen4b), com a ref; vacinas e DNPM não foram ditados
    assert "kg" in got["peso"]["texto"] and got["peso"]["refs"][0].startswith("Observation/")
    assert got["vacinas"]["texto"] == note_templates.NO_RECORD


def test_pediatria_preenche_vacinas_e_dnpm_com_as_falas_citadas(record):
    falas = [
        ("acompanhante", "Ele nasceu de parto normal, a termo, e as vacinas estão em dia"),
        ("medico", "E o desenvolvimento dele? Já andou?"),
        ("acompanhante", "Andou com um ano e já fala palavras"),
        ("acompanhante", "Ele mama no peito e come papinha"),
    ]
    eng = child_engine(record, "pediatria", falas, birth="2022-03-01")
    ins = eng.run()
    secoes = {s["key"]: s for s in mock_note(eng, ins, "pt-BR")["secoes"]}
    assert secoes["vacinas"]["texto"] == "em dia" and secoes["vacinas"]["refs"] == ["Fala/1"]
    assert "anda" in secoes["dnpm"]["texto"]
    assert "gestacao_parto" in secoes and "parto normal" in secoes["gestacao_parto"]["texto"]
    assert secoes["alimentacao"]["refs"] == ["Fala/4"]


# ---------- critério 2: aps_esus tem o CIAP-2 ----------
def test_aps_esus_tem_o_campo_ciap2_e_sugere_pelo_cid(client, auth):
    h = auth("medico")
    done = finish(client, h)
    cid = done["id"]
    assert done["nota"]["modelo"]["id"] == "consulta_clinica"
    trocada = client.put(f"/consultations/{cid}/note-template", json={"modelo": "aps_esus"}, headers=h)
    assert trocada.status_code == 200, trocada.text
    nota = trocada.json()["nota"]
    assert nota["modelo"]["id"] == "aps_esus"
    ciap = next(s for s in nota["modelo"]["secoes"] if s["key"] == "ciap2")
    assert ciap["obrigatoria"] and "CIAP-2" in ciap["rotulo"]
    texto = next(s for s in nota["secoes"] if s["key"] == "ciap2")
    assert "(CID-10 " in texto["texto"] and texto["refs"]  # código CIAP-2 derivado do CID, com a ref
    assert {s["soap"] for s in nota["modelo"]["secoes"]} == {"S", "O", "A", "P"}


def test_trocar_o_modelo_mantem_o_texto_editado_e_recusa_modelo_desconhecido(client, auth):
    h = auth("medico")
    done = finish(client, h)
    cid = done["id"]
    nota = done["nota"]
    nota["plano"]["texto"] = "Retorno em 15 dias com exames."
    assert client.put(f"/consultations/{cid}/note", json=nota, headers=h).status_code == 200
    ps = client.put(f"/consultations/{cid}/note-template", json={"modelo": "ps"}, headers=h).json()["nota"]
    assert ps["modelo"]["id"] == "ps" and ps["plano"]["texto"] == "Retorno em 15 dias com exames."
    assert any(s["key"] == "sinais_vitais" for s in ps["secoes"])
    assert (
        client.put(f"/consultations/{cid}/note-template", json={"modelo": "nao_existe"}, headers=h).status_code == 422
    )
    assert client.put(f"/consultations/{cid}/note-template", json={"modelo": "ps"}, headers=h).status_code == 200


# ---------- critério 3: seção obrigatória vazia impede a aprovação ----------
def test_secao_obrigatoria_vazia_impede_a_aprovacao(client, auth):
    h = auth("medico")
    done = finish(client, h, especialidade="pediatria")
    cid, nota = done["id"], done["nota"]
    barrada = client.put(f"/consultations/{cid}/note", json=nota, headers=h)
    assert barrada.status_code == 422
    detail = barrada.json()["detail"]
    assert detail["codigo"] == "secao_obrigatoria_vazia"
    assert {"Vacinação", "Peso"} <= set(detail["secoes"]) and any("DNPM" in r for r in detail["secoes"])
    # o envio também não passa sem a aprovação das seções
    assert client.post(f"/consultations/{cid}/send", headers=h).status_code == 422

    # o cliente não consegue afrouxar o modelo: o servidor guarda o que gerou
    burla = deepcopy(nota)
    for s in burla["modelo"]["secoes"]:
        s["obrigatoria"] = False
    assert client.put(f"/consultations/{cid}/note", json=burla, headers=h).status_code == 422

    for s in nota["secoes"]:
        if s["key"] == "peso":
            s["texto"] = "14,2 kg (balança do consultório)"
        if s["key"] == "vacinas":
            s["texto"] = "Caderneta em dia"
        if s["key"] == "dnpm":
            s["texto"] = "Desenvolvimento adequado para a idade"
    ok = client.put(f"/consultations/{cid}/note", json=nota | {"atestacao": True}, headers=h)
    assert ok.status_code == 200, ok.text
    saved = {s["key"]: s["texto"] for s in ok.json()["nota"]["secoes"]}
    assert saved["vacinas"] == "Caderneta em dia"
    sent = client.post(f"/consultations/{cid}/send", headers=h)
    assert sent.status_code == 200, sent.text
    exported = sent.json()
    assert "Vacinação: Caderneta em dia" in exported["texto"] and "Peso:" in exported["texto"]


def test_texto_so_com_sem_registro_conta_como_vazio():
    model = note_templates.snapshot(note_templates.get("aps_esus"))
    note = {
        "queixa_principal": {"texto": "Tosse", "refs": []},
        "avaliacao": {"texto": "Sem registro na consulta.", "refs": []},
    }
    note = note | {"secoes": note_templates.complete_sections([], model)}
    faltam = note_templates.missing_required(note, model)
    assert any("Avaliação" in f for f in faltam) and any("CIAP-2" in f for f in faltam)
    assert not any("Motivo" in f for f in faltam)


# ---------- critério 4: nota antiga do cen6 continua renderizando ----------
def test_nota_antiga_do_cen6_continua_renderizando(client, auth):
    h = auth("medico")
    done = finish(client, h)
    cid = done["id"]
    legado = {k: v for k, v in done["nota"].items() if k not in ("modelo", "secoes")}  # como era gravada antes
    with db_session.SessionLocal() as db:
        c = db.get(consult_service.Consultation, cid)
        c.note_json = legado
        db.commit()
    lida = client.get(f"/consultations/{cid}", headers=h).json()["nota"]
    assert lida["modelo"]["id"] == "consulta_clinica"
    assert [s["key"] for s in lida["modelo"]["secoes"]] == list(note_templates.FIXED_SECTIONS)
    assert lida["queixa_principal"]["texto"] == legado["queixa_principal"]["texto"]
    assert lida["secoes"] == []
    # editar e aprovar a nota antiga funciona (o corpo não traz modelo nem secoes)
    legado["plano"]["texto"] = "Suspender ibuprofeno; retorno em 2 semanas."
    assert client.put(f"/consultations/{cid}/note", json=legado | {"atestacao": True}, headers=h).status_code == 200
    exp = client.get(f"/consultations/{cid}/export", headers=h).json()
    assert "Queixa principal:" in exp["texto"] and "Plano: Suspender ibuprofeno" in exp["texto"]
    assert exp["recurso"]["type"]["coding"][0]["code"] == "11488-4"


# ---------- critério 5: LOINC da visita de enfermagem ----------
def loinc_of_export(client, h, **body) -> str:
    done = finish(client, h, **body)
    cid, nota = done["id"], done["nota"]
    for s in nota["secoes"]:
        if not s["texto"] or s["texto"] == note_templates.NO_RECORD:
            s["texto"] = "Preenchido pelo profissional"
    for k in ("queixa_principal", "avaliacao", "plano"):
        nota[k]["texto"] = nota[k]["texto"] or "Registrado"
    assert client.put(f"/consultations/{cid}/note", json=nota | {"atestacao": True}, headers=h).status_code == 200
    sent = client.post(f"/consultations/{cid}/send", headers=h)
    assert sent.status_code == 200, sent.text
    doc = sent.json()["recurso"]
    return doc["type"]["coding"][0]["code"]


def test_visita_de_enfermagem_exporta_loinc_34746_8(client, auth):
    h = auth("enfermagem")
    assert loinc_of_export(client, h, tipo="visita", especialidade="enfermagem") == "34746-8"


def test_visita_medica_exporta_loinc_11506_3_e_consulta_continua_11488_4(client, auth):
    h = auth("medico")
    assert loinc_of_export(client, h, tipo="visita") == "11506-3"
    assert loinc_of_export(client, h) == "11488-4"


# ---------- LLM: o que faltar vem das regras; ref inventada cai ----------
def test_llm_sem_secoes_recebe_o_rascunho_das_regras_e_ref_invalida_cai(client, auth, monkeypatch):
    def fake(name, variables, schema, mock=None, effort=None, **_):
        out = mock(variables)
        if name == "consult_note":
            assert "peso" in variables["template"] and "[obrigatória]" in variables["template"]
            out["secoes"] = [{"key": "peso", "texto": "Peso inventado 99 kg", "refs": ["Observation/nao-existe"]}]
        return LLMResult(output=schema.model_validate(out), model="fake", prompt_version="x", mode="real")

    monkeypatch.setattr(consult_ai, "generate_structured", fake)
    nota = finish(client, auth("medico"), especialidade="pediatria")["nota"]
    got = {s["key"]: s for s in nota["secoes"]}
    assert set(got) == set(keys(nota)) - FIXED
    assert got["peso"]["texto"] == "Peso inventado 99 kg" and got["peso"]["refs"] == []  # texto da IA, sem ref falsa
    assert got["dnpm"]["texto"] == note_templates.NO_RECORD  # o que a IA não trouxe vem das regras


def test_prompt_da_nota_recebe_o_modelo():
    from app.llm.client import load_prompt

    p = load_prompt("consult_note")
    assert p.version == "1.5.0" and "$template" in p.user and "secoes" in p.system


# ---------- as outras vias continuam enxergando as seções dinâmicas ----------
def test_cobertura_conta_falas_citadas_por_secao_dinamica(client, auth):
    h = auth("medico")
    done = finish(client, h, pid="cen4b", especialidade="pediatria")
    peso = next(s for s in done["nota"]["secoes"] if s["key"] == "peso")
    assert peso["refs"]  # citada pelo prontuário
    from app.consult import relevancia

    assert relevancia.cited_falas({"secoes": [{"key": "x", "texto": "t", "refs": ["Fala/7"]}]}) == {7}


def test_cenarios_de_demo_continuam_gerando_nota_com_modelo(client, auth):
    h = auth("medico")
    for sid in ("cen6_consulta", "cen1_visita"):
        script = demo_scripts()[sid]
        res = client.post(
            f"/patients/{script['patient_id']}/consultations",
            json={"tipo": script["tipo"], "especialidade": script["especialidade"], "consentimento": CONSENT},
            headers=h,
        )
        assert res.status_code == 200, res.text
        cid = res.json()["id"]
        say(client, h, cid, script["falas"])
        nota = client.post(f"/consultations/{cid}/finish", headers=h).json()["nota"]
        assert nota["modelo"]["id"] == ("evolucao_internacao" if script["tipo"] == "visita" else "consulta_clinica")


def test_secao_inventada_pelo_cliente_nao_e_guardada(client, auth):
    h = auth("medico")
    done = finish(client, h, especialidade="pediatria")
    nota = done["nota"]
    for s in nota["secoes"]:
        s["texto"] = "Preenchido pelo profissional"
    for k in ("queixa_principal", "avaliacao", "plano"):
        nota[k]["texto"] = nota[k]["texto"] or "Registrado"
    nota["secoes"].append({"key": "inventada", "texto": "x", "refs": ["Observation/nao-existe"]})
    ok = client.put(f"/consultations/{done['id']}/note", json=nota, headers=h)
    assert ok.status_code == 200, ok.text
    assert "inventada" not in [s["key"] for s in ok.json()["nota"]["secoes"]]
