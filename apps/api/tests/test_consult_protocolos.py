"""Protocolos de APS dos 6 eixos, vivos na conversa e com fonte oficial (#28).

Critérios de aceite exercitados pelo Engine sobre cópias do prontuário do cen6 (idade e sexo trocados) e pelo caminho
da API; a base YAML tem teste de esquema e a sobrescrita local é lida de uma pasta temporária.
"""

from copy import deepcopy
from datetime import timedelta

import pytest
import yaml

from app import clock
from app.config import get_settings
from app.consult import knowledge
from app.consult import service as consult_service
from app.consult.engine import Engine
from app.consult.schemas import Segment
from app.timeline.context import build_context
from app.timeline.events import build_timeline


@pytest.fixture(autouse=True)
def _fresh():
    consult_service.clear_chart_cache()
    knowledge.protocols.cache_clear()
    yield
    knowledge.protocols.cache_clear()
    consult_service.clear_chart_cache()


@pytest.fixture
def record():
    """Prontuário recém-semeado: outros testes gravam no store da sessão (ex.: uma evolução com exame dos pés)."""
    from app.fhir_client.client import LocalFhirStore
    from app.seed.scenarios import build_demo_resources
    from app.timeline.record import PatientRecord
    from tests.conftest import ANCHOR

    fresh = LocalFhirStore.from_resources(build_demo_resources(anchor=ANCHOR))

    def _load(pid: str):
        rec = PatientRecord.load(fresh, pid)
        assert rec is not None
        rec.now = ANCHOR
        return rec

    return _load


def engine(record, falas, birth=None, gender=None, extra=None, patient="cen6", conditions=None) -> Engine:
    rec = record(patient)
    rec.patient = deepcopy(rec.patient)
    if birth:
        rec.patient["birthDate"] = birth
    if gender:
        rec.patient["gender"] = gender
    segs = [Segment(n=i, t=float(i), falante=who, texto=t) for i, (who, t) in enumerate(falas, 1)]  # type: ignore[arg-type]
    return Engine(rec, build_timeline(rec), build_context(rec), segs, "clinica_geral", "consulta", clock.now(), extra)


def items(eng: Engine, **filt):
    return [p for p in eng.run().protocolos if all(getattr(p, k) == v for k, v in filt.items())]


def years_ago(n: int, extra_days: int = 0) -> str:
    today = clock.now().date()
    return today.replace(year=today.year - n).isoformat() if not extra_days else today.isoformat()


def months_ago(n: int) -> str:
    today = clock.now().date()
    total = today.year * 12 + today.month - 1 - n
    return f"{total // 12:04d}-{total % 12 + 1:02d}-01"


# ---------- base YAML: esquema ----------
def test_base_has_six_axes_and_every_item_cites_a_full_source():
    defs = knowledge.protocols()
    assert {d.axis for d in defs} == set(knowledge.PROTOCOL_AXES)
    assert len({d.id for d in defs}) == len(defs)
    for d in defs:
        assert d.citation.count(",") >= 3 and str(d.interval_days).isdigit(), d.id
        assert d.source.partition(":")[0] in ("lab", "texto", "vacina", "vital", "escala", "nenhum"), d.id
        assert d.said or d.critical_triggers or d.source.startswith("vital"), d.id
        assert not (d.age_min is not None and d.age_max is not None and d.age_min > d.age_max), d.id


def test_the_13_chronic_items_were_migrated_with_source():
    chronic = [d for d in knowledge.protocols() if d.axis == "Adulto crônico"]
    assert len(chronic) == 13
    assert {d.protocol for d in chronic} == {"Diabetes tipo 2", "Hipertensão", "Doença renal crônica"}
    assert all("Ministério da Saúde" in d.citation or "Sociedade" in d.citation for d in chronic)


def test_schema_rejects_item_without_source_year(tmp_path):
    (tmp_path / "x.yaml").write_text(
        yaml.safe_dump(
            {
                "eixo": "Mulher",
                "fontes": {"f": {"documento": "Doc", "orgao": "Órgão", "pagina": "p. 1"}},
                "itens": [
                    {
                        "id": "x",
                        "protocolo": "P",
                        "item": "I",
                        "fonte": "f",
                        "registro": "nenhum",
                        "intervalo_dias": 1,
                        "orientacao": "o",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="sem documento, órgão, ano e página"):
        knowledge.load_protocols(tmp_path)


# ---------- critérios de aceite ----------
def test_woman_of_30_gets_cervical_screening_with_inca_source(record):
    eng = engine(record, [("medico", "Bom dia, vamos conversar.")], birth=years_ago(30), gender="female")
    colo = items(eng, id="mulher.colo")
    assert len(colo) == 1 and colo[0].eixo == "Mulher" and colo[0].status == "sem_registro"
    assert "INCA" in colo[0].fonte and "2016" in colo[0].fonte
    assert not items(eng, id="mulher.mamografia")  # mamografia é dos 50 aos 69


def test_cervical_and_mammography_age_windows(record):
    older = engine(record, [("medico", "Bom dia.")], birth=years_ago(55), gender="female")
    ids = {p.id for p in items(older)}
    assert "mulher.colo" in ids and "mulher.mamografia" in ids
    young = engine(record, [("medico", "Bom dia.")], birth=years_ago(22), gender="female")
    assert "mulher.colo" not in {p.id for p in items(young)}
    man = engine(record, [("medico", "Bom dia.")], birth=years_ago(40), gender="male")
    assert not [p for p in items(man) if p.eixo == "Mulher"]


def test_child_of_6_months_has_vaccine_items(record):
    eng = engine(record, [("medico", "Bom dia, mãe.")], birth=months_ago(6), gender="male")
    vac = [p for p in items(eng) if p.protocolo == "Calendário vacinal do PNI"]
    ids = {p.id for p in vac}
    assert {"crianca.vacina.6m", "crianca.vacina.influenza"} <= ids
    assert "crianca.vacina.2m" not in ids and all(p.eixo == "Criança" and "PNI" in p.fonte for p in vac)
    assert {"crianca.puericultura", "crianca.crescimento"} <= {p.id for p in items(eng)}
    assert not [p for p in items(eng) if p.eixo in ("Mulher", "Idoso")]


def test_spoken_foot_exam_marks_item_as_addressed_with_fala(record):
    falas = [("medico", "Como o senhor está?"), ("medico", "Examinei os pés agora, com o monofilamento.")]
    eng = engine(record, falas)
    pe = items(eng, id="cronico.dm2.pes")[0]
    assert pe.status == "abordado" and pe.ultimo == "abordado — Fala/2" and "Fala/2" in pe.refs
    assert "Fala/2" in str(eng.run().perguntas_abordadas)  # entra nas perguntas abordadas com a fala
    # sem a fala, o item continua pendente e a pergunta é sugerida
    quiet = engine(record, [("medico", "Como o senhor está?")])
    assert items(quiet, id="cronico.dm2.pes")[0].status != "abordado"
    assert items(quiet, id="cronico.dm2.pes")[0].pergunta.startswith("Posso examinar os seus pés")


def test_foot_exam_not_addressed_when_negated_or_asked(record):
    for texto in ("Não examinei os pés hoje.", "O senhor já examinou os pés?", "Vamos conversar sobre outra coisa."):
        eng = engine(record, [("medico", texto)])
        assert items(eng, id="cronico.dm2.pes")[0].status != "abordado", texto


def test_addressed_item_leaves_the_note_pendencies(record):
    quiet = engine(record, [("medico", "Bom dia.")])
    pend = lambda e: [p.item for p in e.run().protocolos if p.status in ("pendente", "sem_registro")]  # noqa: E731
    said = engine(record, [("medico", "Examinei os pés agora.")])
    assert "Exame dos pés (monofilamento)" in pend(quiet)
    assert "Exame dos pés (monofilamento)" not in pend(said)


def test_phq9_item9_positive_from_scale_is_critical(record):
    extra = {"escalas": {"phq9": {"respostas": {"ideacao": "1"}}}}
    eng = engine(record, [("paciente", "Estou muito triste, sem ânimo.")], extra=extra)
    ins = eng.run()
    risk = [p for p in ins.protocolos if p.id == "mental.suicidio"][0]
    assert risk.status == "critico" and risk.eixo == "Saúde mental" and "Kroenke" in risk.fonte
    crit = [a for a in ins.alertas if a.categoria == "protocolo_critico"]
    assert crit and crit[0].severidade == "critico" and ins.alertas[0].severidade == "critico"


def test_suicidal_speech_is_critical_and_negation_is_not(record):
    eng = engine(record, [("paciente", "Doutora, às vezes eu penso em me matar.")])
    ins = eng.run()
    risk = [p for p in ins.protocolos if p.id == "mental.suicidio"][0]
    assert risk.status == "critico" and risk.refs[0] == "Fala/1"
    assert any(a.categoria == "protocolo_critico" and "Fala/1" in a.refs for a in ins.alertas)
    for texto in (
        "Nunca pensei em me matar, doutora.",
        "A senhora pensa em se matar?",
        "Estou triste mas não penso em me matar.",
    ):
        ins = engine(record, [("paciente", texto)]).run()
        assert not [a for a in ins.alertas if a.categoria == "protocolo_critico"], texto
    ins = engine(record, [("paciente", "Eu não quero mais viver.")]).run()
    assert [a for a in ins.alertas if a.categoria == "protocolo_critico"]


def test_phq9_item9_zero_is_not_critical(record):
    extra = {"escalas": {"phq9": {"respostas": {"ideacao": "0"}}}}
    ins = engine(record, [("paciente", "Estou muito triste.")], extra=extra).run()
    assert not [a for a in ins.alertas if a.categoria == "protocolo_critico"]


def test_local_file_overrides_an_interval(record, tmp_path, monkeypatch):
    base = {d.id: d for d in knowledge.protocols()}["mulher.colo"]
    (tmp_path / "municipio.yaml").write_text(
        yaml.safe_dump({"eixo": "Mulher", "itens": [{"id": "mulher.colo", "intervalo_dias": 365}]}), encoding="utf-8"
    )
    monkeypatch.setenv("PROTOCOLOS_LOCAIS_DIR", str(tmp_path))
    get_settings.cache_clear()
    knowledge.protocols.cache_clear()
    try:
        eng = engine(record, [("medico", "Bom dia.")], birth=years_ago(30), gender="female")
        colo = items(eng, id="mulher.colo")[0]
        assert colo.intervalo_dias == 365 and base.interval_days == 1095 and colo.local is True
        assert colo.fonte.startswith("INCA") or "INCA" in colo.fonte  # a fonte do item base continua
    finally:
        monkeypatch.delenv("PROTOCOLOS_LOCAIS_DIR")
        get_settings.cache_clear()


def test_local_file_can_disable_and_add_items(tmp_path):
    (tmp_path / "x.yaml").write_text(
        yaml.safe_dump(
            {
                "eixo": "Mulher",
                "fontes": {"m": {"documento": "Protocolo municipal", "orgao": "SMS", "ano": 2025, "pagina": "p. 3"}},
                "itens": [
                    {"id": "mulher.mamografia", "ativo": False},
                    {
                        "id": "mulher.local",
                        "protocolo": "Municipal",
                        "item": "Item municipal",
                        "fonte": "m",
                        "registro": "nenhum",
                        "intervalo_dias": 30,
                        "orientacao": "o",
                        "fala": ["municipal"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    defs = {d.id: d for d in knowledge.load_protocols(knowledge.protocols_dir(), tmp_path)}
    assert "mulher.mamografia" not in defs and defs["mulher.local"].local and "SMS" in defs["mulher.local"].citation


# ---------- outros eixos ----------
def test_elderly_gets_ivcf_falls_vaccines_and_beers(record):
    eng = engine(record, [("medico", "Bom dia.")], birth=years_ago(72))
    ids = {p.id for p in items(eng, eixo="Idoso")}
    assert ids == {"idoso.ivcf20", "idoso.quedas", "idoso.vacinas", "idoso.beers"}
    assert not [p for p in items(engine(record, [("medico", "Bom dia.")], birth=years_ago(40))) if p.eixo == "Idoso"]


def test_prenatal_follows_the_trimester_said_in_the_visit(record, store):
    rec = record("cen6")
    rec.patient = deepcopy(rec.patient) | {"birthDate": years_ago(28), "gender": "female"}
    rec.resources = {**rec.resources}
    conds = [dict(c) for c in rec.resources["Condition"]]
    conds.append(
        {
            "resourceType": "Condition",
            "id": "gest1",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {
                "text": "Gestação supervisionada",
                "coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "Z34.9"}],
            },
        }
    )
    rec.resources["Condition"] = conds
    segs = [Segment(n=1, t=1.0, falante="paciente", texto="Estou grávida, com 30 semanas.")]
    eng = Engine(rec, build_timeline(rec), build_context(rec), segs, "obstetricia", "consulta", clock.now())
    ids = {p.id for p in eng.run().protocolos if p.protocolo == "Pré-natal"}
    assert (
        "mulher.prenatal.gbs" in ids and "mulher.prenatal.sorologias" not in ids and "mulher.prenatal.consulta" in ids
    )


def test_mental_health_screening_enters_by_conversation_only(record):
    calm = engine(record, [("paciente", "Estou bem, só vim buscar a receita.")])
    assert not items(calm, eixo="Saúde mental")
    sad = engine(record, [("paciente", "Ando muito triste e sem ânimo.")])
    assert "mental.suicidio" in {p.id for p in items(sad, eixo="Saúde mental")}


def test_teleconsulting_item_appears_when_referral_is_discussed(record):
    eng = engine(record, [("medico", "Vou encaminhar para o cardiologista.")])
    tele = items(eng, eixo="Telessaúde")
    assert tele and tele[0].status in ("pendente", "sem_registro") and "Portaria" in tele[0].fonte
    said = engine(record, [("medico", "Vou pedir uma teleconsultoria com o cardiologista.")])
    assert items(said, eixo="Telessaúde")[0].status == "abordado"


def test_protocol_questions_follow_the_conversation(record):
    eng = engine(record, [("medico", "Examinei os pés agora.")])
    ins = eng.run()
    pend = [q for q in ins.perguntas if q.id.startswith("protocolo:")]
    assert pend and all(q.fonte and q.prioridade in ("alta", "media") for q in pend)
    assert any(q.prioridade == "alta" for q in pend)  # o mesmo protocolo que acabou de ser tratado puxa a próxima


def test_api_exposes_axis_source_and_alert_through_the_real_path(client, auth):
    from tests.test_consult import say, start

    h = auth("medico")
    cid = start(client, h)["id"]
    falas = [
        {"falante": "medico", "texto": "Examinei os pés agora.", "t": 1.0},
        {"falante": "paciente", "texto": "Doutor, às vezes penso em me matar.", "t": 2.0},
    ]
    ins = say(client, h, cid, falas)
    pe = next(p for p in ins["protocolos"] if p["id"] == "cronico.dm2.pes")
    assert pe["eixo"] == "Adulto crônico" and "Ministério da Saúde" in pe["fonte"]
    assert pe["status"] in ("abordado", "em_dia")  # em_dia se outro teste já gravou o exame dos pés no store
    assert pe["status"] == "em_dia" or pe["ultimo"] == "abordado — Fala/1"
    assert any(a["categoria"] == "protocolo_critico" and "Fala/2" in a["refs"] for a in ins["alertas"])
    smart = client.get(f"/consultations/{cid}", headers=h).json()["insights"]
    assert any(p["status"] == "critico" for p in smart["protocolos"])


# ---------- verificação cética: segurança clínica (risco de suicídio, arquivo local, idade, fala) ----------
def risk(ins):
    return [a for a in ins.alertas if a.categoria == "protocolo_critico"]


@pytest.mark.parametrize(
    "texto",
    [
        "Não penso em me matar, doutora.",
        "Não tenho vontade de me matar.",
        "Nunca quis me matar, graças a Deus.",
        "Eu não quero morrer, só estou cansada.",
        "Meu irmão pensou em suicídio no ano passado.",
        "A vizinha tentou suicídio semana passada.",
        "Meu pai se matou quando eu era criança.",
        "Não aguento mais essa dor.",
    ],
)
def test_risk_not_raised_by_negation_or_by_a_relative(record, texto):
    assert not risk(engine(record, [("paciente", texto)]).run()), texto


@pytest.mark.parametrize(
    "texto",
    [
        "as vezes eu penso em me matar",
        "Meu irmão se matou e eu penso em me matar também.",
        "Meu irmão pensou em suicídio e eu também.",
        "Não aguento mais viver assim.",
        "Às vezes acho que seria melhor estar morta.",
    ],
)
def test_risk_raised_even_without_accents_or_next_to_a_relative(record, texto):
    assert risk(engine(record, [("paciente", texto)]).run()), texto


def test_acompanhante_report_about_the_patient_is_still_risk(record):
    # o filho (acompanhante) fala da paciente: "minha mãe disse que quer se matar" não é relato de terceiro
    assert risk(engine(record, [("acompanhante", "Doutora, minha mãe disse que quer se matar.")]).run())


def test_doctor_question_alone_is_not_risk_but_short_yes_is(record):
    for q in ("A senhora pensa em se machucar?", "A senhora pensa em se matar", "Tem ideação suicida?"):
        assert not risk(engine(record, [("medico", q)]).run()), q
    for q, a in (
        ("A senhora tem pensamentos de se matar?", "Sim."),
        ("A senhora pensa em se machucar?", "Sim, às vezes."),
        ("Tem pensado em suicídio?", "Tenho pensado, sim."),
        ("Nos últimos dias pensou em se ferir?", "Às vezes."),
    ):
        ins = engine(record, [("medico", q), ("paciente", a)]).run()
        alert = risk(ins)
        assert alert and alert[0].refs[:2] == ["Fala/2", "Fala/1"], (q, a)


def test_doctor_question_answered_no_or_unrelated_yes_is_not_risk(record):
    for falas in (
        [("medico", "A senhora pensa em se matar?"), ("paciente", "Não.")],
        [("medico", "A senhora pensa em se matar?"), ("paciente", "Nunca pensei nisso.")],
        [("medico", "A senhora pensa em se matar?"), ("paciente", "Hum, não sei.")],
        [("medico", "Bom dia."), ("paciente", "Sim.")],
        [("medico", "O senhor tem medo de morrer?"), ("paciente", "Tenho sim.")],
        [("paciente", "Sim."), ("medico", "A senhora pensa em se matar?")],
    ):
        assert not risk(engine(record, falas).run()), falas


def test_phq9_item9_absent_does_not_cover_the_suicide_item_but_zero_does(record):
    def mental(respostas):
        ins = engine(
            record, [("paciente", "Estou muito triste.")], extra={"escalas": {"phq9": {"respostas": respostas}}}
        ).run()
        return {p.id: p.status for p in ins.protocolos}, risk(ins)

    absent, alerts = mental({"humor": "2", "interesse": "1"})
    assert absent["mental.suicidio"] != "abordado" and absent["mental.phq9"] == "abordado" and not alerts
    zero, alerts = mental({"ideacao": 0})
    assert zero["mental.suicidio"] == "abordado" and not alerts
    for blank in ("", None):
        status, alerts = mental({"ideacao": blank})
        assert status["mental.suicidio"] != "abordado" and not alerts
    positive, alerts = mental({"ideacao": "2"})
    assert positive["mental.suicidio"] == "critico" and alerts


def test_malformed_saved_scale_does_not_break_the_consultation(record):
    for escalas in ({"phq9": None}, {"phq9": {"respostas": [1]}}, {"phq9": {"respostas": None}}):
        engine(record, [("paciente", "Estou muito triste.")], extra={"escalas": escalas}).run()


def test_age_boundaries_and_unknown_age_or_sex(record):
    today = clock.now().date()

    def born(years, days_late=0):
        return (today.replace(year=today.year - years) + timedelta(days=days_late)).isoformat()

    def has(id_, **kw):
        return id_ in {p.id for p in items(engine(record, [("medico", "Bom dia.")], **kw))}

    assert not has("mulher.colo", birth=born(25, 1), gender="female") and has(
        "mulher.colo", birth=born(25), gender="female"
    )
    assert has("mulher.colo", birth=born(65, 1), gender="female") and not has(
        "mulher.colo", birth=born(65), gender="female"
    )
    assert not has("mulher.mamografia", birth=born(50, 1), gender="female") and has(
        "mulher.mamografia", birth=born(50), gender="female"
    )
    assert has("mulher.mamografia", birth=born(70, 1), gender="female") and not has(
        "mulher.mamografia", birth=born(70), gender="female"
    )
    assert not has("idoso.ivcf20", birth=born(60, 1)) and has("idoso.ivcf20", birth=born(60))
    for sex in ("male", "other", "unknown"):
        assert not [
            p
            for p in items(engine(record, [("medico", "Fez o papanicolau?")], birth=born(40), gender=sex))
            if p.eixo == "Mulher"
        ], sex
    # sem data de nascimento nenhum item por faixa de idade vale
    rec = record("cen6")
    rec.patient = {k: v for k, v in rec.patient.items() if k != "birthDate"}
    segs = [Segment(n=1, t=1.0, falante="medico", texto="Bom dia.")]
    ids = {
        p.id
        for p in Engine(rec, build_timeline(rec), build_context(rec), segs, "clinica_geral", "consulta", clock.now())
        .run()
        .protocolos
    }
    assert not ids & {"mulher.colo", "mulher.mamografia", "idoso.ivcf20", "idoso.beers"}


@pytest.mark.parametrize(
    "texto",
    [
        "Não examinei os pés.",
        "Ainda não examinei os pés hoje.",
        "Deixei de examinar os pés.",
        "Não deu tempo de examinar os pés, vou fazer na próxima consulta.",
        "Preciso examinar os pés depois.",
        "O senhor examina os pés todo dia?",
        "Nao examinei os pes",
    ],
)
def test_foot_exam_not_addressed_by_omission_postponement_or_question(record, texto):
    assert items(engine(record, [("medico", texto)]), id="cronico.dm2.pes")[0].status != "abordado", texto


def local_dir(tmp_path, monkeypatch, files):
    for name, content in files.items():
        (tmp_path / name).write_text(
            content if isinstance(content, str) else yaml.safe_dump(content, allow_unicode=True), encoding="utf-8"
        )
    monkeypatch.setenv("PROTOCOLOS_LOCAIS_DIR", str(tmp_path))
    get_settings.cache_clear()
    knowledge.protocols.cache_clear()


@pytest.fixture
def _restore_settings():
    yield
    get_settings.cache_clear()
    knowledge.protocols.cache_clear()


def test_malformed_or_unknown_local_files_never_break_or_weaken_the_base(
    record, tmp_path, monkeypatch, _restore_settings
):
    base = {d.id: d for d in knowledge.protocols()}
    local_dir(
        tmp_path,
        monkeypatch,
        {
            "a_quebrado.yaml": "eixo: [Mulher\n  itens: : :",
            "b_lista.yaml": "- 1\n- 2",
            "c_itens.yaml": {
                "eixo": "Mulher",
                "itens": ["x", {"intervalo_dias": 10}, {"id": "nao.existe", "intervalo_dias": 10}],
            },
            "d_tipos.yaml": {
                "eixo": "Mulher",
                "itens": [{"id": "mulher.colo", "intervalo_dias": "abc"}, {"id": "mulher.mamografia", "idade_min": -3}],
            },
            "e_critico.yaml": {
                "eixo": "Saúde mental",
                "itens": [
                    {
                        "id": "mental.suicidio",
                        "ativo": False,
                        "critico": False,
                        "gatilhos_criticos": [],
                        "idade_min": 99,
                        "sexo": "M",
                    },
                    {"id": "mental.phq9", "ativo": False},
                ],
            },
        },
    )
    got = {d.id: d for d in knowledge.protocols()}
    assert "mental.phq9" not in got  # não crítico: o local pode desligar
    assert set(base) - set(got) == {"mental.phq9"} and set(got) == set(base) - {"mental.phq9"}
    s = got["mental.suicidio"]
    assert (
        s.critical
        and s.critical_triggers == base["mental.suicidio"].critical_triggers
        and s.age_min is None
        and not s.sex
        and not s.local
    )
    assert (
        got["mulher.colo"].interval_days == base["mulher.colo"].interval_days and got["mulher.mamografia"].age_min == 50
    )
    assert risk(engine(record, [("paciente", "Eu penso em me matar.")]).run())  # o alerta continua de pé


def test_local_file_changes_only_text_and_interval_of_a_critical_item(tmp_path, monkeypatch, _restore_settings):
    local_dir(
        tmp_path,
        monkeypatch,
        {
            "x.yaml": {
                "eixo": "Saúde mental",
                "itens": [{"id": "mental.suicidio", "intervalo_dias": 30, "pergunta": "Pergunta local?"}],
            }
        },
    )
    s = {d.id: d for d in knowledge.protocols()}["mental.suicidio"]
    assert s.interval_days == 30 and s.question == "Pergunta local?" and s.critical and s.local


def test_every_source_needs_document_organ_year_and_page(tmp_path):
    def load(fonte):
        (tmp_path / "t.yaml").write_text(
            yaml.safe_dump(
                {
                    "eixo": "Mulher",
                    "fontes": {"x": fonte},
                    "itens": [
                        {
                            "id": "t.1",
                            "protocolo": "p",
                            "item": "i",
                            "fonte": "x",
                            "registro": "nenhum",
                            "intervalo_dias": 1,
                            "orientacao": "o",
                        }
                    ],
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        return knowledge.load_protocols(tmp_path)

    ok = {"documento": "d", "orgao": "o", "ano": 2020, "pagina": "p. 1"}
    assert load(ok)[0].citation == "o, d, 2020, p. 1"
    for broken in (
        {**ok, "pagina": ""},
        {**ok, "pagina": "  "},
        {k: v for k, v in ok.items() if k != "pagina"},
        {**ok, "orgao": ""},
        {**ok, "documento": ""},
        {**ok, "ano": 0},
        {**ok, "ano": "abc"},
        None,
    ):
        with pytest.raises(ValueError, match="fonte 'x'"):
            load(broken)
