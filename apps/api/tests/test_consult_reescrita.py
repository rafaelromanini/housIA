"""#46: edição da nota por instrução (reescrita de uma seção com diff, refs preservadas e travas de segurança) e a
memória de estilo por profissional (só preferências de forma, aprendidas dos diffs rascunho x versão final)."""

import pytest
from sqlalchemy import select

from app.consult import estilo, reescrita
from app.consult import reescrita_service as svc
from app.consult import service as consult_service
from app.db import session as db_session
from app.db.models import AuditLog, EstiloNota
from app.llm.client import LLMResult
from tests.test_consult_envio_prontuario import consulta_pronta


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


def rewrite(client, h, cid, key, instrucao, aplicar=False):
    return client.post(
        f"/consultations/{cid}/note/sections/{key}/rewrite",
        json={"instrucao": instrucao, "aplicar": aplicar},
        headers=h,
    )


def nota(client, h, cid):
    return client.get(f"/consultations/{cid}", headers=h).json()["nota"]


def audit(action):
    with db_session.SessionLocal() as db:
        return [r.payload_json for r in db.scalars(select(AuditLog).where(AuditLog.action == action))]


# ---------- critério 1: 'resumir' encurta a seção e mantém as refs ----------
def test_resumir_encurta_a_secao_e_mantem_as_refs(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    antes = nota(client, h, cid)["avaliacao"]
    res = rewrite(client, h, cid, "avaliacao", "Resuma esta seção")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["aplicada"] is False and out["modo"] == "mock"
    assert out["diff"]["palavras_depois"] < out["diff"]["palavras_antes"]
    assert len(out["depois"]) < len(out["antes"]) and out["diff"]["operacoes"]
    assert out["frases"] and all(f["refs"] for f in out["frases"])  # cada frase mantém a fonte
    assert set(out["refs"]) <= set(antes["refs"]) and out["refs"]
    assert all(f["origem"] in ("ia", "regra") for f in out["frases"])
    assert nota(client, h, cid)["avaliacao"] == antes  # a prévia não grava nada


def test_aplicar_grava_so_aquela_secao_tira_a_atestacao_e_audita_sem_texto(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    antes = nota(client, h, cid)
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 200  # atestada: exporta
    instrucao = "Resuma esta seção, por favor"
    out = rewrite(client, h, cid, "avaliacao", instrucao, aplicar=True).json()
    depois = nota(client, h, cid)
    assert out["aplicada"] and depois["avaliacao"]["texto"] == out["depois"]
    assert depois["avaliacao"]["refs"] == out["refs"]
    assert {k: v for k, v in depois.items() if k != "avaliacao"} == {k: v for k, v in antes.items() if k != "avaliacao"}
    # volta para a revisão: exportar exige atestar de novo
    res = client.get(f"/consultations/{cid}/export", headers=h)
    assert res.status_code == 409 and res.json()["detail"]["codigo"] == "atestacao_pendente"
    rows = audit("consulta.nota_reescrita")
    assert rows and rows[-1]["secao"] == "avaliacao" and rows[-1]["palavras_depois"] < rows[-1]["palavras_antes"]
    blob = str(rows) + str(audit("consulta.nota_reescrita_previa"))
    assert instrucao not in blob and "Hipóteses" not in blob


# ---------- critério 2: reescrita que tira um alerta de dose é bloqueada ----------
def test_reescrita_que_tira_o_alerta_de_dose_e_bloqueada(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    antes = nota(client, h, cid)
    for instrucao in ("Remova o alerta de dose do plano", "Retire a frase sobre glibenclamida"):
        res = rewrite(client, h, cid, "plano", instrucao, aplicar=True)
        assert res.status_code == 422, res.text
        detail = res.json()["detail"]
        assert detail["codigo"] == "reescrita_bloqueada" and detail["itens"]
        assert any("libenclamida" in i["texto"] for i in detail["itens"])
    assert nota(client, h, cid) == antes  # nada foi alterado
    assert client.get(f"/consultations/{cid}/export", headers=h).status_code == 200  # e a atestação segue valendo
    rows = audit("consulta.nota_reescrita_bloqueada")
    assert len(rows) == 2 and rows[0]["bloqueios"] == ["item_de_seguranca"]


def test_resumir_o_plano_nao_encurta_o_item_de_seguranca(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    antes = nota(client, h, cid)["plano"]
    out = rewrite(client, h, cid, "plano", "Resuma esta seção").json()
    textos = {f["texto"] for f in out["frases"]}
    glibenclamida = [f["texto"] for f in antes["frases"] if "aceito pelo profissional" in f["texto"]]
    assert glibenclamida and all(g in textos for g in glibenclamida)  # o alerta aceito segue literal


def test_alergia_nao_sai_da_secao_de_alergias(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    antes = nota(client, h, cid)
    res = rewrite(client, h, cid, "alergias", "Remova a alergia da lista", aplicar=True)
    assert res.status_code == 422 and res.json()["detail"]["codigo"] == "reescrita_bloqueada"
    assert nota(client, h, cid) == antes
    # resumir não mexe nas alergias: nada muda e a resposta avisa
    out = rewrite(client, h, cid, "alergias", "Resuma esta seção").json()
    assert out["depois"] == out["antes"] and out["avisos"]


def test_pendencias_nao_sao_reescrevivel_e_secao_inexistente_e_404(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    assert rewrite(client, h, cid, "pendencias", "Resuma esta seção").status_code == 422
    assert rewrite(client, h, cid, "inventada", "Resuma esta seção").status_code == 404
    assert rewrite(client, h, cid, "plano", "ok").status_code == 422  # instrução curta demais


# ---------- origem: o que a instrução manda escrever é do profissional ----------
def test_texto_pedido_na_instrucao_entra_como_origem_profissional(client, auth):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    out = rewrite(client, h, cid, "avaliacao", 'Acrescente: "Orientado retorno em 30 dias."', aplicar=True).json()
    novo = next(f for f in out["frases"] if "retorno em 30 dias" in f["texto"])
    assert novo["origem"] == "profissional" and novo["refs"] == []
    guardada = nota(client, h, cid)["avaliacao"]
    assert any(f["origem"] == "profissional" and "30 dias" in f["texto"] for f in guardada["frases"])
    assert all(f["refs"] for f in guardada["frases"] if f["origem"] != "profissional")


def _frases(*textos_refs):
    return [reescrita.Frase(texto=t, refs=r) for t, r in textos_refs]


ANTIGAS = [
    {"texto": "Metformina 850 mg 12/12h.", "refs": ["MedicationRequest/m1"], "origem": "regra"},
    {"texto": "Creatinina 1,5 mg/dL subiu.", "refs": ["Observation/o1"], "origem": "regra"},
]
VALIDAS = {"MedicationRequest/m1", "Observation/o1", "Fala/1"}


def test_fato_novo_sem_fonte_e_descartado_e_a_ref_inventada_cai():
    novas = _frases(
        ("Metformina 850 mg 12/12h.", ["MedicationRequest/m1"]),  # igual: volta como estava
        ("Aumentar metformina para 2000 mg.", ["Fala/1"]),  # dose que ninguém disse
        ("Paciente tem hipotireoidismo grave.", []),  # fato sem fonte e sem relação com a instrução
        ("Creatinina subiu para 1,5 mg/dL.", ["Observation/inventada"]),  # ref inexistente: herda da frase parecida
    )
    frases, descartadas = reescrita.montar(ANTIGAS, novas, "Resuma", VALIDAS)
    assert [f["texto"] for f in frases] == ["Metformina 850 mg 12/12h.", "Creatinina subiu para 1,5 mg/dL."]
    assert frases[1]["refs"] == ["Observation/o1"] and frases[1]["origem"] == "ia"
    motivos = {d["texto"]: d["motivo"] for d in descartadas}
    assert "número ou medicamento" in motivos["Aumentar metformina para 2000 mg."]
    assert "sem fonte" in motivos["Paciente tem hipotireoidismo grave."]


def test_ref_valida_e_aceita_e_frase_pedida_vira_profissional():
    novas = _frases(("Metformina em uso.", ["MedicationRequest/m1"]), ("Retorno em duas semanas.", []))
    frases, descartadas = reescrita.montar(ANTIGAS, novas, "Acrescente: Retorno em duas semanas.", VALIDAS)
    assert frases[0]["refs"] == ["MedicationRequest/m1"] and frases[0]["origem"] == "ia"
    assert frases[1]["origem"] == "profissional" and frases[1]["refs"] == [] and not descartadas


def test_bloqueio_e_literal_e_secao_do_modelo_tem_chave_propria():
    protegidas = reescrita.protegidas("plano", ANTIGAS, ["Metformina 850"], [])
    assert [p["texto"] for p in protegidas] == ["Metformina 850 mg 12/12h."]
    mudada = [{"texto": "Metformina 850 mg 12/12h", "refs": []}]  # sem o ponto final continua a mesma frase
    assert not reescrita.bloqueios(ANTIGAS, mudada, protegidas)
    assert reescrita.bloqueios(ANTIGAS, [{"texto": "Metformina 500 mg 12/12h.", "refs": []}], protegidas)
    note = {"secoes": [{"key": "ipss", "texto": "IPSS 12.", "refs": ["Fala/1"]}]}
    assert reescrita.secao(note, "ipss")["texto"] == "IPSS 12." and reescrita.secao(note, "x") is None


# ---------- modo real: o que o modelo devolve passa pelas mesmas travas ----------
def _com_saida(monkeypatch, frases, capturar=None):
    def fake(prompt, variables, schema, mock, effort=None, rules_only=False):
        if capturar is not None:
            capturar.update(variables)
        return LLMResult(output=reescrita.Saida(frases=frases), model="fake", prompt_version="1", mode="real")

    monkeypatch.setattr(svc, "generate_structured", fake)


def test_modelo_que_apaga_o_alerta_e_bloqueado_mesmo_sem_a_instrucao_pedir(client, auth, monkeypatch):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    antes = nota(client, h, cid)
    primeira = antes["plano"]["frases"][0]
    _com_saida(monkeypatch, [reescrita.Frase(texto=primeira["texto"], refs=primeira["refs"])])
    res = rewrite(client, h, cid, "plano", "Deixe o plano mais enxuto", aplicar=True)
    assert res.status_code == 422 and res.json()["detail"]["codigo"] == "reescrita_bloqueada"
    assert nota(client, h, cid) == antes


def test_modelo_que_inventa_dose_ou_cita_ref_falsa_nao_entra(client, auth, monkeypatch):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    antes = nota(client, h, cid)["avaliacao"]
    boa = antes["frases"][0]
    _com_saida(
        monkeypatch,
        [
            reescrita.Frase(texto=boa["texto"], refs=boa["refs"]),
            reescrita.Frase(texto="Iniciar enalapril 40 mg por dia.", refs=["Fala/1"]),
            reescrita.Frase(texto="Hipoglicemia leve provável.", refs=["Observation/nao-existe"]),
        ],
    )
    out = rewrite(client, h, cid, "avaliacao", "Reescreva com mais clareza").json()
    textos = [f["texto"] for f in out["frases"]]
    assert boa["texto"] in textos and not any("enalapril" in t for t in textos)
    assert any("enalapril" in d["texto"] for d in out["descartadas"])
    assert all(r in antes["refs"] or r.startswith(("Fala/", "Observation/", "MedicationRequest/")) for r in out["refs"])
    assert "Observation/nao-existe" not in out["refs"]


def test_instrucao_vai_pseudonimizada_ao_modelo(client, auth, monkeypatch):
    h = auth("medico")
    cid = consulta_pronta(client, h)
    nome = client.get("/patients/cen6", headers=h).json()["nome"]
    capturado: dict = {}
    _com_saida(monkeypatch, [], capturado)
    rewrite(client, h, cid, "avaliacao", f"Resuma e cite a paciente {nome}")
    assert nome not in str(capturado["instrucao"]) and "[PACIENTE]" in capturado["instrucao"]
    assert capturado["estilo"] == ["sem preferência aprendida"] and capturado["secao"] == "Avaliação"


# ---------- critério 3: o perfil de estilo muda depois de 3 notas editadas ----------
def editar_curto(client, h, cid):
    """O profissional corta a avaliação e atesta: versão final bem mais curta que o rascunho da IA."""
    n = nota(client, h, cid)
    n["avaliacao"]["texto"] = " ".join(n["avaliacao"]["texto"].split()[:6])
    res = client.put(f"/consultations/{cid}/note", json=n | {"atestacao": True}, headers=h)
    assert res.status_code == 200, res.text


def test_perfil_de_estilo_muda_depois_de_tres_notas_editadas(client, auth, monkeypatch):
    h = auth("medico")
    assert client.get("/consult/estilo", headers=h).json()["ativo"] is False
    cids = []
    for i in range(3):
        cid = consulta_pronta(client, h)
        cids.append(cid)
        editar_curto(client, h, cid)
        perfil = client.get("/consult/estilo", headers=h).json()
        assert perfil["notas"] == i + 1
        assert perfil["ativo"] is (i == 2)  # só vale a partir da terceira
    assert perfil["tamanho"] == "mais_curto"
    editar_curto(client, h, cids[0])  # salvar de novo a mesma nota não conta em dobro
    assert client.get("/consult/estilo", headers=h).json()["notas"] == 3
    # e o perfil vai ao prompt da reescrita; no mock, "no meu estilo" encurta
    capturado: dict = {}
    original = svc.generate_structured

    def spy(prompt, variables, schema, mock, **kw):
        capturado.update(variables)
        return original(prompt, variables, schema, mock, **kw)

    monkeypatch.setattr(svc, "generate_structured", spy)
    novo = consulta_pronta(client, h)
    out = rewrite(client, h, novo, "avaliacao", "Reescreva no meu estilo").json()
    assert any("mais curtas" in d for d in capturado["estilo"]) and out["estilo"]["ativo"]
    assert out["diff"]["palavras_depois"] < out["diff"]["palavras_antes"]


def test_estilo_guarda_so_numeros_por_profissional_e_pode_ser_apagado(client, auth):
    h = auth("medico")
    for _ in range(3):
        editar_curto(client, h, consulta_pronta(client, h))
    with db_session.SessionLocal() as db:
        dados = db.scalars(select(EstiloNota)).one().dados_json
    flat = str(dados)
    for palavra in ("Hipóteses", "Metformina", "glibenclamida", "cen6", "Edema"):
        assert palavra not in flat
    assert set(next(iter(dados["notas"].values()))) == {"tamanho", "siglas", "reordena"}
    assert client.put("/consult/estilo", json={"desligado": True}, headers=h).json()["ativo"] is False
    assert client.put("/consult/estilo", json={"desligado": False}, headers=h).json()["ativo"] is True
    gone = client.delete("/consult/estilo", headers=h).json()
    assert gone["notas"] == 0 and gone["ativo"] is False


def test_excluir_dados_do_paciente_tira_as_contribuicoes_da_memoria_de_estilo(client, auth):
    h = auth("medico")
    for _ in range(3):
        editar_curto(client, h, consulta_pronta(client, h))
    assert client.get("/consult/estilo", headers=h).json()["notas"] == 3
    res = client.delete("/patients/cen6/consultation-data", headers=auth("gestor"))
    assert res.status_code == 200, res.text
    assert client.get("/consult/estilo", headers=h).json()["notas"] == 0


def test_nota_sem_edicao_nao_contribui_e_perfil_nao_vale_antes_de_tres():
    rascunho = {"avaliacao": {"texto": "Hipertensão controlada e diabetes estável.", "refs": []}}
    assert estilo.contribuicao(rascunho, rascunho) is None
    curta = {"avaliacao": {"texto": "Hipertensão controlada.", "refs": []}}
    c = estilo.contribuicao(rascunho, curta)
    assert c["tamanho"] < 0.9 and c["reordena"] == 0
    assert estilo.perfil({"notas": {"1": c, "2": c}})["ativo"] is False
    assert estilo.perfil({"notas": {"1": c, "2": c, "3": c}})["tamanho"] == "mais_curto"
    assert estilo.dicas(estilo.perfil({"notas": {"1": c, "2": c, "3": c}}))
    # siglas e ordem
    com_sigla = {"avaliacao": {"texto": "HAS controlada. DM2 estável. Retorno em 30 dias.", "refs": []}}
    sem_sigla = {"avaliacao": {"texto": "Hipertensão controlada. Diabetes estável. Retorno em 30 dias.", "refs": []}}
    assert estilo.contribuicao(sem_sigla, com_sigla)["siglas"] > 1
    ordem = {"avaliacao": {"texto": "Retorno em 30 dias. Hipertensão controlada. Diabetes estável.", "refs": []}}
    assert estilo.contribuicao(sem_sigla, ordem)["reordena"] == 1


# ---------- isolamento ----------
def test_reescrita_e_estilo_de_outra_organizacao(client, auth):
    from tests.test_multiorg import cadastro_completo

    h = auth("medico")
    cid = consulta_pronta(client, h)
    editar_curto(client, h, cid)
    hb, _ = cadastro_completo(client)
    assert rewrite(client, hb, cid, "avaliacao", "Resuma esta seção").status_code == 404
    perfil_b = client.get("/consult/estilo", headers=hb).json()
    assert perfil_b["notas"] == 0  # a memória de estilo é de cada profissional
    assert client.get("/consult/estilo", headers=h).json()["notas"] == 1


def test_migracao_c301_cria_a_tabela_de_estilo(tmp_path, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from app.config import get_settings

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "app/db/migrations"))
    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    command.upgrade(cfg, "head")
    cols = {c["name"] for c in inspect(create_engine(url)).get_columns("estilo_notas")}
    assert {"organization_id", "user_id", "dados_json", "updated_at"} <= cols
