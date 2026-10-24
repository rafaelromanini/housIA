"""Smart Apps como plug-ins (#21): registro com manifest, perfis no servidor, apps de transição.

Cada critério de aceite passa pelo caminho real (API/TestClient sobre os cenários sintéticos, LLM mock). O golden do
cen6 fica em test_smart_apps_golden.py.
"""

import csv
import textwrap
from datetime import timedelta

import pytest

from app import clock
from app.consult import apps, knowledge
from app.consult import service as consult_service
from app.consult.apps.base import AppContext, Manifest
from app.consult.engine import Engine
from app.consult.schemas import AppItem, Segment
from app.db.models import SmartAppProfile
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from tests.conftest import login
from tests.test_consult import say, start

CLINICAL = "http://terminology.hl7.org/CodeSystem/condition-clinical"
ICD10 = "http://hl7.org/fhir/sid/icd-10"
LEGACY = {
    "perguntas", "alertas", "lembretes", "queixas", "emocoes", "resumo", "hipoteses", "anamnese",
    "cirurgia", "medicamentos", "alergias", "historico", "exames", "protocolos", "pedidos",
}  # fmt: skip
TRANSITION = {
    "pendencias_alta", "riscos_passagem", "reconciliacao_consulta", "rede_apoio", "retorno_7_dias",
    "gestacao_lactacao", "rastreios", "vacinas",
}  # fmt: skip


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def context(record, pid, falas=(), kind="consulta", specialty="clinica_medica", extra=None, mutate=None) -> AppContext:
    rec = record(pid)
    if mutate:
        rec.resources = {k: list(v) for k, v in rec.resources.items()}  # o store sintético é da sessão: não vaza
        mutate(rec)
    segs = [Segment(n=i, t=float(i), falante=w, texto=t) for i, (w, t) in enumerate(falas, 1)]  # type: ignore[arg-type]
    eng = Engine(rec, build_timeline(rec), build_context(rec), segs, specialty, kind, clock.now(), extra or {})
    return AppContext(eng.run(), eng.chart, kind, specialty, eng.segments, eng.extra)


def run_app(app_id: str, ctx: AppContext):
    return apps.run_one(apps.discover()[app_id], ctx, None)


# ======================================================================================================
# Critério: GET /smart-apps lista >= 21 apps com manifest válido
# ======================================================================================================
def test_catalog_lists_at_least_21_apps_with_valid_manifest(client, auth):
    res = client.get("/smart-apps", headers=auth("medico"))
    assert res.status_code == 200
    listed = res.json()
    ids = {a["id"] for a in listed}
    assert len(listed) >= 21 and len(ids) == len(listed)
    assert LEGACY | TRANSITION <= ids
    for a in listed:
        m = Manifest.model_validate({k: v for k, v in a.items() if k != "oculto"})
        assert m.versao.count(".") == 2 and m.aba in {
            "vivo",
            "raciocinio",
            "medicamentos",
            "prontuario",
            "pedidos",
            "transicao",
        }
        assert a["oculto"] is False
        apps.validate(m)  # base curada existe, especialidades existem
        assert all(t[0].isupper() for t in m.recursos_fhir)
    base = {a["id"]: a["base"] for a in listed if a["base"]}
    assert {"rastreios", "vacinas", "gestacao_lactacao", "rede_apoio"} <= set(base)
    assert all(b["versao"] and b["fonte"] for b in base.values())
    assert client.get("/smart-apps").status_code in (401, 403)


def test_curated_bases_have_the_declared_columns():
    root = knowledge.get_settings().data_dir / "reference"
    expected = {
        "smart_apps_rastreios.csv": {"id", "rastreio", "sexo", "idade_min", "idade_max", "intervalo_meses", "termos",
                                    "exceto_cid", "orientacao", "fonte"},
        "smart_apps_vacinas.csv": {"id", "vacina", "sexo", "idade_min", "idade_max", "intervalo_meses", "termos",
                                  "orientacao", "fonte"},
        "smart_apps_gestacao_lactacao.csv": {"alvo", "rotulo", "gestacao", "lactacao", "orientacao", "fonte"},
    }  # fmt: skip
    for name, cols in expected.items():
        with open(root / name, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert rows and set(rows[0]) == cols
        assert all(None not in r.values() and r["fonte"] for r in rows), name
    with open(root / "smart_apps_gestacao_lactacao.csv", encoding="utf-8") as fh:
        levels = {"contraindicado", "evitar", "cautela", "compativel"}
        assert all(r["gestacao"] in levels and r["lactacao"] in levels for r in csv.DictReader(fh))


def test_invalid_manifest_is_refused():
    with pytest.raises(ValueError):
        Manifest(id="Ruim Id", nome="Ruim", aba="vivo")
    with pytest.raises(ValueError):
        Manifest(id="tabela_sem_colunas", nome="Tabela", aba="vivo", saida={"card": "tabela"})
    with pytest.raises(ValueError):
        Manifest(id="versao_ruim", nome="Versão ruim", aba="vivo", versao="1.0")
    with pytest.raises(ValueError):
        apps.validate(
            Manifest(
                id="base_ausente",
                nome="Base ausente",
                aba="vivo",
                base={"arquivo": "nao_existe.csv", "versao": "1.0.0", "fonte": "x"},
            )
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        apps.validate(Manifest(id="espec_ruim", nome="Especialidade", aba="vivo", especialidades=["astrologia"]))


# ======================================================================================================
# Critério: um app escondido no perfil continua escondido em outro aparelho
# ======================================================================================================
def test_hidden_app_stays_hidden_on_another_device(client, auth):
    h = auth("medico")
    res = client.put("/smart-apps/perfil", json={"especialidade": "", "ocultos": ["emocoes", "vacinas"]}, headers=h)
    assert res.status_code == 200 and res.json()["ocultos"] == ["emocoes", "vacinas"]
    other_device = login(client, "medico")  # outro aparelho: novo login, sem nenhum estado do navegador
    listed = {a["id"]: a["oculto"] for a in client.get("/smart-apps", headers=other_device).json()}
    assert listed["emocoes"] and listed["vacinas"] and not listed["alertas"]
    # outro usuário não é afetado
    assert not any(a["oculto"] for a in client.get("/smart-apps", headers=auth("enfermagem")).json())
    # desfazer também vale no outro aparelho
    client.put("/smart-apps/perfil", json={"ocultos": ["vacinas"]}, headers=other_device)
    assert client.get("/smart-apps/perfil", headers=h).json()["ocultos"] == ["vacinas"]


def test_profile_by_specialty_falls_back_to_general(client, auth):
    h = auth("medico")
    client.put("/smart-apps/perfil", json={"ocultos": ["resumo"]}, headers=h)
    client.put("/smart-apps/perfil", json={"especialidade": "cardiologia", "ocultos": ["rastreios"]}, headers=h)
    card = client.get("/smart-apps/perfil?especialidade=cardiologia", headers=h).json()
    assert card == {"especialidade": "cardiologia", "ocultos": ["rastreios"], "origem": "especialidade"}
    other = client.get("/smart-apps/perfil?especialidade=clinica_medica", headers=h).json()
    assert other["ocultos"] == ["resumo"] and other["origem"] == "usuario"
    flags = {a["id"]: a["oculto"] for a in client.get("/smart-apps?especialidade=cardiologia", headers=h).json()}
    assert flags["rastreios"] and not flags["resumo"]


def test_profile_rejects_unknown_app_and_specialty(client, auth):
    h = auth("medico")
    assert client.put("/smart-apps/perfil", json={"ocultos": ["nao_existe"]}, headers=h).status_code == 422
    assert (
        client.put("/smart-apps/perfil", json={"especialidade": "astrologia", "ocultos": []}, headers=h).status_code
        == 422
    )
    assert client.get("/smart-apps/perfil?especialidade=astrologia", headers=h).status_code == 422


def test_profile_is_stored_once_per_user_and_specialty(client, auth):
    from app.db import session as db_session

    h = auth("medico")
    for hidden in (["alertas"], ["alertas", "resumo"], []):
        client.put("/smart-apps/perfil", json={"ocultos": hidden}, headers=h)
    with db_session.SessionLocal() as db:
        rows = db.query(SmartAppProfile).all()
    assert len(rows) == 1 and rows[0].ocultos == []


# ======================================================================================================
# Critério: um app de exemplo entra com só um módulo, um CSV e um teste
# ======================================================================================================
EXAMPLE_MODULE = textwrap.dedent(
    '''
    """App de exemplo: um módulo, um CSV e um teste."""
    import csv
    from pathlib import Path

    from app.consult.apps.base import AppContext, BaseCurada, Manifest, Saida
    from app.consult.schemas import AppItem

    MANIFEST = Manifest(
        id="exemplo_tabagismo",
        nome="Exemplo: tabagismo",
        aba="prontuario",
        recursos_fhir=["Condition"],
        base=BaseCurada(arquivo="exemplo_tabagismo.csv", versao="0.1.0", fonte="Exemplo sintético"),
        saida=Saida(card="lista"),
    )


    def run(ctx: AppContext) -> list[AppItem]:
        rows = list(csv.DictReader(open(Path(__file__).with_name("exemplo_tabagismo.csv"), encoding="utf-8")))
        return [
            AppItem(texto=r["orientacao"], severidade="info", refs=[ctx.patient_ref])
            for r in rows
            if r["cid"] == "Z72.0"
        ]
    '''
)


@pytest.fixture
def example_app(tmp_path):
    (tmp_path / "exemplo_tabagismo.csv").write_text(
        "cid,orientacao\nZ72.0,Oferecer apoio para parar de fumar\n", encoding="utf-8"
    )
    module = tmp_path / "exemplo_tabagismo.py"
    module.write_text(EXAMPLE_MODULE, encoding="utf-8")
    app = apps.load_file(module)
    yield app
    apps.unregister("exemplo_tabagismo")


def test_example_app_enters_with_one_module_one_csv(client, auth, example_app):
    h = auth("medico")
    assert "exemplo_tabagismo" in {a["id"] for a in client.get("/smart-apps", headers=h).json()}
    c = start(client, h)
    ins = say(client, h, c["id"], knowledge.demo_scripts()["cen6_consulta"]["falas"][:3])
    card = next(a for a in ins["apps"] if a["id"] == "exemplo_tabagismo")
    assert card["itens"][0]["texto"] == "Oferecer apoio para parar de fumar"
    assert card["itens"][0]["refs"] == ["Patient/cen6"] and card["base_versao"] == "0.1.0"
    # o engine, o schema e a tela não mudaram: o app saiu do catálogo ao ser removido
    apps.unregister("exemplo_tabagismo")
    assert "exemplo_tabagismo" not in {a["id"] for a in client.get("/smart-apps", headers=h).json()}


def test_duplicate_app_id_and_module_without_run_are_refused(example_app, tmp_path):
    clone = tmp_path / "clone.py"
    clone.write_text(
        EXAMPLE_MODULE.replace("def run(", "def run_outro(") + "\n\ndef run(ctx):\n    return []\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicado"):
        apps.load_file(clone)
    no_run = tmp_path / "sem_run.py"
    no_run.write_text(
        EXAMPLE_MODULE.replace("def run(", "def executar(").replace('exemplo_tabagismo"', 'outro_app"', 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="MANIFEST"):
        apps.load_file(no_run)


def test_item_without_valid_ref_is_dropped_and_broken_app_does_not_stop_consult(record):
    ctx = context(record, "cen6")
    valid = {"Patient/cen6", "Observation/cen6-x"}
    bad = Manifest(id="app_refs", nome="App com refs", aba="vivo")
    ok_app = apps.SmartApp(
        bad,
        lambda c: [
            AppItem(texto="bom", refs=["Patient/cen6", "Observation/inventada"]),
            AppItem(texto="sem fonte", refs=["Observation/inventada"]),
            AppItem(texto="sem refs"),
        ],
    )
    res = apps.run_one(ok_app, ctx, valid)
    assert [i.texto for i in res.itens] == ["bom"] and res.itens[0].refs == ["Patient/cen6"]
    assert {d["texto"] for d in res.descartados} == {"sem fonte", "sem refs"}

    def boom(_):
        raise RuntimeError("defeito")

    res = apps.run_one(apps.SmartApp(bad, boom), ctx, valid)
    assert res.erro and res.itens == []


# ======================================================================================================
# Migração dos 15 apps: o registro lê o que o engine produziu
# ======================================================================================================
def test_migrated_apps_mirror_engine_output(client, auth):
    h = auth("medico")
    c = start(client, h)
    ins = say(client, h, c["id"], knowledge.demo_scripts()["cen6_consulta"]["falas"])
    by_id = {a["id"]: a for a in ins["apps"]}
    assert LEGACY <= set(by_id)
    assert len(by_id["perguntas"]["itens"]) == len(ins["perguntas"]) > 0
    assert len(by_id["alertas"]["itens"]) == len(ins["alertas"]) > 0
    assert len(by_id["medicamentos"]["itens"]) == len(ins["medicamentos"])
    assert len(by_id["hipoteses"]["itens"]) == len(ins["hipoteses"])
    assert [i["texto"] for i in by_id["alertas"]["itens"]] == [a["texto"] for a in ins["alertas"]]
    assert by_id["medicamentos"]["colunas"] and all(len(i["celulas"]) == 4 for i in by_id["medicamentos"]["itens"])
    assert all(a["erro"] == "" and a["versao"] for a in ins["apps"])


# ======================================================================================================
# Critério: "Pendências para a alta" aparece na visita de um paciente internado
# ======================================================================================================
def test_discharge_pending_shows_in_inpatient_visit_only(client, auth):
    h = auth("medico")
    visit = start(client, h, "cen1", tipo="visita")
    ins = say(client, h, visit["id"], knowledge.demo_scripts()["cen1_visita"]["falas"][:4])
    card = next(a for a in ins["apps"] if a["id"] == "pendencias_alta")
    assert card["aba"] == "transicao" and card["itens"]
    assert any("Reconciliação medicamentosa" in i["texto"] for i in card["itens"])
    assert all(i["refs"] for i in card["itens"])
    assert "riscos_passagem" in {a["id"] for a in ins["apps"]}
    # na consulta ambulatorial (sem internação) não aparece
    amb = start(client, h, "cen6")
    ins6 = say(client, h, amb["id"], knowledge.demo_scripts()["cen6_consulta"]["falas"][:3])
    assert not {"pendencias_alta", "riscos_passagem"} & {a["id"] for a in ins6["apps"]}


def test_discharge_pending_lists_pending_exams_and_clears_with_medrec(record):
    ctx = context(record, "cen1", kind="visita")
    texts = [i.texto for i in run_app("pendencias_alta", ctx).itens]
    assert any("Reconciliação" in t for t in texts)
    done = context(record, "cen1", kind="visita", extra={"medrec_runs": ["2026-01-01T10:00:00+00:00"]})
    assert not any("Reconciliação" in i.texto for i in run_app("pendencias_alta", done).itens)

    def pending_xray(rec):
        rec.resources.setdefault("ServiceRequest", []).append(
            {
                "resourceType": "ServiceRequest", "id": "rx1", "status": "active", "intent": "order",
                "category": [{"coding": [{"system": "http://snomed.info/sct", "code": "363679005"}]}],
                "code": {"text": "RX de tórax de controle"}, "authoredOn": (rec.now - timedelta(days=1)).isoformat(),
                "subject": {"reference": "Patient/cen1"},
            }
        )  # fmt: skip

    exam = [
        i
        for i in run_app("pendencias_alta", context(record, "cen1", kind="visita", mutate=pending_xray)).itens
        if "RX" in i.texto
    ]
    assert exam and exam[0].refs == ["ServiceRequest/rx1"] and exam[0].detalhe.startswith("Pedido")


def test_handoff_risks_use_scales_and_critical_alerts(record):
    ctx = context(record, "cen1", kind="visita")
    items = run_app("riscos_passagem", ctx).itens
    assert any("queda" in i.texto and i.refs[0].startswith("Observation/") for i in items)
    assert [i.severidade for i in items] == sorted(
        (i.severidade for i in items), key=["critico", "atencao", "info", "ok"].index
    )


# ======================================================================================================
# Apps de transição e de prevenção
# ======================================================================================================
def test_reconciliation_in_consult_flags_reported_not_registered(record):
    ctx = context(record, "cen6", [("paciente", "Doutora, tomei um ibuprofeno ontem por causa da dor.")])
    rows = {i.celulas[0]: i for i in run_app("reconciliacao_consulta", ctx).itens}
    assert (
        rows["Ibuprofeno"].celulas[3] == "Dito na conversa, sem registro" and rows["Ibuprofeno"].severidade == "atencao"
    )
    assert rows["Ibuprofeno"].refs == ["Fala/1"]
    assert rows["Metformina"].celulas[3] == "Registrado e em uso"
    assert next(iter(rows)) == "Ibuprofeno"  # o que diverge vem primeiro


def test_support_network_uses_social_weights(record):
    from app.reference import social_weights

    ctx = context(record, "cen6")
    items = run_app("rede_apoio", ctx).itens
    assert items[0].texto.startswith("Risco social") and any(i.texto == "Sem cuidador" for i in items)
    assert "Sem cuidador" == social_weights()["itens"]["tem_cuidador"]["rotulo"]
    assert all(i.refs for i in items)


def test_seven_day_return_window(record):
    inside = run_app("retorno_7_dias", context(record, "cen7")).itens  # alta há 4 dias
    assert (
        "dentro da janela" in inside[1].texto
        and inside[1].severidade == "info"
        and inside[1].refs[0].startswith("Encounter/")
    )
    outside = run_app("retorno_7_dias", context(record, "cen7b")).itens  # alta há 12 dias
    assert "fora da janela" in outside[1].texto and outside[1].severidade == "atencao"
    assert run_app("retorno_7_dias", context(record, "cen6")) is None  # nunca internada
    assert run_app("retorno_7_dias", context(record, "cen1")) is None or "retorno_7_dias" not in {
        a.id for a in apps.run_all(context(record, "cen1", kind="visita"))
    }


def test_screenings_by_age_and_sex(record):
    woman = {i.celulas[0]: i for i in run_app("rastreios", context(record, "cen6")).itens}  # F, 60 anos, diabética
    assert "Mamografia" in woman and "Citopatológico do colo do útero" in woman
    assert "Glicemia ou hemoglobina glicada" not in woman  # já tem diabetes: é protocolo, não rastreio
    assert woman["Mamografia"].celulas[1] == "Sem registro"
    man = {i.celulas[0] for i in run_app("rastreios", context(record, "cen7")).itens}  # M, 79 anos
    assert "Mamografia" not in man and "Citopatológico do colo do útero" not in man
    young = context(record, "cen4")  # M, 24 anos
    assert {i.celulas[0] for i in run_app("rastreios", young).itens} <= {"Aferição da pressão arterial"}


def test_screening_in_date_when_chart_has_a_record(record):
    def add_mammogram(rec):
        rec.resources.setdefault("Procedure", []).append(
            {
                "resourceType": "Procedure", "id": "mamo1", "status": "completed",
                "code": {"text": "Mamografia bilateral"},
                "performedDateTime": (rec.now - timedelta(days=200)).isoformat(),
                "subject": {"reference": "Patient/cen6"},
            }
        )  # fmt: skip

    ctx = context(record, "cen6", mutate=add_mammogram)
    row = next(i for i in run_app("rastreios", ctx).itens if i.celulas[0] == "Mamografia")
    assert row.celulas[1] == "Em dia" and row.refs == ["Procedure/mamo1"] and row.severidade == "ok"

    def old_mammogram(rec):
        add_mammogram(rec)
        rec.resources["Procedure"][-1]["performedDateTime"] = (rec.now - timedelta(days=900)).isoformat()

    row = next(
        i
        for i in run_app("rastreios", context(record, "cen6", mutate=old_mammogram)).itens
        if i.celulas[0] == "Mamografia"
    )
    assert row.celulas[1] == "Vencido" and row.severidade == "atencao"


def test_vaccines_by_age_and_registered_doses(record):
    ctx = context(record, "cen6")
    rows = {i.celulas[0]: i for i in run_app("vacinas", ctx).itens}
    assert rows["Influenza (gripe)"].celulas[1] == "Sem registro" and "HPV" not in rows

    def add_flu(rec):
        rec.resources.setdefault("Immunization", []).append(
            {
                "resourceType": "Immunization", "id": "flu1", "status": "completed",
                "vaccineCode": {"text": "Vacina influenza trivalente"},
                "occurrenceDateTime": (rec.now - timedelta(days=100)).isoformat(),
                "patient": {"reference": "Patient/cen6"},
            }
        )  # fmt: skip

    row = next(
        i
        for i in run_app("vacinas", context(record, "cen6", mutate=add_flu)).itens
        if i.celulas[0].startswith("Influenza")
    )
    assert row.celulas[1] == "Em dia" and row.refs == ["Immunization/flu1"]


def pregnancy_items(record, falas, pid="cen6", mutate=None):
    res = run_app("gestacao_lactacao", context(record, pid, falas, mutate=mutate))
    return None if res is None else res


def test_pregnancy_and_lactation_checks_medicines(record):
    res = pregnancy_items(record, [("paciente", "Doutora, descobri que estou grávida de 8 semanas.")])
    rows = {i.celulas[0]: i for i in res.itens[1:]}
    assert res.itens[0].texto == "Paciente em gestação" and res.itens[0].refs == ["Fala/1"]
    assert rows["Losartana"].severidade == "critico" and rows["Losartana"].celulas[1] == "Contraindicado"
    assert rows["Sinvastatina"].severidade == "atencao"
    assert all(i.refs[0].startswith("MedicationRequest/") for i in res.itens[1:])
    lact = pregnancy_items(record, [("paciente", "Estou amamentando, dou mama de manhã e à noite.")])
    assert lact.itens[0].texto == "Paciente em lactação"
    assert {i.celulas[0]: i.severidade for i in lact.itens[1:]}["Metformina"] == "ok"


def test_pregnancy_not_flagged_by_denial_question_or_wrong_patient(record):
    assert pregnancy_items(record, [("paciente", "Não, grávida eu não estou.")]) is None
    assert pregnancy_items(record, [("medico", "A senhora está grávida?")]) is None
    assert pregnancy_items(record, [("paciente", "Minha filha está grávida e eu vim acompanhar.")]) is None
    assert pregnancy_items(record, [("paciente", "Minha neta está amamentando e eu ajudo em casa.")]) is None
    assert pregnancy_items(record, [("paciente", "Estou com dor de cabeça.")]) is None
    assert run_app("gestacao_lactacao", context(record, "cen6")) is None
    assert "gestacao_lactacao" not in {
        a.id for a in apps.run_all(context(record, "cen7", [("paciente", "Estou grávida.")]))
    }


def test_pregnancy_from_active_condition(record):
    def pregnant(rec):
        rec.resources.setdefault("Condition", []).append(
            {
                "resourceType": "Condition", "id": "gest1",
                "clinicalStatus": {"coding": [{"system": CLINICAL, "code": "active"}]},
                "code": {"text": "Supervisão de gravidez normal", "coding": [{"system": ICD10, "code": "Z34"}]},
                "subject": {"reference": "Patient/cen6"},
            }
        )  # fmt: skip

    res = pregnancy_items(record, [], mutate=pregnant)
    assert res is not None and res.itens[0].refs == ["Condition/gest1"]


# entradas adversariais da verificação (negação, passado, dúvida, outro falante, sem acento)
PREGNANCY_ADVERSARIAL = [
    ("paciente", "Estou gravida de dois meses", True),
    ("paciente", "O teste de gravidez deu negativo", False),
    ("paciente", "Já fui grávida duas vezes, mas hoje não", False),
    ("paciente", "Parei de amamentar mês passado", False),
    ("paciente", "Não estou amamentando", False),
    ("paciente", "Quero engravidar ano que vem", False),
    ("paciente", "Será que estou grávida?", False),
    ("paciente", "estou gestante e amamentando", True),
    ("acompanhante", "Ela está grávida", True),
    ("medico", "Você está grávida, certo?", False),
    ("paciente", "Minha cunhada esta gravida", False),
    ("paciente", "Nunca engravidei", False),
    ("paciente", "Estou dando mama pro neném", True),
]


@pytest.mark.parametrize(("falante", "texto", "esperado"), PREGNANCY_ADVERSARIAL)
def test_pregnancy_adversarial_utterances(record, falante, texto, esperado):
    assert (pregnancy_items(record, [(falante, texto)]) is not None) == esperado
