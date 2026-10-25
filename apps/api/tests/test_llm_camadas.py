"""#54 — pseudonimização em camadas (regex + regras de nomes/endereços) e reidentificação local. Só dados sintéticos."""

import csv
import re

import pytest

from app.config import get_settings
from app.llm.camadas import OMITIDO, Escudo, dicionario, norm
from app.llm.pseudonymize import Pseudonymizer

# (entrada, trechos que NÃO podem sobrar, trechos que precisam continuar)
ADVERSARIAIS = [
    # nome no meio de frase, com sobrenome
    ("ontem a Cláudia Ferreira ligou pedindo receita", ["Cláudia", "Ferreira"], ["ligou pedindo receita"]),
    # parentesco com nome em minúsculas (fala transcrita)
    ("minha vizinha claudia mora aqui perto", ["claudia"], ["minha vizinha"]),
    # título + nome que também é palavra comum
    ("quem ajuda é a dona Rosa", ["Rosa"], ["quem ajuda"]),
    # caixa alta e acento
    ("JOÃO SILVA assinou o termo", ["JOÃO", "SILVA"], ["assinou o termo"]),
    # sem acento onde o nome tem acento, caixa mista
    ("a Lucia e o Jose vieram", ["Lucia", "Jose"], ["vieram"]),
    # apresentação
    ("me chamo joana lima e estou com tosse", ["joana", "lima"], ["estou com tosse"]),
    # endereço por extenso, número por extenso
    ("moro na rua das flores, número quarenta e cinco", ["flores", "quarenta"], ["moro na"]),
    # endereço com número, complemento e abreviação
    ("fica na Av. Paulista 1500 apto 12", ["Paulista", "1500", "apto 12"], ["fica na"]),
    # bairro
    ("no bairro Jardim Esperança tem muito mosquito", ["Jardim", "Esperança"], ["tem muito mosquito"]),
    # CPF sem máscara e CNS
    ("cpf 12345678909 e cartão do SUS 898 0012 3456 7890", ["12345678909", "898 0012 3456 7890"], ["cartão do SUS"]),
    # telefone em formatos variados
    ("ligue 11 91234-5678 ou +55 (21) 3456-7890", ["91234-5678", "3456-7890"], ["ligue"]),
    # e-mail, CEP e data de nascimento
    (
        "mande para maria.souza@exemplo.com.br, CEP 01310-100, nasceu em 03/04/1980",
        ["maria.souza", "01310-100", "03/04/1980"],
        ["nasceu em"],
    ),
    # documento
    ("RG 12.345.678-9 e CNH 01234567890", ["12.345.678-9", "01234567890"], ["RG"]),
    # "seu" como possessivo não é nome; "seu Zé" é
    ("seu Zé pediu, seu médico concordou", ["Zé"], ["seu médico concordou"]),
    # nome e sobrenome ambíguo juntos
    ("Pedro Costa Dias chegou com dor", ["Pedro", "Costa", "Dias"], ["chegou com dor"]),
]


@pytest.mark.parametrize(("entrada", "some", "fica"), ADVERSARIAIS)
def test_entradas_adversariais_nao_vazam(entrada, some, fica):
    e = Escudo()
    saida = e.proteger(entrada)
    for trecho in some:
        assert trecho.lower() not in saida.lower(), (trecho, saida)
    for trecho in fica:
        assert trecho in saida, (trecho, saida)
    # round-trip: o texto volta legível pelo mapa local
    assert e.reidentificar_texto(saida) == entrada


@pytest.mark.parametrize(
    "texto",
    [
        "pele rosa e quente, 3 dias de febre, dor na costa, PA 140/90 mmHg",
        "Dias depois voltou; Flores nas margens; losartana 50 mg 12/12 h",
        "Parkinson, Alzheimer e doença de Wilson; escore de Nelson 3; dipirona 500 mg se dor",
        "Data de Nascimento consta no cadastro; consulta em 03/04/2026 às 15:00",
        "creatinina 1,2 mg/dL em 12/05/2026 e glicemia 110",
    ],
)
def test_texto_clinico_comum_nao_e_alterado(texto):
    e = Escudo()
    assert e.proteger(texto) == texto
    assert e.total == 0


def test_vizinha_claudia_e_a_rua_nao_chegam_ao_provedor():
    e = Escudo()
    saida = e.proteger("minha vizinha Cláudia mora na Rua das Flores")
    assert "Cláudia" not in saida and "Flores" not in saida
    assert e.contagem["regras_ner"] == 2 and e.contagem["regex"] == 0


def test_token_e_estavel_na_chamada_e_a_contagem_e_por_camada():
    e = Escudo()
    saida = e.proteger("Cláudia disse que Cláudia viu [PACIENTE] com CPF 123.456.789-09")
    assert saida.count("[NOME_1]") == 2 and "[CPF_1]" in saida
    assert e.contagem == {"estruturado": 1, "regex": 1, "regras_ner": 2}


def test_proteger_e_idempotente():
    e = Escudo()
    uma = e.proteger("a dona Maria mora na Rua das Acácias, 120")
    duas = e.proteger(uma)
    assert "Maria" not in duas and "Acácias" not in duas and e.contagem["regras_ner"] == 2


def test_token_forjado_na_fala_nao_vira_outra_pessoa():
    e = Escudo()
    saida = e.proteger("Cláudia veio. O paciente disse [NOME_1] e [CPF_7]")
    assert "(NOME_1)" in saida and "(CPF_7)" in saida
    # o provedor ecoa o que recebeu: o token forjado NÃO é reidentificado como Cláudia
    assert "Cláudia" not in e.reidentificar_texto("(NOME_1)") and e.reidentificar_texto("[NOME_1]") == "Cláudia"


def test_token_inventado_ou_alterado_pelo_provedor_vira_dado_omitido():
    e = Escudo()
    e.proteger("Cláudia Souza")
    assert e.reidentificar_texto("[NOME_9] e [CPF_1] e [ENDERECO_3]") == f"{OMITIDO} e {OMITIDO} e {OMITIDO}"
    assert e.reidentificar_texto("[nome 1]") == "Cláudia Souza"  # caixa e separador alterados ainda voltam
    assert e.reidentificar_texto("[NOME_1][NOME_1]") == "Cláudia SouzaCláudia Souza"
    assert e.reidentificar_texto("[NOME_99999]") == "[NOME_99999]"  # fora do formato: fica literal, sem vazar


def test_reidentificacao_em_json_escapa_o_valor():
    e = Escudo()
    e.proteger('dona Maria "da Silva"\nfalou')
    bruto = '{"texto": "ligar para [NOME_1] hoje"}'
    import json

    assert json.loads(e.reidentificar_json(bruto))["texto"].startswith("ligar para ")


def test_nomes_do_prontuario_reforcam_a_camada(record):
    rec = record("cen1")
    Pseudonymizer(**rec.identifiers_for_pseudonymization())  # registra os nomes na requisição
    e = Escudo()
    saida = e.proteger("o antonio ribeiro (grafia livre) apareceu e ribeiro voltou")
    assert "antonio" not in saida.lower() and "ribeiro" not in saida.lower()


def test_escudo_inativo_nao_altera_mas_conta_estruturado():
    e = Escudo(ativo=False)
    t = "Cláudia, [PACIENTE] e [CPF]"
    assert e.proteger(t) == t and e.contagem == {"estruturado": 2, "regex": 0, "regras_ner": 0}


def test_dicionario_e_sintetico_publico_e_sem_colisao_clinica():
    firmes, amb = dicionario()
    assert len(firmes) > 150 and amb
    assert norm("Cláudia") in firmes or norm("Claudia") in firmes
    raiz = get_settings().data_dir / "reference"
    palavras = set()
    for nome in ("drugs.csv", "cid10.csv", "condition_lexicon.csv", "procedure_lexicon.csv", "lab_catalog.csv"):
        txt = (raiz / nome).read_text(encoding="utf-8")
        palavras |= {norm(w) for w in re.findall(r"[A-Za-zÀ-ÿ]{3,}", txt) if w[:1].isupper() or nome == "drugs.csv"}
    assert not (firmes & palavras), sorted(firmes & palavras)
    linhas = [ln for ln in (raiz / "nomes_br.csv").read_text(encoding="utf-8").splitlines() if not ln.startswith("#")]
    assert [r["tipo"] for r in csv.DictReader(linhas)]  # cabeçalho válido


# ---------- verificação #54: entradas adversariais novas ----------
VERIFICACAO = [
    # nome entre colchetes ou com sublinhado NÃO é token: não pode escapar da camada
    ("[Maria Silva] veio com o marido", ["Maria", "Silva"], ["veio com o marido"]),
    ("Maria_Silva chegou", ["Maria", "Silva"], ["chegou"]),
    # CPF com espaços e com pontuação mista
    ("CPF 123 456 789 00 e também 123.456.789.00", ["123 456 789 00", "123.456.789.00"], ["CPF", "e também"]),
    # telefone com DDD e nono dígito separados
    ("liga no 11 9 8765 4321 depois das 18h", ["8765 4321"], ["liga no", "depois das 18h"]),
    # número ditado dígito a dígito ("meia" = seis)
    ("o telefone é nove oito sete meia cinco quatro três dois um", ["nove oito sete meia"], ["o telefone é"]),
    # e-mail ofuscado
    ("escreve pra joao arroba gmail ponto com ou maria(at)gmail.com", ["joao arroba", "maria(at)"], ["escreve pra"]),
    # data de nascimento em ISO, com espaços e sem o ano por extenso
    ("data de nascimento: 1950-04-03 e nasci em 3 de abril 1950", ["1950-04-03", "3 de abril 1950"], ["nasci em"]),
    # o mesmo nome dito com título e depois sem
    ("a dona Rosa chegou e depois Rosa disse que melhorou", ["Rosa"], ["chegou e depois", "disse que melhorou"]),
    # dois gatilhos seguidos: o primeiro não pode engolir o segundo
    ("dona Rosa e dona Lúcia Helena vieram", ["Rosa", "Lúcia", "Helena"], ["vieram"]),
    # acompanhante dizendo o próprio nome com apóstrofo e hífen
    ("sou a Ana D'Ávila-Souza, filha dele", ["Ávila", "Souza"], ["filha dele"]),
    # id FHIR com 11 dígitos ou com hífen não é CPF nem telefone
    (
        "veja Observation/12345678901 e MedicationRequest/1234-5678, Fala/3",
        [],
        ["Observation/12345678901", "1234-5678"],
    ),
]


@pytest.mark.parametrize(("entrada", "some", "fica"), VERIFICACAO)
def test_verificacao_54_entradas_adversariais(entrada, some, fica):
    e = Escudo()
    saida = e.proteger(entrada)
    for trecho in some:
        assert trecho.lower() not in saida.lower(), (trecho, saida)
    for trecho in fica:
        assert trecho in saida, (trecho, saida)
    assert norm(e.reidentificar_texto(saida)) == norm(entrada)


@pytest.mark.parametrize(
    "texto",
    [
        "tome 5000 7500 UI; dose 1000 2000 mg/dia; 1 2 3 vezes ao dia por seis meses",
        "Dias após a cirurgia voltou; uma ou duas vezes; três, quatro ou cinco dias; PA 120 80",
        "enoxaparina 40 mg 12 12 h; CEP não informado; leito 12 quarto 3",
    ],
)
def test_verificacao_54_dose_e_numero_clinico_nao_vira_telefone_nem_cpf(texto):
    e = Escudo()
    assert e.proteger(texto) == texto


def test_verificacao_54_gatilho_dentro_de_token_real_nao_e_reprocessado():
    e = Escudo()
    saida = e.proteger("o [PACIENTE] e o [FAMILIAR] falaram com seu João")
    assert "[PACIENTE]" in saida and "[FAMILIAR]" in saida and "João" not in saida
