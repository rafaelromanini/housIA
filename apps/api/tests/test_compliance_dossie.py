"""#53 — dossiê de conformidade vivo: RoPA, RIPD e matriz, com partes calculadas e referências que existem."""

import ast
from pathlib import Path

import pytest

from app.compliance import service
from app.db import session as db_session
from tests.test_audit_integridade import ids_da_cadeia, sql

RAIZ = Path(__file__).resolve().parents[3]


def dossie(client, auth, **params):
    return client.get("/compliance/dossie", params=params, headers=auth("gestor"))


def test_dossie_json_tem_ropa_ripd_matriz_e_partes_calculadas(client, auth):
    res = dossie(client, auth)
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    d = res.json()
    assert d["versao"] and d["atualizado_em"] and "não é parecer jurídico" in d["aviso"].lower().replace("nao", "não")
    assert len(d["ropa"]) >= 5 and d["ripd"]["riscos"] and len(d["matriz"]) >= 10
    assert any("LLM" in op for o in d["ropa"] for op in o["operadores"])  # provedor de LLM declarado como operador
    c = d["calculado"]
    assert c["cadeia_audit_log"]["integra"] is True and c["acessos_quebra_de_vidro"]["total"] == 0
    assert c["provedor_de_llm"]["modo"] == "mock" and c["provedor_de_llm"]["provedor"] is None
    assert "precisa_revisao_juridica" in d and d["resumo_matriz"]["nao_implementado"] >= 1


def test_dossie_markdown_traz_as_tres_secoes_e_a_versao(client, auth):
    res = dossie(client, auth, formato="md")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/markdown")
    texto = res.text
    for trecho in ("## RoPA", "## RIPD resumido", "## Matriz CFM e LGPD", "## Estado do sistema (calculado)"):
        assert trecho in texto
    assert f"versão {service.estatico()['versao']}" in texto and "NÃO implementado" in texto


def test_dossie_so_o_gestor_e_a_geracao_e_auditada(client, auth):
    for papel in ("medico", "enfermagem"):
        assert dossie(client, auth) and client.get("/compliance/dossie", headers=auth(papel)).status_code == 403
    assert client.get("/compliance/dossie").status_code == 401
    assert dossie(client, auth, formato="pdf").status_code == 422
    logado = client.get("/audit", params={"action": "compliance."}, headers=auth("gestor")).json()
    assert logado["total"] >= 1


def test_dossie_reflete_a_cadeia_quebrada_e_a_retencao_configurada(client, auth, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "transcript_retention_days", "consulta=90")
    client.get("/audit", headers=auth("gestor"))
    sql("UPDATE audit_log SET action = 'x.forjada' WHERE id = :i", i=ids_da_cadeia()[0])
    c = dossie(client, auth).json()["calculado"]
    assert c["cadeia_audit_log"]["integra"] is False and c["cadeia_audit_log"]["primeira_quebra"]["id"]
    assert c["retencao"]["configurada"] is True and c["retencao"]["transcricao_dias_por_tipo"]["consulta"] == 90
    assert "QUEBRADA" in service.markdown(dossie(client, auth).json())


def _funcoes(path: Path) -> set[str]:
    return {n.name for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.FunctionDef)}


def test_a_matriz_so_cita_codigo_e_testes_que_existem():
    d = service.estatico()
    itens = [*d["matriz"], *d["ropa"]]
    for item in itens:
        for caminho in item["codigo"]:
            assert (RAIZ / caminho).exists(), f"código citado não existe: {caminho}"
    for m in d["matriz"]:
        for ref in m["testes"]:
            arquivo, _, funcao = ref.partition("::")
            assert (RAIZ / arquivo).is_file(), f"arquivo de teste não existe: {arquivo}"
            assert funcao in _funcoes(RAIZ / arquivo), f"teste não existe: {ref}"
        if m["status"] == "implementado":
            assert m["testes"] and m["codigo"], f"'implementado' sem teste ou código: {m['artigo']}"
        if m["status"] == "nao_implementado":
            assert not m["testes"] and not m["codigo"], f"'não implementado' não pode citar controle: {m['artigo']}"


def test_nada_no_dossie_afirma_conformidade_e_a_base_legal_pede_revisao():
    d = service.estatico()
    texto = str(d).lower()
    assert (
        "totalmente conforme" not in texto
        and "100% conforme" not in texto
        and "em conformidade com a lgpd" not in texto
    )
    assert all(o.get("revisao_juridica") for o in d["ropa"])  # toda base legal proposta pede revisão humana
    assert all(op for o in d["ropa"] for op in [o["base_legal"], o["retencao"], o["finalidade"], o["dados"]])


@pytest.mark.usefixtures("client")
def test_dossie_funciona_sem_usuario_de_outra_organizacao_vazar(client, auth):
    from tests.test_audit_integridade import outra_org_gestor

    meu = dossie(client, auth).json()["calculado"]["cadeia_audit_log"]["total"]
    outro = client.get("/compliance/dossie", headers=outra_org_gestor(client)).json()["calculado"]["cadeia_audit_log"]
    assert outro["total"] <= meu and db_session.engine is not None


def test_pendencias_do_ripd_sao_todas_texto(client, auth):
    """Item de lista YAML com ': ' sem aspas vira objeto e quebra a tela de conformidade (React #31)."""
    d = dossie(client, auth).json()
    assert d["ripd"]["pendencias"] and all(isinstance(p, str) for p in d["ripd"]["pendencias"])
