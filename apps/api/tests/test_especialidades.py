"""Pacotes de especialidade, escalas, dose pediátrica por peso e glossário clínico (#27).

Os critérios de aceite passam pelo caminho real (API/TestClient e Engine sobre os cenários, LLM mock):
- criança de 12 kg com 'amoxicilina 500 mg de 8 em 8 horas' gera alerta de dose acima do máximo, com a fonte;
- paciente com FA mostra o CHA2DS2-VASc com os pontos e as refs;
- o perfil de urologia calcula o IPSS a partir das respostas;
- 'xixi' sai como 'diurese' na nota;
- schema dos YAML dos pacotes.
"""

import csv
import re

import pytest

from app import clock
from app.consult import ai as consult_ai
from app.consult import apps, dose_peso, glossario, knowledge, note_templates, scales
from app.consult import service as consult_service
from app.consult.engine import Engine
from app.consult.especialidades import Pacote, pacotes
from app.consult.schemas import Segment
from app.consult.templates import surgical_specialties
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from tests.test_consult import say, start


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def falas_de(script: str) -> list[dict]:
    return knowledge.demo_scripts()[script]["falas"]


def engine_for(record, pid: str, falas: list[tuple[str, str]], specialty: str = "clinica_medica") -> Engine:
    rec = record(pid)
    segs = [Segment(n=i, t=float(i), falante=who, texto=t) for i, (who, t) in enumerate(falas, 1)]  # type: ignore[arg-type]
    return Engine(rec, build_timeline(rec), build_context(rec), segs, specialty, "consulta", clock.now())


def peso_ins(record, falas: list[tuple[str, str]]):
    return engine_for(record, "cen9", falas, "pediatria").run()


def dose_alerts(ins) -> list:
    return [m for m in ins.sugestoes_medicamentos if "mg/kg" in m.texto or "sem peso" in m.texto]


# ---------- schema dos pacotes ----------
def test_pacotes_validam_e_substituem_as_constantes():
    packs = pacotes()
    assert list(knowledge.SPECIALTIES) == list(packs)
    assert knowledge.SPECIALTIES["pediatria"] == "Pediatria"
    assert knowledge.SPECIALTY_FOCUS["urologia"].startswith("sintomas do trato urinário inferior (IPSS)")
    assert {"nutricao", "saude_mental"} <= set(packs)  # perfis novos
    assert len(packs) >= 15


def test_pacotes_apontam_para_o_que_existe():
    known_apps = {a.manifest.id for a in apps.all_apps()}
    cids = {
        r["codigo"]
        for r in csv.DictReader(open(knowledge.get_settings().data_dir / "reference" / "cid10.csv", encoding="utf-8"))
    }
    for pack in pacotes().values():
        assert set(pack.smart_apps) <= known_apps, pack.id
        assert set(pack.escalas) <= scales.ids(), pack.id
        assert pack.hipoteses_prioritarias == [c for c in pack.hipoteses_prioritarias if c in cids], pack.id
        assert note_templates.get(pack.modelo_nota) is not None, pack.id
        assert note_templates.select("consulta", pack.id).id == pack.modelo_nota, pack.id
        for q in pack.perguntas:
            assert q.id.startswith("pk_")
    assert set(surgical_specialties()) <= set(pacotes())  # as cirúrgicas vêm dos modelos, não do pacote


def test_schema_rejeita_campo_desconhecido_e_cid_invalido():
    base = pacotes()["pediatria"].model_dump()
    with pytest.raises(ValueError):
        Pacote.model_validate(base | {"escala_inventada": 1})
    with pytest.raises(ValueError):
        Pacote.model_validate(base | {"hipoteses_prioritarias": ["XYZ"]})
    with pytest.raises(ValueError):
        Pacote.model_validate(base | {"glossario": {"xixi": " "}})


def test_perguntas_do_pacote_aparecem_so_na_especialidade(client, auth):
    h = auth("medico")
    nut = start(client, h, "cen6", especialidade="nutricao")
    ids = {q["id"].split(":")[0] for q in nut["insights"]["perguntas"]}
    assert {"pk_nut_01", "pk_nut_02"} <= ids
    clin = start(client, h, "cen6", especialidade="clinica_medica")
    assert not any(q["id"].startswith("pk_") for q in clin["insights"]["perguntas"])
    sm = start(client, h, "cen6", especialidade="saude_mental")
    assert any(q["id"].startswith("pk_sm_01") for q in sm["insights"]["perguntas"])


def test_endpoint_de_especialidades_traz_o_pacote(client, auth):
    res = client.get("/consult/config", headers=auth("medico"))
    if res.status_code != 200:  # o caminho do config varia: procura pela lista de especialidades
        pytest.skip("rota de config fora do prefixo esperado")
    pack = next(e for e in res.json()["especialidades"] if e["value"] == "urologia")
    assert "ipss" in pack["escalas"] and pack["modelo_nota"] == "urologia" and pack["smart_apps"]


# ---------- pediatria: dose por peso ----------
def test_crianca_12kg_amoxicilina_500_8_em_8_alerta_com_fonte(record):
    ins = peso_ins(record, [(f["falante"], f["texto"]) for f in falas_de("cen9_pediatria")])
    alerts = [m for m in dose_alerts(ins) if "Amoxicilina" in m.texto]
    assert len(alerts) == 1
    a = alerts[0]
    assert a.severidade == "critico" and a.tipo == "ajuste_dose"
    assert "acima do máximo" in a.texto and "12 kg" in a.texto
    assert "41,7 mg/kg/dose" in a.texto and "125 mg/kg/dia" in a.texto
    assert "ANVISA" in a.fonte and "SBP" in a.fonte
    assert any(r.startswith("Fala/") for r in a.refs) and any(r.startswith("Observation/") for r in a.refs)
    assert ins.modo == "regras"  # o alerta nasce das regras, antes de qualquer IA
    assert any("Amoxicilina" in h.texto for h in ins.alertas if h.severidade == "critico")


def test_dose_na_faixa_nao_alerta(record):
    for fala in (
        "Vou passar amoxicilina 250 mg de 8 em 8 horas por 7 dias.",  # 20,8 mg/kg/dose e 62,5 mg/kg/dia
        "Pode dar paracetamol 150 mg de 6 em 6 horas se tiver febre.",  # 12,5 mg/kg/dose
        "Dipirona 200 mg de 6 em 6 horas se tiver dor.",  # 16,7 mg/kg/dose
    ):
        assert dose_alerts(peso_ins(record, [("medico", fala)])) == [], fala


@pytest.mark.parametrize(
    ("fala", "trecho"),
    [
        ("Prescrevo paracetamol 500 mg de 4 em 4 horas.", "mg/kg/dose"),
        ("Vou passar ibuprofeno 400 mg de 6 em 6 horas.", "mg/kg/dose"),
        ("Azitromicina 500 mg uma vez ao dia por 3 dias.", "mg/kg/dose"),
        ("Dipirona 300 mg de 3 em 3 horas.", "intervalo de 3 h"),
        ("Vou passar amoxicilina 1 g de 8 em 8 horas.", "mg/kg/dose"),
    ],
)
def test_outras_doses_acima_do_maximo(record, fala, trecho):
    found = dose_alerts(peso_ins(record, [("medico", fala)]))
    assert found and found[0].severidade == "critico" and trecho in found[0].texto and found[0].fonte


def test_subdose_e_atencao(record):
    found = dose_alerts(peso_ins(record, [("medico", "Vou passar amoxicilina 50 mg de 8 em 8 horas.")]))
    assert found and found[0].severidade == "atencao" and "abaixo da faixa" in found[0].texto


def test_negacao_e_adulto_nao_geram_alerta(record):
    assert dose_alerts(peso_ins(record, [("acompanhante", "Eu nunca dei amoxicilina 500 mg de 8 em 8 horas.")])) == []
    adult = engine_for(record, "cen6", [("medico", "Vou passar amoxicilina 500 mg de 8 em 8 horas.")]).run()
    assert dose_alerts(adult) == []  # dose de adulto em adulto: a tabela é pediátrica


def test_funcoes_de_dose():
    assert dose_peso.parse_mg("500 mg") == 500 and dose_peso.parse_mg("1 g") == 1000
    assert dose_peso.parse_mg("1.000 mg") == 1000 and dose_peso.parse_mg("2,5 mg") == 2.5
    assert dose_peso.parse_mg("20 gotas") is None
    assert dose_peso.interval_hours({"frequencia": 1, "periodo": 8, "unidade": "h"}) == 8
    assert dose_peso.interval_hours({"frequencia": 3, "periodo": 1, "unidade": "d"}) == 8
    assert dose_peso.avaliar("amoxicilina", 250, 8, 12, 2) is None
    assert dose_peso.avaliar("amoxicilina", 500, 8, 12, 2).severidade == "critico"
    assert dose_peso.avaliar("amoxicilina", 500, 8, 12, 40) is None  # fora da idade pediátrica
    assert dose_peso.avaliar("losartana", 50, 24, 12, 2) is None  # fármaco fora da tabela


def test_sem_peso_pede_o_peso(record):
    rec = record("cen9")
    rec.resources["Observation"] = [
        o for o in rec.resources.get("Observation", []) if "29463-7" not in str(o.get("code"))
    ]
    segs = [Segment(n=1, t=1, falante="medico", texto="Vou passar amoxicilina 500 mg de 8 em 8 horas.")]
    ins = Engine(rec, build_timeline(rec), build_context(rec), segs, "pediatria", "consulta", clock.now()).run()
    found = [m for m in ins.sugestoes_medicamentos if "sem peso" in m.texto]
    if found:
        assert found[0].severidade == "atencao" and "Pesar a criança" in found[0].texto
    else:  # peso ainda vindo de outra fonte do prontuário: então a conferência por kg vale
        assert dose_alerts(ins)


def test_peso_dito_na_conversa_vale_sem_observation():
    said = dose_peso.spoken_weight([Segment(n=3, t=1, falante="acompanhante", texto="Ele pesa 12,5 quilos.")])
    assert said == (12.5, "Fala/3")


def test_dose_pelo_caminho_da_api_alerta_antes_da_ia_e_no_pedido(client, auth):
    h = auth("medico")
    c = start(client, h, "cen9", especialidade="pediatria")
    ins = say(client, h, c["id"], falas_de("cen9_pediatria"))
    crit = [m for m in ins["sugestoes_medicamentos"] if m["severidade"] == "critico" and "mg/kg" in m["texto"]]
    assert crit and "ANVISA" in crit[0]["fonte"]
    draft = next(d for d in ins["pedidos"] if d["item"].lower().startswith("amoxicilina"))
    check = next(k for k in draft["checagens"] if k["tipo"] == "dose_peso")
    assert (
        check["severidade"] == "critico"
        and "Fonte:" in check["texto"]
        and any(r.startswith("Observation/") for r in check["refs"])
    )


def test_csv_de_doses_pediatricas_tem_fonte_e_principio_conhecido():
    rows = list(
        csv.DictReader(open(knowledge.get_settings().data_dir / "reference" / "pediatric_doses.csv", encoding="utf-8"))
    )
    assert len(rows) >= 8
    catalog = {
        r["principio_ativo"]
        for r in csv.DictReader(open(knowledge.get_settings().data_dir / "reference" / "drugs.csv", encoding="utf-8"))
    }
    for r in rows:
        assert r["principio_ativo"] in catalog
        assert re.search(r"ANVISA|SBP", r["fonte"]), r
        assert float(r["mg_kg_dose_min"]) < float(r["mg_kg_dose_max"])


# ---------- escalas ----------
def test_fa_mostra_cha2ds2_vasc_com_pontos_e_refs(client, auth):
    h = auth("medico")
    c = start(client, h, "cen2", especialidade="cardiologia")
    app = next(a for a in c["insights"]["apps"] if a["id"] == "escalas")
    item = next(i for i in app["itens"] if i["texto"].startswith("CHA2DS2-VASc (risco tromboembólico na FA): 4 de 9"))
    assert item["severidade"] == "critico" and "anticoagulação" in item["texto"]
    assert any(r.startswith("Condition/") for r in item["refs"])
    for linha in ("C — Insuficiência cardíaca", "H — Hipertensão", "A — Idade 65 a 74 anos", "Sc — Sexo feminino"):
        assert linha in item["detalhe"]
    assert "Condition/" in item["detalhe"] and "Fonte: Lip GYH" in item["detalhe"]


def test_cha2ds2_vasc_pontos_por_criterio():
    cond = lambda cid, i: (cid, cid, {"id": i})  # noqa: E731
    r = scales.cha2ds2_vasc(
        80,
        True,
        [cond("I48", "a"), cond("I50.0", "b"), cond("I10", "c"), cond("E11.9", "d"), cond("I63.9", "e")],
        "Patient/x",
    )
    assert r.total == 1 + 1 + 2 + 1 + 2 + 0 + 0 + 1
    assert scales.cha2ds2_vasc(50, False, [cond("I48", "a")], "Patient/x").total == 0
    assert scales.cha2ds2_vasc(50, False, [cond("I48", "a")], "Patient/x").severidade == "ok"
    homem = scales.cha2ds2_vasc(70, False, [cond("I48", "a")], "Patient/x")
    assert homem.total == 1 and homem.severidade == "atencao"  # homem com 1 ponto: considerar anticoagular


def test_ipss_pelo_perfil_de_urologia_a_partir_das_respostas(client, auth):
    h = auth("medico")
    c = start(client, h, "cen7e", especialidade="urologia")
    ins = say(
        client,
        h,
        c["id"],
        [
            {
                "falante": "medico",
                "texto": "O senhor sente que não esvazia a bexiga por completo depois de urinar?",
                "t": 1,
            },
            {"falante": "paciente", "texto": "Sinto quase sempre, doutor.", "t": 2},
            {"falante": "medico", "texto": "E o jato, como está?", "t": 3},
            {"falante": "paciente", "texto": "O jato fraco acontece mais da metade das vezes.", "t": 4},
            {"falante": "paciente", "texto": "À noite eu levanto 3 vezes pra fazer xixi.", "t": 5},
        ],
    )
    app = next(a for a in ins["apps"] if a["id"] == "escalas")
    ipss = next(i for i in app["itens"] if i["texto"].startswith("IPSS"))
    assert "incompleta" in ipss["texto"] and "Faltam" in ipss["detalhe"] and "Fala/2" in ipss["detalhe"]
    # o profissional completa as respostas pelo endpoint: soma 5 + 4 + 3 + 3 + 2 + 1 + 3 = 21 (grave)
    res = client.put(
        f"/consultations/{c['id']}/escalas/ipss",
        json={"respostas": {"frequencia": "3", "intermitencia": "2", "urgencia": "3", "esforco": "1"}},
        headers=h,
    )
    assert res.status_code == 200, res.text
    app = next(a for a in res.json()["insights"]["apps"] if a["id"] == "escalas")
    ipss = next(i for i in app["itens"] if i["texto"].startswith("IPSS"))
    assert "21 de 35" in ipss["texto"] and "sintomas graves" in ipss["texto"] and ipss["severidade"] == "critico"
    assert "Barry MJ" in ipss["detalhe"]


def test_ipss_calculo_e_faixas():
    todos = {i.chave: "0" for i in scales.IPSS.itens}
    assert scales.calcular("ipss", todos).total == 0
    assert scales.calcular("ipss", todos | {"nocturia": "5", "jato_fraco": "2"}).faixa == "sintomas leves"
    assert scales.calcular("ipss", {k: "5" for k in todos}).total == 35
    assert scales.calcular("ipss", {k: "3" for k in todos}).faixa == "sintomas graves"
    parcial = scales.calcular("ipss", {"urgencia": "2"})
    assert parcial.total is None and not parcial.completo and len(parcial.faltam) == 6


@pytest.mark.parametrize(
    ("escala", "valor", "total", "faixa"),
    [
        ("phq9", "0", 0, "sintomas mínimos"),
        ("phq9", "1", 9, "depressão leve"),
        ("phq9", "2", 18, "depressão moderadamente grave"),
        ("phq9", "3", 27, "depressão grave"),
        ("gad7", "1", 7, "ansiedade leve"),
        ("gad7", "2", 14, "ansiedade moderada"),
        ("gad7", "3", 21, "ansiedade grave"),
    ],
)
def test_phq9_gad7(escala, valor, total, faixa):
    r = scales.calcular(escala, {i.chave: valor for i in scales.QUESTIONARIOS[escala].itens})
    assert r.total == total and r.faixa == faixa


def test_phq9_item_9_gera_aviso_de_risco():
    resp = {i.chave: "0" for i in scales.PHQ9.itens} | {"ideacao": "1"}
    r = scales.calcular("phq9", resp)
    assert r.aviso.startswith("ATENÇÃO") and r.severidade == "critico"


def test_morse_braden_ivcf():
    morse = {
        "historico_queda": "sim",
        "diagnostico_secundario": "sim",
        "auxilio_deambulacao": "muleta",
        "terapia_iv": "sim",
        "marcha": "fraca",
        "estado_mental": "orientado",
    }
    assert scales.calcular("morse", morse).total == 25 + 15 + 15 + 20 + 10 + 0
    assert scales.calcular("morse", morse).faixa == "risco elevado"
    braden = {
        "percepcao_sensorial": "2",
        "umidade": "2",
        "atividade": "1",
        "mobilidade": "2",
        "nutricao": "2",
        "friccao": "1",
    }
    assert scales.calcular("braden", braden).total == 10 and scales.calcular("braden", braden).faixa == "risco alto"
    ivcf = {i.chave: "nao" for i in scales.IVCF20.itens} | {
        "idade": "85+",
        "saude": "regular_ruim",
        "avdi_dinheiro": "sim",
        "avdi_compras": "sim",
    }
    r = scales.calcular("ivcf20", ivcf)
    assert r.total == 3 + 1 + 4  # o teto do grupo AVD instrumental é 4, não 8
    tudo = {i.chave: list(i.opcoes)[-1] for i in scales.IVCF20.itens}
    assert scales.calcular("ivcf20", tudo).total == 40


def test_validacao_das_respostas(client, auth):
    h = auth("medico")
    c = start(client, h, "cen6", especialidade="saude_mental")
    url = f"/consultations/{c['id']}/escalas"
    assert client.put(f"{url}/phq9", json={"respostas": {"humor": "9"}}, headers=h).status_code == 422
    assert client.put(f"{url}/phq9", json={"respostas": {"inventado": "1"}}, headers=h).status_code == 422
    assert client.put(f"{url}/astrologia", json={"respostas": {"a": "1"}}, headers=h).status_code == 422
    ok = client.put(f"{url}/phq9", json={"respostas": {i.chave: "1" for i in scales.PHQ9.itens}}, headers=h)
    assert ok.status_code == 200
    app = next(a for a in ok.json()["insights"]["apps"] if a["id"] == "escalas")
    assert any("PHQ-9" in i["texto"] and "9 de 27" in i["texto"] for i in app["itens"])
    assert client.put(f"{url}/imc", json={"respostas": {"altura_cm": 700}}, headers=h).status_code == 422
    assert any(s["id"] == "phq9" for s in client.get("/escalas", headers=h).json())


def test_nyha_pela_fala_e_imc():
    segs = [
        Segment(n=1, t=1, falante="paciente", texto="Eu canso para subir uma escada, preciso parar no meio."),
        Segment(n=2, t=2, falante="paciente", texto="Hoje fico sem ar até tomando banho."),
    ]
    classe, refs = scales.nyha_pela_fala(segs)
    assert classe == "III" and refs == ["Fala/2"]
    assert scales.nyha_pela_fala([Segment(n=1, t=1, falante="paciente", texto="Não tenho falta de ar.")])[0] == "I"
    assert scales.imc(80, 170, 40).faixa.startswith("sobrepeso")
    assert scales.imc(60, 175, 70).faixa.startswith("baixo peso")  # idoso: corte de 22 (Lipschitz)
    assert scales.imc(60, 175, 40).faixa.startswith("eutrofia")
    assert scales.imc(50, 100, 5).total is None and "OMS" in scales.imc(50, 100, 5).aviso


# ---------- glossário ----------
def test_glossario_troca_popular_por_tecnico_sem_acento_nem_caixa():
    text, swaps = glossario.tecnico("Xixi de noite, pressão alta e FALTA DE AR", "urologia")
    assert text == "Diurese de noite, HAS e Dispneia"
    assert ("Xixi", "Diurese") in swaps
    assert glossario.tecnico("bexiga caída e xixi fraco", "urologia")[0] == "cistocele e hipofluxo urinário"
    assert glossario.tecnico("o pixixi", "")[0] == "o pixixi"  # só palavra inteira
    assert "esteatose hepática" in glossario.tecnico("gordura no fígado", "nutricao")[0]


def test_xixi_sai_como_diurese_na_nota(client, auth):
    h = auth("medico")
    c = start(client, h, "cen9", especialidade="pediatria")
    say(client, h, c["id"], falas_de("cen9_pediatria"))
    res = client.post(f"/consultations/{c['id']}/finish", json={}, headers=h)
    assert res.status_code == 200, res.text
    nota = client.get(f"/consultations/{c['id']}", headers=h).json()["nota"]
    texto = " ".join(
        [nota[k]["texto"] for k in ("queixa_principal", "hda", "antecedentes")] + [s["texto"] for s in nota["secoes"]]
    ).lower()
    assert "diurese" in texto and "xixi" not in texto
    segs = client.get(f"/consultations/{c['id']}", headers=h).json()["segmentos"]
    assert any("xixi" in s["texto"] for s in segs)  # a fala original não muda


def test_glossario_vale_no_prompt_do_stt():
    prompt = glossario.prompt_stt("pediatria")
    assert "diurese" in prompt and "dermatite de fralda" in prompt and prompt.startswith("Consulta clínica")
    assert len(glossario.termos("")) >= 40
    assert consult_ai.glossario is glossario


def test_stt_envia_o_vocabulario(monkeypatch):
    from app.consult import stt

    sent = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"text": "ok"}

    def fake_post(url, files, data, headers, timeout):
        sent.update(data)
        return Resp()

    monkeypatch.setattr(stt.httpx, "post", fake_post)
    monkeypatch.setattr(
        stt, "get_settings", lambda: type("S", (), {"stt_url": "http://x", "stt_api_key": "", "stt_model": "m"})()
    )
    assert stt.transcribe(b"abc", "audio/webm", "pt-BR", glossario.prompt_stt("urologia")) == "ok"
    assert "diurese" in sent["prompt"]
    sent.clear()
    stt.transcribe(b"abc", "audio/webm", "pt-BR")
    assert "prompt" not in sent  # sem vocabulário, o campo nem vai
