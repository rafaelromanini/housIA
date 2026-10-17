"""Copiloto da consulta (#20): pergunta livre com fonte em cada frase; regras primeiro, IA só no que sobra."""

import pytest
from sqlalchemy import select

from app.consult import ask as copilot
from app.consult import service as consult_service
from app.consult.knowledge import demo_scripts
from app.db import session as db_session
from app.db.models import AuditLog
from app.llm.client import LLMResult
from tests.conftest import login
from tests.test_consult import say, start

SCRIPT = demo_scripts()["cen6_consulta"]["falas"]


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


@pytest.fixture
def cons(client):
    h = login(client)
    return client, h, start(client, h)["id"]


def ask(client, h, cid, pergunta, via="texto"):
    res = client.post(f"/consultations/{cid}/ask", json={"pergunta": pergunta, "via": via}, headers=h)
    assert res.status_code == 200, res.text
    return res.json()["mensagem"]


def text_of(msg) -> str:
    return " ".join(f["texto"] for f in msg["resposta"]["frases"])


def refs_of(msg) -> set[str]:
    return {r for f in msg["resposta"]["frases"] for r in f["refs"]}


def test_ultimo_potassio_responde_valor_e_observation(cons):
    client, h, cid = cons
    msg = ask(client, h, cid, "qual o último potássio?")
    r = msg["resposta"]
    assert r["intencao"] == "valor_exame" and r["modo"] == "regras"
    assert "5,3 mmol/L" in r["frases"][0]["texto"]
    assert "Observation/cen6-lab2-potassio" in r["frases"][0]["refs"]
    assert "4,6" in r["frases"][0]["texto"]  # anterior, calculado pela regra
    assert all(f["refs"] and f["fonte"] for f in r["frases"])


def test_posso_usar_aine_no_cen6_diz_nao_com_tfg_losartana_hctz_e_fonte(cons):
    client, h, cid = cons
    msg = ask(client, h, cid, "posso usar AINE?")
    r = msg["resposta"]
    assert r["intencao"] == "posso_usar"
    assert r["frases"][0]["texto"].startswith("Não")
    texto = text_of(msg)
    assert "TFG" in texto and "Losartana" in texto and "Hidroclorotiazida" in texto
    refs = refs_of(msg)
    assert "Observation/cen6-lab2-creatinina" in refs
    assert "MedicationRequest/cen6-rx-losartana-c1" in refs
    assert "MedicationRequest/cen6-rx-hidroclorotiazida-c3" in refs
    assert "@pergunta" not in refs
    fontes = " ".join(f["fonte"] for f in r["frases"])
    assert "KDIGO" in fontes
    assert all(f["refs"] or f["fonte"] for f in r["frases"])


def test_posso_usar_aine_depois_da_consulta_inteira(cons):
    client, h, cid = cons
    say(client, h, cid, SCRIPT)  # a paciente relata ibuprofeno: a tríplice ameaça renal entra com as falas
    msg = ask(client, h, cid, "Posso prescrever ibuprofeno pra ela?")
    assert msg["resposta"]["frases"][0]["texto"].startswith("Não")
    assert "Tríplice" in text_of(msg)
    assert any(r.startswith("Fala/") for r in refs_of(msg)) or "MedicationRequest/cen6-rx-losartana-c1" in refs_of(msg)


def test_posso_usar_alergia_penicilina(cons):
    client, h, cid = cons
    msg = ask(client, h, cid, "posso usar amoxicilina?")
    assert msg["resposta"]["frases"][0]["texto"].startswith("Não")
    assert "AllergyIntolerance/cen6-alg-0" in refs_of(msg)


def test_posso_usar_sem_alerta_cita_o_que_foi_conferido(cons):
    client, h, cid = cons
    msg = ask(client, h, cid, "posso usar paracetamol?")
    head = msg["resposta"]["frases"][0]
    assert head["texto"].startswith("Sem alerta") and head["refs"] and head["fonte"]


def test_interacao_entre_dois_medicamentos(cons):
    client, h, cid = cons
    msg = ask(client, h, cid, "tem interação entre losartana e ibuprofeno?")
    assert msg["resposta"]["intencao"] == "interacao"
    assert "BRA + AINE" in text_of(msg)
    assert "MedicationRequest/cen6-rx-losartana-c1" in refs_of(msg)
    assert any("KDIGO" in f["fonte"] for f in msg["resposta"]["frases"])


def test_dose_por_tfg_aplica_regra_com_a_tfg_do_paciente(cons):
    client, h, cid = cons
    msg = ask(client, h, cid, "qual a dose de metformina com essa TFG?")
    assert msg["resposta"]["intencao"] == "dose"
    texto = text_of(msg)
    assert "TFG atual (~40)" in texto and "a regra se aplica" in texto
    assert "Observation/cen6-lab2-creatinina" in refs_of(msg)


def test_dose_por_peso_sem_peso_registrado_avisa(cons):
    client, h, cid = cons
    msg = ask(client, h, cid, "dose de dipirona por peso")
    r = msg["resposta"]
    assert "Sem peso registrado" in r["aviso"] and not r["frases"]


def test_alergias_do_prontuario(cons):
    client, h, cid = cons
    msg = ask(client, h, cid, "ela tem alergia a quê?")
    assert "AllergyIntolerance/cen6-alg-0" in refs_of(msg)


def test_o_que_mudou_desde_a_ultima_consulta(cons):
    client, h, cid = cons
    msg = ask(client, h, cid, "o que mudou desde a última consulta?")
    texto = text_of(msg)
    assert msg["resposta"]["intencao"] == "mudou"
    assert "Potássio subiu de 4,6 para 5,3" in texto
    assert {"Observation/cen6-lab1-potassio", "Observation/cen6-lab2-potassio"} <= refs_of(msg)


def test_tfg_e_alertas_por_regra(cons):
    client, h, cid = cons
    say(client, h, cid, SCRIPT)
    tfg = ask(client, h, cid, "qual a TFG?")
    assert "~40" in text_of(tfg) and "CKD-EPI" in tfg["resposta"]["frases"][0]["fonte"]
    alerts = ask(client, h, cid, "quais os alertas críticos?")
    assert alerts["resposta"]["intencao"] == "alertas" and alerts["resposta"]["frases"]


def test_mock_sem_regra_nao_inventa_e_avisa(cons):
    client, h, cid = cons
    msg = ask(client, h, cid, "o que você acha do prognóstico dela?")
    r = msg["resposta"]
    assert r["intencao"] == "sem_resposta" and not r["frases"] and "regra" in r["aviso"]


def _fake_llm(frases, mode="real"):
    def fake(prompt, variables, schema, mock, effort=None):
        return LLMResult(output=schema.model_validate({"frases": frases}), model="teste", prompt_version="x", mode=mode)

    return fake


def test_ia_frase_sem_ref_valida_e_removida(cons, monkeypatch):
    client, h, cid = cons
    monkeypatch.setattr(
        copilot,
        "generate_structured",
        _fake_llm(
            [
                {"texto": "Tem hipertensão e diabetes.", "refs": ["Condition/cen6-dx-0"]},
                {"texto": "Tem insuficiência cardíaca grave.", "refs": ["Condition/inventada-99"]},
                {"texto": "Já foi operada do coração.", "refs": []},
            ]
        ),
    )
    msg = ask(client, h, cid, "resuma o caso em poucas palavras")
    r = msg["resposta"]
    assert r["intencao"] == "ia" and r["modo"] == "ia"
    assert [f["texto"] for f in r["frases"]] == ["Tem hipertensão e diabetes."]
    assert r["frases"][0]["origem"] == "ia"
    dropped = {d["texto"] for d in r["descartados"]}
    assert dropped == {"Tem insuficiência cardíaca grave.", "Já foi operada do coração."}


def test_ia_nao_calcula_numero_fora_dos_dados_derruba_a_frase(cons, monkeypatch):
    client, h, cid = cons
    monkeypatch.setattr(
        copilot,
        "generate_structured",
        _fake_llm(
            [
                {"texto": "A TFG é 40 mL/min.", "refs": ["Observation/cen6-lab2-creatinina"]},
                {"texto": "A dose segura é 777 mg.", "refs": ["Observation/cen6-lab2-creatinina"]},
            ]
        ),
    )
    msg = ask(client, h, cid, "conta a dose pra mim")
    assert [f["texto"] for f in msg["resposta"]["frases"]] == ["A TFG é 40 mL/min."]
    assert msg["resposta"]["descartados"][0]["motivo"] == "número fora dos dados"


def test_ia_pergunta_vai_pseudonimizada_e_resposta_reidentificada(cons, monkeypatch):
    client, h, cid = cons
    seen = {}

    def fake(prompt, variables, schema, mock, effort=None):
        seen.update(variables)
        return LLMResult(
            output=schema.model_validate(
                {"frases": [{"texto": "[PACIENTE] relata tontura.", "refs": ["Condition/cen6-dx-0"]}]}
            ),
            model="t",
            prompt_version="x",
            mode="real",
        )

    monkeypatch.setattr(copilot, "generate_structured", fake)
    msg = ask(client, h, cid, "o que a Joana tem de importante?")
    assert "Joana" not in seen["pergunta"] and "[PACIENTE]" in seen["pergunta"]
    assert "Joana" in msg["resposta"]["frases"][0]["texto"] and "[PACIENTE]" not in text_of(msg)


def test_alt_q_voz_nao_cria_segment(cons):
    client, h, cid = cons
    say(client, h, cid, SCRIPT[:3])
    before = client.get(f"/consultations/{cid}", headers=h).json()
    msg = ask(client, h, cid, "qual o último potássio?", via="voz")
    after = client.get(f"/consultations/{cid}", headers=h).json()
    assert msg["via"] == "voz"
    assert len(before["segmentos"]) == len(after["segmentos"]) == 3
    assert after["insights"]["falas_analisadas"] == 3
    assert "potássio" not in " ".join(s["texto"] for s in after["segmentos"])


def test_inserir_na_nota_leva_as_refs_com_origem_copiloto(cons):
    client, h, cid = cons
    say(client, h, cid, SCRIPT)
    msg = ask(client, h, cid, "posso usar AINE?")
    res = client.post(
        f"/consultations/{cid}/copiloto/inserir", json={"mensagem_id": msg["id"], "destino": "plano"}, headers=h
    )
    assert res.status_code == 200, res.text
    ins = res.json()["insercao"]
    assert ins["origem"] == "copiloto" and set(ins["refs"]) == refs_of(msg)
    # a inserção feita durante a consulta entra na nota quando ela é gerada
    note = client.post(f"/consultations/{cid}/finish", headers=h).json()["nota"]
    assert "Não." in note["plano"]["texto"]
    assert refs_of(msg) <= set(note["plano"]["refs"])
    assert note["copiloto"][0]["origem"] == "copiloto" and note["copiloto"][0]["destino"] == "plano"
    # e depois de finalizada a inserção é imediata, sem duplicar a mesma
    again = client.post(
        f"/consultations/{cid}/copiloto/inserir", json={"mensagem_id": msg["id"], "destino": "plano"}, headers=h
    ).json()
    assert again["criada"] is False
    msg2 = ask(client, h, cid, "qual o último potássio?")
    done = client.post(
        f"/consultations/{cid}/copiloto/inserir",
        json={"mensagem_id": msg2["id"], "destino": "exames_complementares"},
        headers=h,
    ).json()
    assert "Observation/cen6-lab2-potassio" in done["nota"]["exames_complementares"]["refs"]
    # a revisão da nota não perde a proveniência
    kept = client.put(f"/consultations/{cid}/note", json=done["nota"] | {"copiloto": []}, headers=h).json()["nota"]
    assert len(kept["copiloto"]) == 2


def test_comandos_adicionar_ao_plano_e_criar_pendencia(cons):
    client, h, cid = cons
    ask(client, h, cid, "adicionar ao plano: repetir potássio e creatinina em 7 dias")
    p = ask(client, h, cid, "criar pendência registrar alergia a camarão")
    assert p["resposta"]["acao"]["tipo"] == "pendencia"
    state = client.get(f"/consultations/{cid}/copiloto", headers=h).json()
    assert [i["destino"] for i in state["insercoes"]] == ["plano", "pendencia"]
    segs = client.get(f"/consultations/{cid}", headers=h).json()["segmentos"]
    assert segs == []
    say(client, h, cid, SCRIPT[:2])
    note = client.post(f"/consultations/{cid}/finish", headers=h).json()["nota"]
    assert "repetir potássio e creatinina em 7 dias" in note["plano"]["texto"]
    assert any("registrar alergia a camarão" in x["texto"] for x in note["pendencias"])


def test_comando_sem_texto_usa_a_ultima_resposta(cons):
    client, h, cid = cons
    first = ask(client, h, cid, "qual o último potássio?")
    cmd = ask(client, h, cid, "adicionar ao plano")
    ins = client.get(f"/consultations/{cid}/copiloto", headers=h).json()["insercoes"][0]
    assert ins["mensagem_id"] == first["id"] and "5,3 mmol/L" in ins["texto"]
    assert "Observation/cen6-lab2-potassio" in ins["refs"] and cmd["resposta"]["acao"]["tipo"] == "plano"


def test_alerta_critico_sugere_discutir(cons):
    client, h, cid = cons
    say(client, h, cid, SCRIPT)
    state = client.get(f"/consultations/{cid}/copiloto", headers=h).json()
    assert state["sugestoes"] and state["sugestoes"][0]["alerta"]
    msg = ask(client, h, cid, state["sugestoes"][0]["pergunta"])
    assert msg["resposta"]["intencao"] == "alertas" and msg["resposta"]["frases"]


def test_audit_do_copiloto_sem_texto(cons):
    client, h, cid = cons
    ask(client, h, cid, "qual o último potássio do paciente Joana?")
    with db_session.SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "consulta.copiloto")).all()
    assert len(rows) == 1
    blob = str(rows[0].payload_json if hasattr(rows[0], "payload_json") else rows[0].__dict__)
    assert "potássio" not in blob and "Joana" not in blob
    assert "valor_exame" in blob


def test_historico_persiste_e_exige_papel_clinico(cons):
    client, h, cid = cons
    ask(client, h, cid, "qual o último potássio?")
    ask(client, h, cid, "alergias")
    state = client.get(f"/consultations/{cid}/copiloto", headers=h).json()
    assert [m["id"] for m in state["mensagens"]] == [1, 2]
    gestor = login(client, "gestor")
    assert client.post(f"/consultations/{cid}/ask", json={"pergunta": "x"}, headers=gestor).status_code == 403
    assert client.post(f"/consultations/{cid}/ask", json={"pergunta": " "}, headers=h).status_code == 422


def test_validate_descarta_frase_sem_fonte():
    ok = copilot.Frase(texto="a", refs=["Observation/1"])
    sem_fonte = copilot.Frase(texto="b")
    so_fonte = copilot.Frase(texto="c", fonte="KDIGO")
    ia_sem_ref = copilot.Frase(texto="d", fonte="KDIGO", origem="ia")
    kept, dropped = copilot.validate([ok, sem_fonte, so_fonte, ia_sem_ref], {"Observation/1"})
    assert [f.texto for f in kept] == ["a", "c"] and {d["texto"] for d in dropped} == {"b", "d"}
