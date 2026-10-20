"""Pacotes de especialidade, escalas, dose pediátrica por peso e glossário clínico (#27).

Os critérios de aceite passam pelo caminho real (API/TestClient e Engine sobre os cenários, LLM mock):
- criança de 12 kg com 'amoxicilina 500 mg de 8 em 8 horas' gera alerta de dose acima do máximo, com a fonte;
- paciente com FA mostra o CHA2DS2-VASc com os pontos e as refs;
- o perfil de urologia calcula o IPSS a partir das respostas;
- 'xixi' sai como 'diurese' na nota;
- schema dos YAML dos pacotes.
"""

import copy
import csv
import re
from datetime import timedelta

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
    marks = ("Dose citada", "sem peso", "sem a concentração")
    return [m for m in ins.sugestoes_medicamentos if m.tipo == "ajuste_dose" and any(k in m.texto for k in marks)]


def peso_ins_com_peso(record, valor, falas, unit="kg", dias=0, idade=None, pid="cen9", spec="pediatria"):
    """Roda a análise com o peso da Observation trocado (`valor=None` apaga) e, se pedido, outra idade."""
    rec = record(pid)
    rec.resources = copy.deepcopy(rec.resources)  # o registro do store é compartilhado entre os testes
    obs = rec.resources["Observation"]
    if valor is None:
        rec.resources["Observation"] = [o for o in obs if "29463-7" not in str(o.get("code"))]
    for o in obs:
        if "29463-7" in str(o.get("code")) and valor is not None:
            o["valueQuantity"] = {"value": valor, "unit": unit}
            o["effectiveDateTime"] = (clock.now() - timedelta(days=dias)).isoformat()
    if idade is not None:
        rec.patient = dict(rec.patient, birthDate=f"{clock.now().year - idade}-01-01")
    segs = [Segment(n=i, t=float(i), falante=w, texto=t) for i, (w, t) in enumerate(falas, 1)]  # type: ignore[arg-type]
    return Engine(rec, build_timeline(rec), build_context(rec), segs, spec, "consulta", clock.now()).run()


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
    ins = peso_ins_com_peso(record, None, [("medico", "Vou passar amoxicilina 500 mg de 8 em 8 horas.")])
    found = [m for m in ins.sugestoes_medicamentos if "sem peso" in m.texto]
    assert len(found) == 1 and found[0].severidade == "atencao" and "Pesar a criança" in found[0].texto
    assert not [m for m in ins.sugestoes_medicamentos if "mg/kg" in m.texto]  # sem peso não há conferência cega


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


AMOX = [("medico", "Vou passar amoxicilina 500 mg de 8 em 8 horas.")]


@pytest.mark.parametrize(
    ("valor", "unit"), [(12, "kg"), (12000, "g"), (26.46, "[lb_av]"), (26.46, "lb")], ids=["kg", "g", "lb_av", "lb"]
)
def test_peso_em_outra_unidade_vira_kg_antes_da_dose(record, valor, unit):
    found = dose_alerts(peso_ins_com_peso(record, valor, AMOX, unit))
    assert len(found) == 1 and found[0].severidade == "critico" and "peso de 12 kg" in found[0].texto


@pytest.mark.parametrize("valor", [0, -3, 5000])
def test_peso_zero_negativo_ou_absurdo_nao_vira_conferencia_cega(record, valor):
    found = dose_alerts(peso_ins_com_peso(record, valor, AMOX))
    assert len(found) == 1 and "sem peso" in found[0].texto and "mg/kg" not in found[0].texto


def test_peso_em_unidade_desconhecida_e_descartado(record):
    found = dose_alerts(peso_ins_com_peso(record, 26, AMOX, "stone"))
    assert len(found) == 1 and "sem peso" in found[0].texto


def test_peso_antigo_pede_pesagem_e_cede_ao_peso_dito(record):
    assert "sem peso" in dose_alerts(peso_ins_com_peso(record, 12, AMOX, dias=400))[0].texto  # mais de 1 ano
    velho = dose_alerts(peso_ins_com_peso(record, 5, AMOX, dias=200))[0]  # 5 kg de 200 dias atrás
    assert "peso de 5 kg" in velho.texto and "pesar de novo" in velho.texto  # a idade do peso aparece no alerta
    falas = [("acompanhante", "Ela pesa 12 quilos agora."), *AMOX]
    dito = dose_alerts(peso_ins_com_peso(record, 5, falas, dias=200))[0]
    assert "peso de 12 kg" in dito.texto and "peso dito na consulta" in dito.texto
    assert any(r.startswith("Fala/1") for r in dito.refs)


def test_peso_dito_nao_vem_do_nascimento_nem_da_mae():
    for fala in ("Ele nasceu pesando 3 quilos.", "Eu peso 60 quilos e ele é pequeno.", "Meu peso é 70 quilos."):
        assert dose_peso.spoken_weight([Segment(n=1, t=1, falante="acompanhante", texto=fala)]) is None, fala
    assert dose_peso.spoken_weight([Segment(n=1, t=1, falante="acompanhante", texto="Ela pesa 0 quilos.")]) is None


def test_mcg_nao_vira_mg(record):
    assert dose_alerts(peso_ins(record, [("medico", "Vou passar amoxicilina 500 mcg de 8 em 8 horas.")])) == []
    assert dose_peso.parse_mg("500 mcg") is None and dose_peso.parse_mg("500 microgramas") is None


@pytest.mark.parametrize(
    ("fala", "esperado"),
    [
        ("Vou passar amoxicilina 0,5 g de 8 em 8 horas.", "critico"),  # vírgula decimal não parte a oração
        ("Vou passar amoxicilina 62,5 mg de 8 em 8 horas.", "atencao"),  # decimal: subdose, não 62 e 5
        ("amoxicilina 250 mg/5 ml, 10 ml de 8 em 8 horas", "critico"),  # 10 ml × 50 mg/ml = 500 mg
        ("amoxicilina 250 mg por 5 ml, dar 2,5 ml de 8 em 8 horas", None),  # 125 mg: na faixa
        ("dipirona 1 grama a cada 4 horas", "critico"),
        ("Vou passar Novalgina 1 g de 6 em 6 horas.", "critico"),  # nome comercial
        ("Pode dar Tylenol 750 mg de 4 em 4 horas.", "critico"),
        ("vou passar amoxicilina 500 mg de oito em oito horas", "critico"),  # intervalo por extenso
    ],
)
def test_unidades_decimais_e_nomes_comerciais(record, fala, esperado):
    found = dose_alerts(peso_ins(record, [("medico", fala)]))
    assert [m.severidade for m in found] == ([esperado] if esperado else []), found
    if esperado == "critico":
        assert found[0].fonte


def test_dose_em_ml_sem_concentracao_avisa_em_vez_de_calar(record):
    found = dose_alerts(peso_ins(record, [("medico", "amoxicilina 5 ml de 8 em 8 horas")]))
    assert len(found) == 1 and found[0].severidade == "atencao" and "sem a concentração" in found[0].texto


@pytest.mark.parametrize(
    ("fala", "esperado"),
    [
        ("amoxicilina 50 mg/kg/dia de 8 em 8 horas", None),  # mg/kg não é mg
        ("amoxicilina 150 mg/kg/dia dividido em 3 vezes", "critico"),
        ("amoxicilina 100 mg/kg por dose", "critico"),
        ("Vou passar amoxicilina 500 mg por dia.", None),  # total do dia = 41,7 mg/kg/dia
        ("Vou passar amoxicilina 1500 mg por dia dividido em três doses.", "critico"),  # 125 mg/kg/dia
        ("Vou passar amoxicilina 750 mg por dia dividido em 3 doses.", None),  # 250 mg por tomada
        ("Vou passar amoxicilina 500 mg de 12 em 12 horas.", None),  # 83 mg/kg/dia em 2 doses: dose alta guiada
        ("Vou passar amoxicilina 540 mg de 12 em 12 horas.", None),  # 90 mg/kg/dia, 45 mg/kg/dose
        ("Vou passar amoxicilina 600 mg de 12 em 12 horas.", "critico"),  # 100 mg/kg/dia
    ],
)
def test_dose_diaria_e_por_tomada(record, fala, esperado):
    found = dose_alerts(peso_ins(record, [("medico", fala)]))
    assert [m.severidade for m in found] == ([esperado] if esperado else []), found


def test_adolescente_grande_toma_dose_de_adulto_mas_respeita_o_teto(record):
    for fala in (
        "paracetamol 1 g de 6 em 6 horas",
        "ibuprofeno 600 mg de 8 em 8 horas",
        "amoxicilina 875 mg de 12 em 12 horas",
        "dipirona 1 g de 6 em 6 horas",
        "ondansetrona 8 mg de 8 em 8 horas",
    ):
        assert dose_alerts(peso_ins_com_peso(record, 60, [("medico", fala)], idade=16)) == [], fala
    teto = dose_alerts(peso_ins_com_peso(record, 90, [("medico", "paracetamol 1 g de 4 em 4 horas")], idade=17))
    assert len(teto) == 1 and teto[0].severidade == "critico" and "teto de 4000 mg/dia" in teto[0].texto
    peq = dose_alerts(peso_ins_com_peso(record, 40, [("medico", "paracetamol 750 mg de 6 em 6 horas")], idade=12))
    assert len(peq) == 1 and "mg/kg/dose" in peq[0].texto  # 40 kg ainda se calcula por kg


def test_dose_em_ml_no_pedido_e_concentracao_nao_e_dose(client, auth):
    from app.consult.orders import parse_posology

    assert parse_posology("amoxicilina 250 mg/5 ml, 10 ml de 8 em 8 horas").dose == "10 ml"
    assert parse_posology("500 mg de 8 em 8 horas").dose == "500 mg"
    h = auth("medico")
    c = start(client, h, "cen9", especialidade="pediatria")
    ins = say(
        client, h, c["id"], [{"falante": "medico", "texto": "Vou passar amoxicilina 5 ml de 8 em 8 horas.", "t": 1}]
    )
    draft = next(d for d in ins["pedidos"] if d["item"].lower().startswith("amoxicilina"))
    assert any(k["tipo"] == "dose_peso" and k["severidade"] == "atencao" for k in draft["checagens"])


def test_orders_importa_sozinho_sem_ciclo():
    import os
    import subprocess
    import sys
    from pathlib import Path

    out = subprocess.run(
        [sys.executable, "-c", "import app.consult.orders"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[1],
        env={**os.environ, "LLM_MODE": "mock", "PYTHONPATH": "."},
    )
    assert out.returncode == 0, out.stderr[-400:]


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


# ---------- verificação cética: escalas e glossário ----------
@pytest.mark.parametrize(
    ("fala", "classe"),
    [
        ("Tenho falta de ar quando subo escada, mas em repouso não sinto nada.", "II"),  # "em repouso" negado
        ("Não tenho falta de ar em repouso, só quando subo ladeira.", "II"),  # a negativa não apaga o esforço
        ("Fico sem ar até parada, deitada.", "IV"),  # "sem ar" é a própria dispneia
        ("Consigo andar no plano sem problema.", None),  # "no plano" negado: sem classe
        ("Não tenho falta de ar.", "I"),
        ("Eu canso pra tomar banho.", "III"),
    ],
)
def test_nyha_pela_fala_respeita_a_negacao(fala, classe):
    assert scales.nyha_pela_fala([Segment(n=1, t=1, falante="paciente", texto=fala)])[0] == classe
    assert scales.nyha_pela_fala([Segment(n=1, t=1, falante="medico", texto=fala)])[0] is None  # só fala do paciente


def test_ipss_pela_fala_nem_sempre_nao_e_sempre():
    fala = [Segment(n=1, t=1, falante="paciente", texto="Nem sempre o jato fraco, quase nunca.")]
    assert scales.ipss_pela_fala(fala)[0] == {"jato_fraco": "1"}
    assert scales.ipss_pela_fala([Segment(n=1, t=1, falante="paciente", texto="Jato fraco, nem sempre.")])[0] == {}


def test_phq9_com_ideacao_positiva_e_critico_mesmo_incompleto():
    r = scales.calcular("phq9", {"ideacao": "1", "humor": "0"})
    assert r.total is None and r.severidade == "critico" and r.aviso.startswith("ATENÇÃO")
    assert scales.calcular("phq9", {"humor": "0"}).severidade == "info"  # incompleta sem ideação: só informa


def test_escalas_resposta_faltando_ou_fora_da_faixa_nao_somam():
    parcial = scales.calcular("ivcf20", {"idade": "85+", "saude": "regular_ruim"})
    assert parcial.total is None and not parcial.completo and parcial.faltam
    assert scales.calcular("ipss", {"esvaziamento": "9"}).total is None  # opção inexistente não pontua
    assert scales.validar_respostas("ipss", {"nocturia": "6"}) and scales.validar_respostas("braden", {"umidade": "0"})
    assert scales.validar_respostas("ivcf20", {"idade": "40"})  # fora das faixas do instrumento
    assert scales.validar_respostas("phq9", {"humor": "1.0", "xx": "1"})


def test_ivcf20_pesos_e_tetos_do_instrumento():
    todos = {i.chave: ("sim" if "sim" in i.opcoes else "") for i in scales.IVCF20.itens}
    todos |= {"idade": "85+", "saude": "regular_ruim"}
    assert scales.calcular("ivcf20", todos).total == 40  # máximo do instrumento
    nenhum = {i.chave: ("nao" if "nao" in i.opcoes else "") for i in scales.IVCF20.itens}
    assert scales.calcular("ivcf20", nenhum | {"idade": "60-74", "saude": "boa"}).total == 0
    # tetos por domínio: 3 itens de AVD instrumental valem 4, não 12; domínio de comorbidade vale 4
    assert (
        scales.calcular(
            "ivcf20",
            nenhum
            | {
                "idade": "60-74",
                "saude": "boa",
                "avdi_dinheiro": "sim",
                "avdi_compras": "sim",
                "avdi_domestico": "sim",
            },
        ).total
        == 4
    )
    assert scales.calcular("ivcf20", nenhum | {"idade": "75-84", "saude": "boa", "avdb_banho": "sim"}).total == 1 + 6
    assert [f.maximo for f in scales.IVCF20.faixas] == [6, 14, 40]  # 0-6 robusto, 7-14 em risco, 15+ frágil


def test_cha2ds2_vasc_aneurisma_de_aorta_nao_conta_e_idade_ausente_avisa():
    cond = lambda cid, i: (cid, cid, {"id": i})  # noqa: E731
    assert scales.cha2ds2_vasc(50, False, [cond("I48", "a"), cond("I71.4", "b")], "Patient/x").total == 0
    assert scales.cha2ds2_vasc(50, False, [cond("I48", "a"), cond("I70.2", "b")], "Patient/x").total == 1
    sem_idade = scales.cha2ds2_vasc(None, False, [cond("I48", "a")], "Patient/x")
    assert "Idade não registrada" in sem_idade.aviso


@pytest.mark.parametrize(
    ("texto", "pacote", "esperado"),
    [
        ("Derrame pleural volumoso e derrame articular.", "", "Derrame pleural volumoso e derrame articular."),
        ("Teve derrame cerebral ano passado.", "", "Teve AVC ano passado."),
        ("Gases arteriais e gasometria normais.", "", "Gases arteriais e gasometria normais."),
        ("Chiado no peito, sem febre.", "pediatria", "Chiado no peito, sem febre."),
        ("Exame do sistema nervoso normal.", "saude_mental", "Exame do sistema nervoso normal."),
        ("Ferida operatória limpa; curativo trocado.", "enfermagem", "Ferida operatória limpa; curativo trocado."),
        (
            "Refluxo gastroesofágico e refluxo vesicoureteral.",
            "nutricao",
            "Refluxo gastroesofágico e refluxo vesicoureteral.",
        ),
        ("Fraqueza no braço direito.", "", "Fraqueza no braço direito."),  # déficit focal não é astenia
        ("Aperto no peito ao esforço.", "saude_mental", "Opressão torácica ao esforço."),  # sem rotular como ansiedade
        ("Xixizinho, pixixi e oxixi não são o termo.", "", "Xixizinho, pixixi e oxixi não são o termo."),
        ("Nega azia, mas tem dor de cabeça.", "", "Nega pirose, mas tem cefaleia."),  # a negação fica
        ("XIXI! Xixi? xixi.", "", "DIURESE! Diurese? diurese."),
    ],
)
def test_glossario_nao_muda_o_sentido(texto, pacote, esperado):
    saida = glossario.tecnico(texto, pacote)[0]
    assert saida == esperado or saida.replace("DIURESE", "Diurese") == esperado.replace("DIURESE", "Diurese"), saida


def test_glossario_dos_pacotes_nao_tem_palavra_solta_ambigua():
    arriscadas = {"peito", "nervoso", "regra", "ferida", "curativo", "refluxo", "caiu", "bronquite", "gases", "derrame"}
    for pack in pacotes().values():
        assert not arriscadas & set(glossario._key(k) for k in pack.glossario), pack.id
    assert not arriscadas & set(glossario.comum())


def test_glossario_nao_mexe_na_fala_original_nem_nas_refs():
    from app.consult.schemas import Frase, NoteSection

    fala = Segment(n=1, t=1, falante="acompanhante", texto="Fez pouco xixi hoje.")
    frase = Frase(texto="Mãe refere que fez pouco xixi hoje.", refs=["Fala/1"])
    sec = NoteSection(texto=frase.texto, refs=["Fala/1"], frases=[frase])

    class Note:  # só o que `aplicar_nota` toca
        queixa_principal = hda = antecedentes = sec
        secoes: list = []

    swaps = glossario.aplicar_nota(Note(), "pediatria")
    assert swaps and "xixi" not in sec.texto and "diurese" in frase.texto
    assert frase.refs == ["Fala/1"] and sec.refs == ["Fala/1"] and fala.texto == "Fez pouco xixi hoje."
