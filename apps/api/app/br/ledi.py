"""Exportação para o e-SUS APS (#52): ficha de atendimento individual no formato LEDI, a partir da consulta finalizada.

VERSÃO: dicionário LEDI APS 7.3.x (nomes de campo da Ficha de Atendimento Individual do PEC). Os nomes e as
enumerações abaixo foram escritos sem acesso ao dicionário oficial e PRECISAM de conferência humana contra a versão do
e-SUS PEC do município (ver STATUS, "#52"). O arquivo é o JSON estruturado; a serialização Thrift que o PEC importa
é uma etapa à parte (pendente). Nada é inventado: código sem tabela oficial vai para `pendencias`, nunca para o arquivo.

Regras: só CID-10 e CIAP-2 confirmados na nota (hipótese fica de fora); só procedimento SIGTAP oficial de 10 dígitos
(os códigos DEMO-* da distribuição não saem); CIDs além do limite da ficha são listados em `pendencias`.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime

VERSAO_LEDI = "7.3.x (conferir com o e-SUS PEC do município)"
TIPO_FICHA = "atendimento_individual"
MAX_PROBLEMAS = 2  # ciaps/cid10 por atendimento na ficha (conferir)
SIGTAP_OFICIAL = re.compile(r"^\d{10}$")
NAMESPACE = uuid.UUID("5b0c1a52-7a3e-4c1e-9d5e-0f6b1d3a7e52")  # fixo: reexportar gera o mesmo uuid (idempotente)
TURNOS = ((12, 1), (18, 2), (24, 3))  # até 12h manhã, até 18h tarde, resto noite (conferir enumeração do LEDI)


def turno(hora: int) -> int:
    return next(cod for limite, cod in TURNOS if hora < limite)


def cid_ledi(codigo: str) -> str:
    """ "N17.9" vira "N179" (o LEDI usa o CID-10 sem ponto)."""
    return (codigo or "").replace(".", "").upper().strip()


def ciap_ledi(texto: str) -> str | None:
    """ "K86 Hipertensão sem complicações" vira "K86"; vazio ou fora do padrão vira None."""
    m = re.match(r"^([A-Z]\d{2})\b", (texto or "").strip().upper())
    return m.group(1) if m else None


def _procedimentos(note: dict, pendencias: list[str]) -> list[str]:
    proc = ((note.get("relatorio_cirurgico") or {}).get("procedimento_sus")) or {}
    codigo = str(proc.get("codigo") or "").strip()
    if not codigo:
        return []
    if SIGTAP_OFICIAL.match(codigo):
        return [codigo]
    pendencias.append(
        f"Procedimento SIGTAP {codigo} não é um código oficial (10 dígitos): fica fora do arquivo até haver uma "
        "competência real da tabela SIGTAP."
    )
    return []


def montar(
    *,
    consulta_id: int,
    quando: datetime,
    nota: dict,
    paciente: dict,
    sexo: str,
    cns_profissional: str,
    cbo: str,
    cnes: str,
) -> dict:
    """Devolve {"arquivo": ficha LEDI, "pendencias": [...], "avisos": [...]}. Os dados obrigatórios (CNES, CBO, CNS do
    profissional e CNS ou CPF do paciente) são checados antes, no serviço; aqui eles já chegam validados."""
    pendencias: list[str] = []
    cids, ciaps = [], []
    confirmados = [c for c in nota.get("cids", []) if c.get("tipo") in ("principal", "secundario") and c.get("codigo")]
    for c in confirmados:
        if len(cids) < MAX_PROBLEMAS:
            cids.append(cid_ledi(c["codigo"]))
            if (ciap := ciap_ledi(c.get("ciap2", ""))) and ciap not in ciaps:
                ciaps.append(ciap)
        else:
            pendencias.append(f"CID {c['codigo']} não coube na ficha (limite de {MAX_PROBLEMAS}); lance no PEC.")
    for c in confirmados[:MAX_PROBLEMAS]:
        if not ciap_ledi(c.get("ciap2", "")):
            pendencias.append(f"CID {c['codigo']} sem CIAP-2 mapeado: informe o CIAP-2 no PEC.")
    if not cids:
        pendencias.append("A nota não tem CID-10 confirmado: o problema/condição avaliada fica em branco.")
    procedimentos = _procedimentos(nota, pendencias)
    pendencias += [
        "Campos não derivados da consulta (preencher no PEC): INE da equipe, local e tipo de atendimento, "
        "código IBGE do município.",
    ]
    ident = {"cnsCidadao": paciente["cns"]} if paciente.get("cns") else {"cpfCidadao": paciente["cpf"]}
    nasc = paciente.get("nascimento")
    atendimento = ident | {
        "numeroProntuario": f"housia-{consulta_id}",
        "dataNascimento": nasc,
        "sexo": {"M": 0, "F": 1}.get(sexo),
        "turno": turno(quando.hour),
        "dataHoraInicialAtendimento": quando.isoformat(),
        "problemaCondicaoAvaliada": {"ciaps": ciaps, "cid10": cids},
        "procedimentosSigtap": procedimentos,
    }
    arquivo = {
        "tipoFicha": TIPO_FICHA,
        "versaoLedi": VERSAO_LEDI,
        "uuidFicha": f"{cnes}-{uuid.uuid5(NAMESPACE, f'{cnes}:{consulta_id}')}",
        "headerTransport": {
            "profissionalCNS": cns_profissional,
            "cboCodigo_2002": cbo,
            "cnes": cnes,
            "dataAtendimento": quando.date().isoformat(),
        },
        "atendimentosIndividuais": [atendimento],
    }
    avisos = [
        "Arquivo gerado por regras a partir da nota revisada e atestada; o profissional é responsável pelo conteúdo."
    ]
    return {"arquivo": arquivo, "pendencias": pendencias, "avisos": avisos}


def nascimento_iso(valor: object) -> str | None:
    if isinstance(valor, str):
        try:
            return date.fromisoformat(valor[:10]).isoformat()
        except ValueError:
            return None
    return None
