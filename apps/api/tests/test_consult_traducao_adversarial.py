"""Tradução (#22), verificação adversarial: negação es/en, "no" em português, alergia em outro idioma, fidelidade."""

import pytest

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
