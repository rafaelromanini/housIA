"""Perfis BR Core / RNDS (#52): declaração de `meta.profile` e identificadores brasileiros nos recursos do Bundle.

ATENÇÃO (conferência humana pendente): as URLs abaixo são as canônicas que o time conhece dos guias da RNDS e do BR
Core, escritas sem validação contra os pacotes de IG publicados. Declarar um perfil é afirmar conformidade: antes de
ligar em produção, validar o Bundle com o validador FHIR e os pacotes de IG e corrigir a tabela `PERFIS`. O Sumário de
Alta e o RAC completos (seções obrigatórias) NÃO são gerados aqui; só a declaração no Composition atual.
"""

from __future__ import annotations

from app.br import identificadores as ids

BASE = "http://www.saude.gov.br/fhir/r4/StructureDefinition"
PERFIS = {
    "Patient": f"{BASE}/BRIndividuo-1.0",
    "Practitioner": f"{BASE}/BRProfissional-1.0",
    "Organization": f"{BASE}/BREstabelecimentoSaude-1.0",
    "PractitionerRole": f"{BASE}/BRLotacaoProfissional-1.0",
    "Composition": f"{BASE}/BRRegistroAtendimentoClinico-2.0",
    "DocumentReference": f"{BASE}/BRDocumentoReferencia-1.0",
}
CONFERIDOS: dict[str, bool] = dict.fromkeys(PERFIS, False)  # nenhum validado contra o IG ainda
ORG_URL = "urn:housia:br:organization"
ROLE_URL = "urn:housia:br:practitionerrole"


def declarar(resource: dict) -> dict:
    """Acrescenta `meta.profile` do tipo do recurso, sem duplicar. Recurso sem perfil conhecido fica como está."""
    url = PERFIS.get(resource.get("resourceType", ""))
    if url:
        perfis = resource.setdefault("meta", {}).setdefault("profile", [])
        if url not in perfis:
            perfis.append(url)
    return resource


def aplicar_ao_bundle(
    bundle: dict, *, cns: str | None, cbo: str | None, cnes: str | None, org_nome: str | None
) -> dict:
    """Declara os perfis nas entradas conhecidas e, com o cadastro da organização e do profissional, junta o CNS
    (Practitioner), o Organization com CNES (Composition.custodian) e o PractitionerRole com o CBO. Sem cadastro, só
    declara os perfis: nada é inventado."""
    entries = bundle.get("entry", [])
    prac = next((e for e in entries if e["resource"].get("resourceType") == "Practitioner"), None)
    comp = next((e for e in entries if e["resource"].get("resourceType") == "Composition"), None)
    for e in entries:
        if e["resource"].get("resourceType") in ("Practitioner", "Composition", "DocumentReference"):
            declarar(e["resource"])
    if cnes:
        org = declarar(
            {
                "resourceType": "Organization",
                "identifier": [ids.identificador_fhir(ids.CNES_SYSTEM, cnes)],
                "name": org_nome or f"Estabelecimento CNES {cnes}",
            }
        )
        entries.append(
            {
                "fullUrl": ORG_URL,
                "resource": org,
                "request": {
                    "method": "POST",
                    "url": "Organization",
                    "ifNoneExist": f"identifier={ids.CNES_SYSTEM}|{cnes}",
                },
            }
        )
        if comp is not None:
            comp["resource"]["custodian"] = {"reference": ORG_URL}
    if prac is not None:
        if cns:
            prac["resource"].setdefault("identifier", []).append(ids.identificador_fhir(ids.CNS_SYSTEM, cns))
        if cbo:
            role = declarar(
                {
                    "resourceType": "PractitionerRole",
                    "practitioner": {"reference": prac["fullUrl"]},
                    "code": [{"coding": [{"system": ids.CBO_SYSTEM, "code": cbo}]}],
                }
            )
            if cnes:
                role["organization"] = {"reference": ORG_URL}
            entries.append(
                {
                    "fullUrl": ROLE_URL,
                    "resource": role,
                    "request": {
                        "method": "POST",
                        "url": "PractitionerRole",
                        "ifNoneExist": f"role={ids.CBO_SYSTEM}|{cbo}&practitioner={prac['fullUrl']}",
                    },
                }
            )
    return bundle
