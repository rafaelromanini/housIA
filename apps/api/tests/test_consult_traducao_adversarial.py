"""Tradução (#22), verificação adversarial: negação es/en, "no" em português, alergia em outro idioma, fidelidade."""

import pytest

from app.consult import ai as consult_ai
from app.consult import fidelidade, idiomas
from tests.test_consult import start
from tests.test_consult_traducao import falas, tag


def queixa(client, auth, texto, idioma, rotulo="Dor no peito", paciente="cen8"):
    h = auth("medico")
    cid = start(client, h, paciente)["id"]
    ins = falas(client, h, cid, [{"falante": "paciente", "texto": texto, "t": 1, "idioma": idioma}])["insights"]
    t = tag(ins, rotulo)
    return t and t["status"]


@pytest.mark.parametrize(
    ("texto", "idioma", "status"),
    [
        ("No tengo dolor de pecho ni falta de aire.", "es", "negado"),
        ("No, me duele el pecho, doctora.", "es", "presente"),
        ("No tengo fiebre, pero me duele el pecho.", "es", "presente"),
        ("No chest pain, doctor.", "en", "negado"),
        ("No, I have chest pain since yesterday.", "en", "presente"),
        ("I don't have a fever, but I have chest pain.", "en", "presente"),
        ("¿Le duele el pecho?", "es", "presente"),
        # chip de idioma errado: o acompanhante fala português, "no" é "em o" e não pode virar negação
        ("Ela sente dor no peito desde ontem.", "es", "presente"),
        ("Sinto uma dor no peito.", "en", "presente"),
        ("Ela não sente dor no peito.", "es", "negado"),
    ],
)
def test_negation_and_portuguese_no(client, auth, texto, idioma, status):
    assert queixa(client, auth, texto, idioma) == status


@pytest.mark.parametrize(
    ("texto", "idioma", "esperado"),
    [
        ("Soy alérgica a la penicilina, me da urticaria.", "es", True),
        ("Tengo alergia a la dipirona, me hincha la cara.", "es", True),
        ("I am allergic to penicillin, I get hives.", "en", True),
        ("No soy alérgica a la penicilina.", "es", False),
        ("I'm not allergic to penicillin.", "en", False),
        ("Sin alergias conocidas.", "es", False),
        ("Mi madre es alérgica a la penicilina.", "es", False),  # alergia de parente não é do paciente
        ("My mother is allergic to penicillin.", "en", False),
        ("Ela sente dor, e eu tenho alergia a dipirona.", "es", True),  # português sob chip errado
        # a transcrição põe vírgula antes do "não" solto: continua sendo negativa (es, en e pt)
        ("Alergia a la penicilina, no.", "es", False),
        ("Allergy to penicillin, no.", "en", False),
        ("Alergia a penicilina, não.", "pt-BR", False),
        ("Alergia a la penicilina, sí, me da urticaria.", "es", True),
    ],
)
def test_allergy_said_in_another_language_reaches_the_allergy_rule(client, auth, texto, idioma, esperado):
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    ins = falas(client, h, cid, [{"falante": "paciente", "texto": texto, "t": 1, "idioma": idioma}])["insights"]
    relatadas = [a for a in ins["alergias"] if a["fonte"] == "relatado"]
    assert bool(relatadas) == esperado, relatadas
    assert all(a["refs"] == ["Fala/1"] for a in relatadas)


def test_spanish_allergy_brings_avoid_list_and_reaction_severity(client, auth):
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    texto = "Soy alérgica a la penicilina, se me cerró la garganta."
    ins = falas(client, h, cid, [{"falante": "paciente", "texto": texto, "t": 1, "idioma": "es"}])["insights"]
    penicilina = next(a for a in ins["alergias"] if a["fonte"] == "relatado")
    assert penicilina["alergeno"] == "Penicilinas" and penicilina["evitar"] and penicilina["refs"] == ["Fala/1"]


@pytest.mark.parametrize(
    ("original", "traducao", "diverge"),
    [
        ("Tomo 500 mg dos veces al día", "Tomo 500 mg uma vez ao dia", True),  # frequência
        ("Take 10 mg twice a day", "Tomo 10 mg uma vez ao dia", True),
        ("Tomo 500 mg cada 8 horas", "Tomo 500 mg a cada 12 horas", True),
        ("Tomo 5 mg por día", "Tomo 5 mg por semana", True),  # período sem número
        ("Tomo 0,5 mg", "Tomo 0.5 mg", False),  # vírgula decimal x ponto decimal
        ("Take 0.5 mg", "Tomo 0,5 mg", False),
        ("Tomo 0,5 mg", "Tomo 5 mg", True),
        ("Tomo 1.000 mg", "Tomo 1000 mg", False),  # milhar
        ("Take 1,000 mg", "Tomo 1 mg", True),
        ("Tomo 1.000 mg", "Tomo 1 mg", True),
        ("Tomo insulina 10 unidades", "Tomo insulina 100 unidades", True),
        ("Tomo veinte unidades de insulina", "Tomo 2 unidades de insulina", True),
        ("Tomo 500 mg", "Tomo 500 mg de ibuprofeno", True),  # remédio inventado
        ("Tomo warfarina 5 mg", "Tomo varfarina 5 mg", False),  # grafia do espanhol x pt
        ("I take warfarin 5 mg daily", "Tomo varfarina 5 mg diariamente", False),
        ("I take warfarin 5 mg daily", "Tomo aspirina 5 mg diariamente", True),  # remédio trocado
        ("Tomo 5 mg todos los días", "Tomo 5 mg todos os dias", False),
        ("Hace dos días tengo tos", "Há 2 dias estou com tosse", False),
    ],
)
def test_fidelity_adversarial(original, traducao, diverge):
    assert bool(fidelidade.divergencias(original, traducao)) == diverge, fidelidade.divergencias(original, traducao)


def test_portuguese_markers_disable_the_negation_swap():
    assert idiomas.texto_para_regras("Sinto dor no peito", "es") == "Sinto dor no peito"
    assert "nao" in idiomas.texto_para_regras("No me duele el pecho", "es")


@pytest.mark.parametrize(
    ("original", "de", "traducao", "para", "diverge"),
    [
        # a negação trocada inverte a alergia ou o sintoma sem mudar número nem remédio
        ("No soy alérgica a la penicilina", "es", "Sou alérgica à penicilina", "pt-BR", True),
        ("No soy alérgica a la penicilina", "es", "Não sou alérgica à penicilina", "pt-BR", False),
        ("Sí, me duele", "es", "Não, dói", "pt-BR", True),
        ("I don't have allergies", "en", "Tenho alergias", "pt-BR", True),
        ("I don’t have allergies", "en", "Não tenho alergias", "pt-BR", False),  # apóstrofo tipográfico
        ("O paciente não tem alergia", "pt-BR", "El paciente tiene alergia", "es", True),  # fala do médico, pt → es
        ("Sin alergias conocidas", "es", "Sem alergias conhecidas", "pt-BR", False),
        # "no" é "em o" em português: não é negação
        ("Dói no peito", "pt-BR", "Me duele en el pecho", "es", False),
        ("Me duele en el pecho", "es", "Dói no peito", "pt-BR", False),
        ("Aucune allergie", "fr", "Sem alergia", "pt-BR", False),  # idioma sem lista de negações: não confere
        # metade de uma dose e AAS (nome curto)
        ("Tomo media pastilla de 50 mg", "es", "Tomo uma pastilha de 50 mg", "pt-BR", True),
        ("Tomo media pastilla de 50 mg", "es", "Tomo meio comprimido de 50 mg", "pt-BR", False),
        ("Dolor en medio del pecho", "es", "Dor no meio do peito", "pt-BR", False),
        ("Tomo aspirina", "es", "Tomo AAS", "pt-BR", False),
        ("I take 500 mg at 8 am", "en", "Tomo 500 mg às 8h", "pt-BR", False),
    ],
)
def test_fidelity_negation_half_doses_and_short_names(original, de, traducao, para, diverge):
    got = fidelidade.divergencias(original, traducao, de, para)
    assert bool(got) == diverge, got


def test_translation_that_flips_negation_raises_alert_and_rules_keep_the_original(client, auth, monkeypatch):
    """Ponta a ponta: a tradução inverte "não sou alérgica"; há alerta e a alergia não é inventada pela tradução."""
    h = auth("medico")
    cid = start(client, h, "cen8")["id"]
    falas(
        client, h, cid, [{"falante": "paciente", "texto": "No soy alérgica a la penicilina.", "t": 1, "idioma": "es"}]
    )
    monkeypatch.setattr(
        consult_ai,
        "run_translation",
        lambda segments, targets, pseudo: {n: "Sou alérgica à penicilina." for n in targets},
    )
    out = client.post(f"/consultations/{cid}/translate", json={}, headers=h)
    assert out.status_code == 200, out.text
    ins = out.json()["insights"]
    assert [a for a in ins["alertas"] if a["categoria"] == "traducao" and "negação" in a["texto"]]
    assert not [a for a in ins["alergias"] if a["fonte"] == "relatado"]
