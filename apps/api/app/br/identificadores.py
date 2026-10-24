"""Identificadores brasileiros da saúde (#52): CPF, CNS, CNES e CBO, com validação e mensagens em português.

Os NamingSystems são os da RNDS (mesmo prefixo do CPF que o código já usava). CPF e CNS têm dígito verificador; o
CNES (7 dígitos) e o CBO (6 dígitos) não têm dígito público, então só o formato é conferido. O CBO NÃO é conferido
contra
a tabela oficial da CBO-2002 (pendente de conferência humana, ver STATUS).
"""

from __future__ import annotations

import re

from app.fhir_client import codes

CNS_SYSTEM = "http://rnds.saude.gov.br/fhir/r4/NamingSystem/cns"
CNES_SYSTEM = "http://rnds.saude.gov.br/fhir/r4/NamingSystem/cnes"
CBO_SYSTEM = "http://www.saude.gov.br/fhir/r4/CodeSystem/BRCBO"
CPF_SYSTEM = codes.CPF


def so_digitos(valor: object) -> str:
    """Tira máscara e espaços ("123.456.789-09" vira "12345678909"). Aceita só texto; outro tipo vira vazio."""
    return re.sub(r"[\s.\-/]", "", valor) if isinstance(valor, str) else ""


def _exigir(valor: object, rotulo: str) -> str:
    d = so_digitos(valor)
    if not d:
        raise ValueError(f"{rotulo} não informado.")
    if not (d.isascii() and d.isdigit()):
        raise ValueError(f"{rotulo} deve ter só números (a máscara é aceita).")
    return d


def validar_cpf(valor: object) -> str:
    d = _exigir(valor, "CPF")
    if len(d) != 11:
        raise ValueError("CPF deve ter 11 dígitos.")
    if d == d[0] * 11:
        raise ValueError("CPF inválido (dígitos repetidos).")
    for n in (9, 10):
        soma = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (soma * 10) % 11 % 10 != int(d[n]):
            raise ValueError("CPF inválido (dígito verificador não confere).")
    return d


def cns_definitivo(pis11: str) -> str:
    """CNS definitivo (começa com 1 ou 2): 11 dígitos do PIS + sufixo 000/001 + dígito verificador."""
    soma = sum(int(pis11[i]) * (15 - i) for i in range(11))
    dv = 11 - soma % 11
    if dv == 11:
        dv = 0
    if dv == 10:
        dv = 11 - (soma + 2) % 11
        return f"{pis11}001{dv}"
    return f"{pis11}000{dv}"


def validar_cns(valor: object) -> str:
    d = _exigir(valor, "CNS")
    if len(d) != 15:
        raise ValueError("CNS deve ter 15 dígitos.")
    if d[0] in "12":
        if cns_definitivo(d[:11]) != d:
            raise ValueError("CNS inválido (dígito verificador não confere).")
    elif d[0] in "789":
        if len(set(d[1:])) == 1:  # 700000000000000, 888888888888888...: passam na conta do dígito, mas não são CNS
            raise ValueError("CNS provisório inválido (dígitos repetidos).")
        if sum(int(d[i]) * (15 - i) for i in range(15)) % 11 != 0:
            raise ValueError("CNS provisório inválido (dígito verificador não confere).")
    else:
        raise ValueError("CNS inválido: deve começar com 1, 2, 7, 8 ou 9.")
    return d


def validar_cnes(valor: object) -> str:
    d = _exigir(valor, "CNES")
    if len(d) != 7:
        raise ValueError("CNES deve ter 7 dígitos.")
    if d == "0" * 7:
        raise ValueError("CNES inválido (zerado).")
    return d


def validar_cbo(valor: object) -> str:
    d = _exigir(valor, "CBO")
    if len(d) != 6:
        raise ValueError("CBO deve ter 6 dígitos (ex.: 225125).")
    if d[0] == "0":
        raise ValueError("CBO inválido: a família ocupacional não pode começar com 0.")
    return d


def identificador_fhir(sistema: str, valor: str) -> dict:
    return {"system": sistema, "value": valor}


def do_paciente(patient: dict) -> dict[str, str | None]:
    """CPF e CNS do Patient FHIR; identificador que não passa na validação é ignorado."""
    achados: dict[str, str | None] = {"cpf": None, "cns": None}
    for ident in patient.get("identifier") or []:
        try:
            if ident.get("system") == CNS_SYSTEM and not achados["cns"]:
                achados["cns"] = validar_cns(ident.get("value"))
            elif ident.get("system") == CPF_SYSTEM and not achados["cpf"]:
                achados["cpf"] = validar_cpf(ident.get("value"))
        except ValueError:
            continue
    return achados
