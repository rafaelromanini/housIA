"""Corpus de regressão das regras da consulta (data/consultations/regressao_falas.json).

Cada frase passa pelo caminho real (falante automático → Engine sobre o prontuário sintético, LLM mock) e o
resultado é comparado ao esperado, categoria a categoria. O teste agregado mede falso positivo e falso negativo do
corpus inteiro a cada build: alerta falso destrói o diferencial "regras antes da IA". O corpus é um ativo vivo —
frase nova que expõe um erro entra com `limitacao` (xfail estrito) até a regra ser corrigida.
"""

import json
import re

import pytest

from app import clock
from app.consult import service as consult_service
from app.consult.engine import PATIENT_SIDE, Engine, infer_speaker
from app.consult.knowledge import demo_scripts, regression_corpus
from app.consult.schemas import Segment
from app.consult.text import mentioned_drugs
from app.llm import client as llm_client
from app.patients.service import load_record
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from tests.test_consult import FakeClient

CORPUS = regression_corpus()
FRASES = CORPUS["frases"]
CATEGORIAS = ("queixas", "emocoes", "medicamentos", "alertas", "cirurgia", "antecedente_familiar")
SPEAKERS = {"medico", "paciente", "acompanhante", "desconhecido"}


@pytest.fixture(scope="module")
def charts(store):
    """Prontuário (record, eventos, contexto) e alertas que ele já gera sem conversa, por paciente."""
    cache: dict[str, tuple] = {}

    def _get(pid: str) -> tuple:
        if pid not in cache:
            record = load_record(store, pid)
            chart = (record, build_timeline(record), build_context(record))
            baseline = {m.texto for m in _engine(chart, []).run().sugestoes_medicamentos}
            cache[pid] = (chart, baseline)
        return cache[pid]

    return _get


def _engine(chart: tuple, segments: list[Segment]) -> Engine:
    record, events, ctx = chart
    return Engine(record, events, ctx, segments, "clinica_medica", "consulta", clock.now())


def avaliar(frase: dict, charts) -> dict:
    """O que as regras extraem da frase (um único segmento), por categoria, em conjuntos comparáveis."""
    chart, baseline = charts(frase.get("paciente", CORPUS["paciente_padrao"]))
    falante = frase["falante"]
    auto = falante == "desconhecido"
    if auto:
        falante = infer_speaker(frase["frase"])
    seg = Segment(n=1, t=0, falante=falante, falante_auto=auto, texto=frase["frase"])
    ins = _engine(chart, [seg]).run()
    patient_side = falante in PATIENT_SIDE
    return {
        "falante": falante,
        "queixas": {(t.rotulo, t.status) for t in ins.queixas},
        "emocoes": {e.emocao for e in ins.emocoes},
        "medicamentos": {(m.ingredient, m.status) for m in mentioned_drugs(frase["frase"])} if patient_side else set(),
        "alertas": {
            "triplice" if "Tríplice" in m.texto else m.tipo
            for m in ins.sugestoes_medicamentos
            if m.texto not in baseline
        },
        "cirurgia": {i.tipo for i in ins.cirurgia if any(r.startswith("Fala/") for r in i.refs)},
        "antecedente_familiar": {True} if any(h.tipo == "familiar" for h in ins.historico) else set(),
    }


def esperado(frase: dict) -> dict:
    e = frase["esperado"]
    return {
        "falante": e["falante"],
        "queixas": set(e.get("queixas", {}).items()),
        "emocoes": set(e.get("emocoes", [])),
        "medicamentos": set(e.get("medicamentos", {}).items()),
        "alertas": set(e.get("alertas", [])),
        "cirurgia": set(e.get("cirurgia", [])),
        "antecedente_familiar": {True} if e.get("antecedente_familiar") else set(),
    }


def diferencas(got: dict, exp: dict) -> dict[str, dict[str, list]]:
    """categoria → falsos positivos (extraído e não esperado) e falsos negativos (esperado e não extraído)."""
    out = {}
    for k in CATEGORIAS:
        if got[k] != exp[k]:
            out[k] = {
                "falso_positivo": sorted(map(str, got[k] - exp[k])),
                "falso_negativo": sorted(map(str, exp[k] - got[k])),
            }
    return out


def _params() -> list:
    return [
        pytest.param(f, id=f["id"], marks=[pytest.mark.xfail(reason=f["limitacao"], strict=True)])
        if f.get("limitacao")
        else pytest.param(f, id=f["id"])
        for f in FRASES
    ]


# ---------- o corpus em si ----------
def test_corpus_bem_formado():
    assert len(FRASES) >= 60
    ids = [f["id"] for f in FRASES]
    assert len(ids) == len(set(ids)), "ids repetidos no corpus"
    for f in FRASES:
        assert (
            f["frase"].strip() and f["falante"] in SPEAKERS and f["esperado"]["falante"] in SPEAKERS - {"desconhecido"}
        )
        assert set(f["esperado"]) <= {"falante", *CATEGORIAS}, f["id"]
        if f["falante"] != "desconhecido":
            assert f["esperado"]["falante"] == f["falante"], f["id"]
    # cobre os modos de falha que motivaram o corpus: falante, negação, sem pontuação, medicamento, emoção, cirurgia
    assert {"falante", "negacao", "sem_pontuacao", "medicamento", "emocao", "cirurgia", "controle"} <= {
        f["categoria"] for f in FRASES
    }
    # há controles positivos: suprimir tudo não passa no corpus
    assert sum(1 for f in FRASES if f["categoria"] == "controle") >= 15


@pytest.mark.parametrize("frase", _params())
def test_frase_do_corpus(frase, charts):
    got, exp = avaliar(frase, charts), esperado(frase)
    assert got["falante"] == exp["falante"], f"falante: {got['falante']} (esperado {exp['falante']})"
    assert not diferencas(got, exp), diferencas(got, exp)


def test_corpus_mede_falso_positivo_a_cada_build(charts):
    """Métrica do corpus inteiro (sem as limitações conhecidas): zero falso positivo é o critério de build."""
    fp = {k: 0 for k in CATEGORIAS}
    fn = {k: 0 for k in CATEGORIAS}
    extraidos = {k: 0 for k in CATEGORIAS}
    falante_ok = avaliadas = 0
    falhas = []
    for f in FRASES:
        if f.get("limitacao"):
            continue
        avaliadas += 1
        got, exp = avaliar(f, charts), esperado(f)
        falante_ok += got["falante"] == exp["falante"]
        for k in CATEGORIAS:
            extraidos[k] += len(got[k])
            fp[k] += len(got[k] - exp[k])
            fn[k] += len(exp[k] - got[k])
        if diferencas(got, exp) or got["falante"] != exp["falante"]:
            falhas.append(f"{f['id']}: {diferencas(got, exp)} falante={got['falante']}")
    relatorio = (
        f"{avaliadas} frases ({len(FRASES) - avaliadas} limitações conhecidas) | falante {falante_ok}/{avaliadas} | "
        + " | ".join(f"{k}: {fp[k]} FP / {fn[k]} FN em {extraidos[k]} extraídos" for k in CATEGORIAS)
    )
    assert sum(fp.values()) == 0, relatorio + "\n" + "\n".join(falhas)
    assert sum(fn.values()) == 0 and falante_ok == avaliadas, relatorio + "\n" + "\n".join(falhas)
    # o corpus exercita de verdade cada categoria (não passa por não extrair nada)
    assert all(extraidos[k] > 0 for k in CATEGORIAS), relatorio


def test_falante_automatico_nos_roteiros_de_demo():
    """A heurística acerta o lado (profissional × paciente/acompanhante) nos roteiros, com e sem pontuação."""
    for script in ("cen6_consulta", "cen1_visita"):
        falas = demo_scripts()[script]["falas"]
        lado = lambda s: "medico" if s == "medico" else "paciente"  # noqa: E731
        assert all(lado(infer_speaker(f["texto"])) == lado(f["falante"]) for f in falas), script
        exato = sum(infer_speaker(f["texto"]) == f["falante"] for f in falas)
        assert exato / len(falas) >= 0.9, (script, exato)
        # como chega do microfone: sem pontuação e em minúsculas
        mic = [(re.sub(r"[.,;:!?…]", "", f["texto"]).lower(), f["falante"]) for f in falas]
        assert sum(lado(infer_speaker(t)) == lado(s) for t, s in mic) / len(mic) >= 0.9, script


# ---------- critérios de aceite pela API (TestClient, cenário real, LLM mock) ----------
@pytest.fixture
def consult(client, auth):
    consult_service.clear_chart_cache()
    h = auth("medico")

    def _new(pid: str = "cen6") -> tuple[int, dict]:
        res = client.post(f"/patients/{pid}/consultations", json={"tipo": "consulta"}, headers=h)
        assert res.status_code == 200, res.text
        return res.json()["id"], h

    yield _new
    consult_service.clear_chart_cache()


def _say(client, h, cid, *falas: tuple[str | None, str]) -> dict:
    segs = [{"texto": t} | ({"falante": who} if who else {}) for who, t in falas]
    res = client.post(f"/consultations/{cid}/segments", json={"segmentos": segs}, headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def test_pergunta_sem_interrogacao_em_automatico_e_do_medico_e_nao_vira_queixa(client, consult):
    cid, h = consult()
    out = _say(client, h, cid, (None, "a senhora tem sentido dor no peito"))
    assert (out["novos"][0]["falante"], out["novos"][0]["falante_auto"]) == ("medico", True)
    assert out["insights"]["queixas"] == []


def test_vocativo_e_marcador_sem_pontuacao_separam_a_negativa_da_queixa(client, consult):
    cid, h = consult()
    out = _say(client, h, cid, (None, "não doutora só tontura quando levanto"))
    assert out["novos"][0]["falante"] == "paciente"
    tags = {t["rotulo"]: t["status"] for t in out["insights"]["queixas"]}
    assert tags == {"Tontura": "presente", "Tontura ao levantar": "presente"}


def test_aine_suspenso_ou_negado_nao_gera_alerta_de_aine_nem_triplice_ameaca(client, consult):
    cid, h = consult()
    ins = _say(
        client,
        h,
        cid,
        ("paciente", "Não tomo mais ibuprofeno, parei faz um mês"),
        ("paciente", "Nunca tomei anti-inflamatório"),
    )["insights"]
    # o ciclo da IA (mock) re-mescla e também não pode reintroduzir o alerta
    ins = client.post(f"/consultations/{cid}/insights", headers=h).json()["insights"]
    textos = " ".join(m["texto"] for m in ins["sugestoes_medicamentos"] + ins["alertas"])
    assert "Tríplice" not in textos and "AINE" not in textos and "anti-inflamatório" not in textos.lower()
    assert "Ibuprofeno" not in textos
    assert not any(h_["cid"] == "N17.9" and "Ibuprofeno" in str(h_["a_favor"]) for h_ in ins["hipoteses"])
    # o suspenso vira histórico: aparece como suspenso, não como em uso
    meds = {m["medicamento"]: m["status"] for m in ins["medicamentos"]}
    assert meds["Ibuprofeno"] == "suspenso" and "anti-inflamatório" not in meds
    assert any("Ibuprofeno" in h_["texto"] and h_["tipo"] == "relatado" for h_ in ins["historico"])
    assert not any("Ibuprofeno" in m for m in ins["anamnese"]["medicacoes_em_uso"])


def test_ibuprofeno_em_uso_continua_gerando_triplice_ameaca(client, consult):
    """Controle: a negação não pode apagar o alerta verdadeiro."""
    cid, h = consult()
    ins = _say(client, h, cid, ("paciente", "Tomo ibuprofeno todo dia, mas não tá adiantando"))["insights"]
    assert any("Tríplice" in m["texto"] for m in ins["sugestoes_medicamentos"])


def test_negacao_de_emocao_e_duvida(client, consult):
    cid, h = consult()
    ins = _say(client, h, cid, (None, "Não estou preocupada, não"), (None, "Não tenho medo nenhum"))["insights"]
    assert ins["emocoes"] == []
    ins = _say(client, h, cid, (None, "Não entendi, doutora"))["insights"]
    assert [(e["emocao"], e["refs"]) for e in ins["emocoes"]] == [("confusao", ["Fala/3"])]


def test_nao_vai_precisar_operar_nao_cria_cirurgia_planejada(client, consult):
    cid, h = consult()
    ins = _say(client, h, cid, (None, "Não vai precisar operar o joelho"))["insights"]
    assert not any("Fala/1" in i["refs"] for i in ins["cirurgia"])
    # paciente sem cirurgia programada no prontuário: nada de planejada nem de checagem pré-operatória
    cid, h = consult("cen3")
    ins = _say(client, h, cid, (None, "Não vai precisar operar o joelho"))["insights"]
    assert not any(i["tipo"] in ("planejada", "checagem") for i in ins["cirurgia"])
    # controle: cirurgia de fato planejada continua gerando as checagens
    ins = _say(client, h, cid, ("paciente", "o ortopedista quer operar o joelho mês que vem"))["insights"]
    assert any(i["tipo"] == "planejada" for i in ins["cirurgia"]) and any(
        i["tipo"] == "checagem" for i in ins["cirurgia"]
    )


def test_ia_recebe_o_status_dos_medicamentos_relatados(client, consult, monkeypatch):
    """A IA vê o que as regras concluíram (suspenso ≠ em uso) e o ciclo dela não reintroduz o alerta."""
    cid, h = consult()
    _say(client, h, cid, ("paciente", "Não tomo mais ibuprofeno, parei faz um mês"))
    anamnese = {
        "queixa_principal": "",
        "hda": "",
        "antecedentes": [],
        "medicacoes_em_uso": [],
        "alergias": [],
        "habitos_e_contexto": [],
        "revisao_de_sistemas": [],
        "refs": [],
    }
    empty = {k: [] for k in ("destaques", "queixas", "emocoes", "perguntas", "falantes", "hipoteses", "cirurgia")}
    fake = FakeClient(
        empty | {"resumo": "", "sugestoes_medicamentos": [], "historico_relatado": [], "anamnese": anamnese}
    )
    monkeypatch.setattr(llm_client.get_settings(), "llm_mode", "real")
    monkeypatch.setattr(llm_client, "_client", fake)
    res = client.post(f"/consultations/{cid}/insights", headers=h)
    monkeypatch.setattr(llm_client, "_client", None)
    assert res.status_code == 200, res.text
    sent = json.dumps(fake.calls[0], ensure_ascii=False, default=str)
    assert "medicamentos_relatados" in sent and '\\"status\\": \\"suspenso\\"' in sent
    textos = " ".join(m["texto"] for m in res.json()["insights"]["sugestoes_medicamentos"])
    assert "Tríplice" not in textos and "Ibuprofeno" not in textos
