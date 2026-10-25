"""#56 — autorização prévia: Rol/DUT e operadora ao vivo, pacote de justificativa revisado e trilha no audit_log.

LLM_MODE=mock. A base de regras do repositório é de EXEMPLO; os testes de importação usam tabelas sintéticas próprias.
"""

import csv
import io
from datetime import date, timedelta

import pytest
from fhir.resources.R4B.claim import Claim
from fhir.resources.R4B.servicerequest import ServiceRequest
from sqlalchemy import select

from app import clock
from app.config import get_settings
from app.consult import autorizacao, autorizacao_regras
from app.consult import service as consult_service
from app.consult.schemas import Hypothesis
from app.db import session as db_session
from app.db.models import AuditLog, AutorizacaoPrevia
from app.fhir_client import codes
from app.fhir_client.client import get_fhir
from tests.conftest import ANCHOR, CONSENT
from tests.test_consult_acesso import segundo_medico

ACTIVE = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]}
RM = "Vou pedir uma ressonância do joelho."
REF_CSV = get_settings().data_dir / "reference"


@pytest.fixture(autouse=True)
def _limpo():
    consult_service.clear_chart_cache()
    autorizacao_regras.limpar_cache()
    yield
    consult_service.clear_chart_cache()
    autorizacao_regras.limpar_cache()


def nascimento(anos: int, dias_a_menos: int = 0) -> str:
    hoje = clock.now().date()
    try:
        d = hoje.replace(year=hoje.year - anos)
    except ValueError:
        d = hoje.replace(year=hoje.year - anos, day=28)
    return (d + timedelta(days=dias_a_menos)).isoformat()


def condition(pid: str, code: str, text: str, rid: str, verificacao: str | None = None) -> dict:
    res = {
        "resourceType": "Condition",
        "id": rid,
        "clinicalStatus": ACTIVE,
        "code": {"coding": [{"system": codes.ICD10, "code": code, "display": text}], "text": text},
        "subject": {"reference": f"Patient/{pid}"},
        "onsetDateTime": (ANCHOR - timedelta(days=900)).isoformat(),
    }
    if verificacao:
        res["verificationStatus"] = {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": verificacao}]
        }
    return res


def paciente(client, h, nasc="1960-05-02", cid_codigo="M17", verificacao=None, nome="Jorge Teste Prado") -> str:
    novo = client.post("/patients", json={"nome": nome, "nascimento": nasc, "sexo": "M"}, headers=h)
    assert novo.status_code == 201, novo.text
    pid = novo.json()["id"]
    if cid_codigo:
        get_fhir().update(condition(pid, cid_codigo, "Gonartrose", f"{pid}-cond", verificacao))
    consult_service.clear_chart_cache()
    return pid


def abrir(client, h, pid) -> int:
    res = client.post(f"/patients/{pid}/consultations", json={"tipo": "consulta", "consentimento": CONSENT}, headers=h)
    assert res.status_code == 200, res.text
    return res.json()["id"]


def falar(client, h, cid, *falas, t0=1):
    segs = [{"falante": f, "texto": x, "t": t0 + i, "origem": "demo"} for i, (f, x) in enumerate(falas)]
    res = client.post(f"/consultations/{cid}/segments", json={"segmentos": segs}, headers=h)
    assert res.status_code == 200, res.text
    return res.json()["insights"]


def aut(ins, rid="rm_joelho"):
    return next((a for a in ins["autorizacoes"] if a["id"] == rid), None)


def crit(a, cid_):
    return next(i for i in a["criterios"] if i["id"] == cid_)


def preparado(client, h, *falas_extra, nasc="1960-05-02"):
    """Paciente com gonartrose confirmada, RM de joelho pedida e tratamento conservador dito."""
    pid = paciente(client, h, nasc)
    cid = abrir(client, h, pid)
    ins = falar(
        client,
        h,
        cid,
        ("medico", RM),
        ("paciente", "Já fiz fisioterapia e tomo anti-inflamatório há meses, sem melhora."),
        *falas_extra,
    )
    return pid, cid, ins


def gerar(client, h, cid, rid="rm_joelho"):
    return client.post(f"/consultations/{cid}/autorizacoes", json={"regra_id": rid}, headers=h)


def audit(prefixo="consulta.autorizacao"):
    with db_session.SessionLocal() as db:
        return [a for a in db.scalars(select(AuditLog).order_by(AuditLog.id)) if a.action.startswith(prefixo)]


# ---------- base de regras ----------
def test_base_de_exemplo_e_marcada_nao_oficial_com_fonte_versao_e_vigencia():
    base = autorizacao_regras.base_ativa()
    assert len(base.regras) >= 14 and not base.erros
    cid10 = [r["codigo"] for r in csv.DictReader(open(REF_CSV / "cid10.csv", encoding="utf-8"))]
    for r in base.regras.values():
        assert r.origem == "exemplo" and r.conferencia == "pendente" and not r.oficial, r.id
        assert "NÃO OFICIAL" in r.fonte and r.versao and r.vigencia.inicio, r.id
        assert r.criterios and r.termos and r.documentos, r.id
        for prefixo in r.cids:
            assert any(c.startswith(prefixo) for c in cid10), (r.id, prefixo)
    assert "EXEMPLO" in base.aviso() and base.operadoras


# ---------- critério 1: RM de joelho sugere a pergunta do tratamento conservador ----------
def test_citar_rm_de_joelho_sugere_a_pergunta_do_tratamento_conservador(client, auth):
    h = auth()
    pid = paciente(client, h)
    cid = abrir(client, h, pid)
    ins = falar(client, h, cid, ("medico", RM))
    a = aut(ins)
    assert a and a["gatilho"] == "fala" and a["gatilho_refs"] == ["Fala/1"] and a["situacao"] == "incompleta"
    assert crit(a, "conservador")["status"] == "faltando" and crit(a, "conservador")["refs"] == []
    perguntas = [p["pergunta"] for p in ins["perguntas"]]
    assert any("tratamento conservador" in p for p in perguntas)
    assert crit(a, "cid")["status"] == "atendido" and crit(a, "cid")["refs"] == [f"Condition/{pid}-cond"]
    assert crit(a, "radiografia")["status"] == "faltando"  # sem exame prévio no prontuário
    # a pergunta some quando o paciente conta (a evidência é a Fala/N dele)
    ins2 = falar(client, h, cid, ("paciente", "Já fiz fisioterapia e tomei anti-inflamatório, sem melhora."), t0=5)
    c2 = crit(aut(ins2), "conservador")
    assert c2["status"] == "atendido" and c2["refs"] == ["Fala/2"]
    assert not any("tratamento conservador" in p["pergunta"] for p in ins2["perguntas"])


def test_sem_evidencia_nada_e_atendido_cid_e_so_hipotese_ou_provisorio(client, auth):
    h = auth()
    pid = paciente(client, h, cid_codigo=None)  # sem nenhum diagnóstico
    ins = falar(client, h, abrir(client, h, pid), ("medico", RM))
    a = aut(ins)
    assert {i["status"] for i in a["criterios"]} == {"faltando"} and a["faltam"] == len(a["criterios"])
    assert all(i["refs"] == [] for i in a["criterios"])
    # diagnóstico provisório no prontuário não é CID confirmado
    pid2 = paciente(client, h, verificacao="provisional")
    ins2 = falar(client, h, abrir(client, h, pid2), ("medico", RM))
    assert crit(aut(ins2), "cid")["status"] == "faltando"


def test_cid_so_como_hipotese_nao_vale_e_confirmada_vale(record):
    from app import clock as ck
    from app.consult.chart import ChartView
    from app.timeline.context import build_context
    from app.timeline.events import build_timeline

    rec = record("cen6")
    chart = ChartView(rec, build_timeline(rec), build_context(rec), ck.now())
    hip = Hypothesis(
        hipotese="Neoplasia de mama",
        cid="C50",
        confianca="media",
        a_favor=[],
        contra=[],
        proximos_passos="",
        sinais_de_alarme="",
        refs=["Fala/1"],
    )
    ctx = autorizacao.Ctx(chart, [], [hip], {}, ck.now().date(), rec.id)
    r = autorizacao.avaliar_regra("cid:C50", ctx)
    assert not r.ok and "só como hipótese" in r.evidencia and r.refs == []
    ctx.extra = {"hipoteses": {"C50": {"decisao": "provisoria"}}}
    assert not autorizacao.avaliar_regra("cid:C50", ctx).ok
    ctx.extra = {"hipoteses": {"C50": {"decisao": "confirmada"}}}
    ok = autorizacao.avaliar_regra("cid:C50", ctx)
    assert ok.ok and ok.refs == ["Fala/1"]


def test_tratamento_negado_planejado_ou_perguntado_nao_conta(client, auth):
    h = auth()
    for fala in (
        ("paciente", "Nunca fiz fisioterapia."),
        ("medico", "Vou pedir fisioterapia."),
        ("medico", "O senhor já fez fisioterapia?"),
    ):
        pid = paciente(client, h)
        ins = falar(client, h, abrir(client, h, pid), ("medico", RM), fala)
        assert crit(aut(ins), "conservador")["status"] == "faltando", fala


def test_so_o_pedido_do_profissional_abre_a_autorizacao(client, auth):
    h = auth()
    for falante, texto in (
        ("paciente", "Doutor, eu queria uma ressonância do joelho."),
        ("medico", "Não vou pedir ressonância do joelho agora."),
        ("medico", "Se piorar, eu peço a ressonância do joelho."),
        ("medico", "O senhor já fez ressonância do joelho?"),
        ("medico", "O resultado da ressonância do joelho mostrou desgaste."),
    ):
        pid = paciente(client, h)
        ins = falar(client, h, abrir(client, h, pid), (falante, texto))
        assert aut(ins) is None, texto


# ---------- idade no limite ----------
def test_idade_no_limite_minimo_e_maximo(client, auth):
    h = auth()
    fala = ("medico", "Vou pedir a cirurgia bariátrica.")
    casos = [(65, 0, "atendido"), (65, -1, "faltando")]  # 65 anos exatos vale; um dia antes dos 66 também
    # idade_max=65: nascer há 66 anos + 1 dia faz 65 anos e 364 dias (vale); há 66 anos exatos, não vale
    pid = paciente(client, h, nasc=nascimento(66, 1), cid_codigo="E66")
    a = aut(falar(client, h, abrir(client, h, pid), fala), "cirurgia_bariatrica")
    assert crit(a, "idade_max")["status"] == "atendido" and "65 anos" in crit(a, "idade_max")["evidencia"]
    pid = paciente(client, h, nasc=nascimento(66), cid_codigo="E66")
    a = aut(falar(client, h, abrir(client, h, pid), fala), "cirurgia_bariatrica")
    assert crit(a, "idade_max")["status"] == "faltando" and crit(a, "idade_max")["refs"] == []
    del casos
    # idade_min=18: 18 anos exatos vale; um dia antes, não
    fala = ("medico", "Vou pedir uma polissonografia.")
    pid = paciente(client, h, nasc=nascimento(18), cid_codigo="G47.3")
    a = aut(falar(client, h, abrir(client, h, pid), fala), "polissonografia")
    assert crit(a, "idade_min")["status"] == "atendido"
    pid = paciente(client, h, nasc=nascimento(18, 1), cid_codigo="G47.3")
    a = aut(falar(client, h, abrir(client, h, pid), fala), "polissonografia")
    assert crit(a, "idade_min")["status"] == "faltando"


# ---------- operadora ----------
def test_operadora_desconhecida_confere_so_a_dut_e_avisa(client, auth):
    h = auth()
    pid, cid, ins = preparado(client, h)
    a = aut(ins)
    assert a["operadora"]["status"] == "nao_informada" and "Operadora não informada" in " ".join(a["avisos"])
    res = client.put(
        f"/consultations/{cid}/autorizacao/operadora", json={"operadora": "Plano Desconhecido SA"}, headers=h
    )
    assert res.status_code == 200, res.text
    a = next(x for x in res.json()["autorizacoes"] if x["id"] == "rm_joelho")
    assert a["operadora"]["status"] == "sem_regras" and "sem regras cadastradas" in a["operadora"]["aviso"]
    assert crit(a, "conservador")["status"] == "atendido"  # a DUT segue avaliada
    assert audit("consulta.autorizacao_operadora")[-1].payload_json["operadora"] == "desconhecida"
    # operadora cadastrada: exigências da operadora entram (documento e prazo)
    res = client.put(
        f"/consultations/{cid}/autorizacao/operadora", json={"operadora": "operadora exemplo a"}, headers=h
    )
    a = next(x for x in res.json()["autorizacoes"] if x["id"] == "rm_joelho")
    assert a["operadora"]["status"] == "com_regras" and a["operadora"]["prazo_dias"] == 7
    docs = {d["rotulo"]: d["status"] for d in a["documentos"]}
    assert docs["Guia de solicitação (TISS)"] == "faltando" and docs["Relatório médico com justificativa"] == "gerado"
    assert docs["Laudo da radiografia"] == "faltando"


# ---------- importador ----------
def tabela(*linhas_extra, base="rol_dut_exemplo.csv"):
    leitor = list(csv.reader(open(REF_CSV / base, encoding="utf-8")))
    cab, linhas = leitor[0], leitor[1:3]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cab)
    w.writerows(linhas)
    for extra in linhas_extra:
        w.writerow(extra(cab, linhas[0]))
    return buf.getvalue()


def com(**mud):
    def f(cab, modelo):
        linha = dict(zip(cab, modelo, strict=True))
        linha.update(mud)
        return [linha[c] for c in cab]

    return f


def test_tabela_importada_malformada_gera_relatorio_de_erros_e_nao_grava(tmp_path):
    texto = tabela(
        com(id="novo_ok"),
        com(id="Id Ruim"),  # id inválido
        com(id="datas", vigencia_inicio="31/12/2026"),  # data fora do formato
        com(id="tipo", tipo="magia"),
        com(id="regra", criterios="a::Texto::inventada:regra"),
        com(id="cidruim", cids="XYZ9"),
        com(id="idades", idade_min="80", idade_max="18"),
        com(id="rm_joelho"),  # repete um id já existente na própria tabela
    )
    texto += "so,tres,colunas\n"
    rel = autorizacao_regras.validar_regras(texto)
    assert not rel.ok and rel.aceitas == 3  # as 2 linhas do modelo + novo_ok
    campos = {(e["linha"], e["campo"]) for e in rel.erros}
    assert {(5, "id"), (6, "vigencia_inicio"), (7, "tipo"), (8, "criterios"), (9, "cids"), (10, "idade_max")} <= campos
    assert any(e["linha"] == 11 and e["campo"] == "id" for e in rel.erros)  # repetido
    assert any("colunas" in e["mensagem"] for e in rel.erros if e["linha"] == 12)
    destino = tmp_path / "rol.csv"
    with pytest.raises(ValueError):
        autorizacao_regras.gravar_importacao(texto, "regras", destino)
    assert not destino.exists()
    sem_cabecalho = autorizacao_regras.validar_regras("id,procedimento\nx,y\n")
    assert not sem_cabecalho.ok and "colunas ausentes" in sem_cabecalho.erros[0]["mensagem"]
    assert not autorizacao_regras.validar_regras("").ok


def test_csv_com_formula_e_recusado_em_qualquer_coluna():
    for mud in (
        dict(procedimento='=HYPERLINK("http://x","a")'),
        dict(fonte="@SUM(A1:A9)"),
        dict(observacao="+cmd|' /C calc'!A0"),
        dict(versao="-2+3"),
        dict(procedimento="\t=1+1"),
    ):
        rel = autorizacao_regras.validar_regras(tabela(com(id="formula", **mud)))
        assert not rel.ok, mud
        assert any("fórmula" in e["mensagem"] for e in rel.erros), (mud, rel.erros)
    ok = autorizacao_regras.validar_regras(tabela(com(id="limpo")))
    assert ok.ok


def test_importacao_valida_entra_pendente_nunca_oficial_e_vira_a_base_ativa(tmp_path, monkeypatch):
    texto = tabela(com(id="nova_regra", origem="exemplo", conferencia="conferido"))  # tenta se declarar conferida
    destino = tmp_path / "rol.csv"
    rel = autorizacao_regras.gravar_importacao(texto, "regras", destino)
    assert rel.ok and rel.publico()["pendencia"]
    base = autorizacao_regras._carregar(str(destino), str(REF_CSV / "operadoras_exemplo.csv"))
    r = base.regras["nova_regra"]
    assert r.origem == "importado" and r.conferencia == "pendente" and not r.oficial
    monkeypatch.setattr(get_settings(), "autorizacao_regras_csv", str(destino))
    autorizacao_regras.limpar_cache()
    assert set(autorizacao_regras.base_ativa().regras) == {"nova_regra"} | {
        row[0] for row in list(csv.reader(open(REF_CSV / "rol_dut_exemplo.csv", encoding="utf-8")))[1:3]
    }
    assert "pendentes de conferência" in autorizacao_regras.base_ativa().aviso()


def test_endpoint_de_validacao_so_do_gestor(client, auth):
    corpo = {"tabela": "regras", "csv": tabela(com(id="Ruim Id"))}
    assert client.post("/consult/autorizacao/validar-regras", json=corpo, headers=auth("medico")).status_code == 403
    res = client.post("/consult/autorizacao/validar-regras", json=corpo, headers=auth("gestor"))
    assert res.status_code == 200 and res.json()["ok"] is False and res.json()["erros"][0]["campo"] == "id"
    base = client.get("/consult/autorizacao/base", headers=auth("medico")).json()
    assert "EXEMPLO" in base["aviso"] and len(base["regras"]) >= 14


def test_regra_com_vigencia_vencida_nao_checa_e_nao_gera_pacote(client, auth, tmp_path, monkeypatch):
    modelo = list(csv.reader(open(REF_CSV / "rol_dut_exemplo.csv", encoding="utf-8")))
    cab = modelo[0]
    linha = dict(zip(cab, next(r for r in modelo[1:] if r[0] == "rm_joelho"), strict=True))
    linha["vigencia_fim"] = (clock.now().date() - timedelta(days=1)).isoformat()
    destino = tmp_path / "vencida.csv"
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cab)
    w.writerow([linha[c] for c in cab])
    destino.write_text(buf.getvalue(), encoding="utf-8")
    monkeypatch.setattr(get_settings(), "autorizacao_regras_csv", str(destino))
    autorizacao_regras.limpar_cache()
    h = auth()
    pid = paciente(client, h)
    cid = abrir(client, h, pid)
    a = aut(falar(client, h, cid, ("medico", RM), ("paciente", "Já fiz fisioterapia, sem melhora.")))
    assert a["situacao"] == "regra_vencida" and a["criterios"] == [] and "fora de vigência" in a["avisos"][0]
    res = gerar(client, h, cid)
    assert res.status_code == 409 and "fora de vigência" in res.json()["detail"]


# ---------- pacote de justificativa ----------
def test_pacote_leva_as_frases_com_fala_n_e_a_saida_so_depois_da_aprovacao(client, auth):
    h = auth()
    pid, cid, _ = preparado(client, h)
    res = gerar(client, h, cid)
    assert res.status_code == 200, res.text
    p = res.json()
    frases = p["pacote"]["frases"]
    assert p["status"] == "rascunho" and p["revisado"] is False and p["pacote"]["modo"] == "regras"
    assert frases[0]["texto"].startswith("Solicito autorização prévia") and frases[0]["refs"] == ["Fala/1"]
    assert any("Fala/2" in f["refs"] for f in frases) and all(f["refs"] for f in frases)
    assert any(f["refs"] == [f"Condition/{pid}-cond"] for f in frases)
    assert p["pacote"]["pendencias"] and "NÃO OFICIAL" in p["pacote"]["regra"]["fonte"]
    url = f"/consultations/{cid}/autorizacoes/{p['id']}"
    # nada sai antes da revisão do médico
    assert client.get(f"{url}/export.pdf", headers=h).status_code == 409
    assert client.get(f"{url}/export.json", headers=h).status_code == 409
    assert client.put(f"{url}/status", json={"status": "enviado"}, headers=h).status_code == 409
    ok = client.post(f"{url}/aprovar", headers=h)
    assert ok.status_code == 200 and ok.json()["revisado"] is True and ok.json()["revisado_por"]
    pdf = client.get(f"{url}/export.pdf", headers=h)
    assert (
        pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and pdf.headers["content-type"] == "application/pdf"
    )
    js = client.get(f"{url}/export.json", headers=h).json()
    assert "EXEMPLO" in js["aviso"]
    recursos = {e["resource"]["resourceType"]: e["resource"] for e in js["bundle"]["entry"]}
    ServiceRequest.model_validate(recursos["ServiceRequest"])
    Claim.model_validate(recursos["Claim"])
    assert recursos["Claim"]["use"] == "preauthorization" and recursos["Claim"]["status"] == "draft"
    assert recursos["ServiceRequest"]["status"] == "draft"
    assert f"Condition/{pid}-cond" in [i["reference"] for i in recursos["ServiceRequest"]["supportingInfo"]]
    assert not any(i["reference"].startswith("Fala/") for i in recursos["ServiceRequest"]["supportingInfo"])


def test_afirmacao_sem_fonte_ou_com_fonte_de_fora_nao_edita_nem_aprova(client, auth):
    h = auth()
    outro = paciente(client, h, nome="Maria Outra Silva")
    _, cid, _ = preparado(client, h)
    pid_ped = gerar(client, h, cid).json()["id"]
    url = f"/consultations/{cid}/autorizacoes/{pid_ped}"
    for frases in (
        [{"texto": "O paciente falhou no tratamento conservador.", "refs": []}],  # sem evidência
        [{"texto": "Inventei algo.", "refs": ["Fala/99"]}],  # fala que não existe nesta consulta
        [{"texto": "Doença de outro paciente.", "refs": [f"Condition/{outro}-cond"]}],  # evidência de outro paciente
        [],  # vazio
    ):
        res = client.put(f"{url}/justificativa", json={"frases": frases}, headers=h)
        assert res.status_code == 422 and res.json()["detail"]["problemas"], frases
    # adulteração direta no banco: a aprovação revalida e barra
    with db_session.SessionLocal() as db:
        p = db.get(AutorizacaoPrevia, pid_ped)
        pacote = dict(p.pacote_json)
        pacote["frases"] = [*pacote["frases"], {"texto": "Frase sem fonte.", "refs": []}]
        p.pacote_json = pacote
        db.commit()
    res = client.post(f"{url}/aprovar", headers=h)
    assert res.status_code == 422 and res.json()["detail"]["codigo"] == "frases_sem_fonte"
    assert audit("consulta.autorizacao_bloqueada")
    # edição válida do profissional (com a evidência) volta a poder ser aprovada
    boa = [
        {
            "texto": "Gonartrose confirmada, sem resposta ao tratamento clínico.",
            "refs": ["Fala/2", f"Condition/{_pid(cid)}-cond"],
        }
    ]
    res = client.put(f"{url}/justificativa", json={"frases": boa}, headers=h)
    assert res.status_code == 200 and res.json()["pacote"]["frases"][0]["origem"] == "profissional"
    assert client.post(f"{url}/aprovar", headers=h).status_code == 200


def _pid(cid: int) -> str:
    with db_session.SessionLocal() as db:
        from app.db.models import Consultation

        return db.get(Consultation, cid).patient_id


def test_editar_depois_de_aprovar_tira_a_aprovacao(client, auth):
    h = auth()
    _, cid, _ = preparado(client, h)
    p = gerar(client, h, cid).json()
    url = f"/consultations/{cid}/autorizacoes/{p['id']}"
    assert client.post(f"{url}/aprovar", headers=h).json()["revisado"] is True
    frases = [{"texto": f["texto"], "refs": f["refs"]} for f in p["pacote"]["frases"]][:2]
    novo = client.put(f"{url}/justificativa", json={"frases": frases}, headers=h).json()
    assert novo["revisado"] is False and novo["revisado_por"] is None
    assert client.get(f"{url}/export.pdf", headers=h).status_code == 409
    refeito = gerar(client, h, cid).json()  # regerar também zera a aprovação
    assert refeito["id"] == p["id"] and refeito["revisado"] is False


def test_pedido_aprovado_ou_enviado_nao_muda_e_so_medico_aprova(client, auth):
    hm, he = auth("medico"), auth("enfermagem")
    _, cid, _ = preparado(client, hm)
    p = gerar(client, hm, cid).json()
    url = f"/consultations/{cid}/autorizacoes/{p['id']}"
    assert client.post(f"{url}/aprovar", headers=he).status_code in (403, 404)  # enfermagem não aprova
    assert client.post(f"{url}/aprovar", headers=hm).status_code == 200
    env = client.put(f"{url}/status", json={"status": "enviado", "protocolo": "PROT-123"}, headers=he)
    assert env.status_code == 200 and env.json()["status"] == "enviado" and env.json()["protocolo"] == "PROT-123"
    # enviado: nem edita, nem regera, nem volta
    assert (
        client.put(
            f"{url}/justificativa", json={"frases": [{"texto": "x", "refs": ["Fala/1"]}]}, headers=hm
        ).status_code
        == 409
    )
    assert gerar(client, hm, cid).status_code == 409
    assert client.put(f"{url}/status", json={"status": "rascunho"}, headers=hm).status_code == 409
    assert client.put(f"{url}/status", json={"status": "aprovado"}, headers=hm).status_code == 200
    # aprovado é final
    assert client.put(f"{url}/status", json={"status": "negado"}, headers=hm).status_code == 409
    assert client.put(f"{url}/status", json={"status": "rascunho"}, headers=hm).status_code == 409
    assert client.put(f"{url}/justificativa", json={"frases": []}, headers=hm).status_code == 409
    assert client.post(f"{url}/aprovar", headers=hm).status_code == 409
    assert gerar(client, hm, cid).status_code == 409
    # pedido negado volta a rascunho para novo recurso, com nova revisão
    _, cid2, _ = preparado(client, hm)
    q = gerar(client, hm, cid2).json()
    u2 = f"/consultations/{cid2}/autorizacoes/{q['id']}"
    client.post(f"{u2}/aprovar", headers=hm)
    client.put(f"{u2}/status", json={"status": "enviado"}, headers=hm)
    assert client.put(f"{u2}/status", json={"status": "negado"}, headers=hm).json()["status"] == "negado"
    volta = client.put(f"{u2}/status", json={"status": "rascunho"}, headers=hm).json()
    assert volta["status"] == "rascunho" and volta["revisado"] is False


def test_trilha_no_audit_log_sem_texto_nem_pii(client, auth):
    h = auth()
    pid, cid, _ = preparado(client, h)
    p = gerar(client, h, cid).json()
    url = f"/consultations/{cid}/autorizacoes/{p['id']}"
    client.post(f"{url}/aprovar", headers=h)
    client.put(f"{url}/status", json={"status": "enviado", "protocolo": "PROT-9"}, headers=h)
    client.get(f"{url}/export.pdf", headers=h)
    linhas = audit()
    acoes = [a.action for a in linhas]
    for esperado in (
        "consulta.autorizacao_gerada",
        "consulta.autorizacao_aprovada",
        "consulta.autorizacao_status",
        "consulta.autorizacao_exportada",
    ):
        assert esperado in acoes
    bruto = " ".join(str(a.payload_json) for a in linhas)
    assert "Jorge" not in bruto and "fisioterapia" not in bruto and "Solicito" not in bruto and "PROT-9" not in bruto
    gerada = next(a for a in linhas if a.action == "consulta.autorizacao_gerada")
    assert gerada.payload_json["regra"] == "rm_joelho" and gerada.payload_json["origem_regra"] == "exemplo"
    assert all(a.organization_id for a in linhas)


# ---------- isolamento ----------
def test_consulta_sem_vinculo_e_403_em_todas_as_rotas_novas(client, auth):
    dono = auth()
    _, cid, _ = preparado(client, dono)
    p = gerar(client, dono, cid).json()
    outro = segundo_medico(client)
    base = f"/consultations/{cid}"
    for verbo, url, corpo in (
        ("get", f"{base}/autorizacoes", None),
        ("put", f"{base}/autorizacao/operadora", {"operadora": "x"}),
        ("post", f"{base}/autorizacoes", {"regra_id": "rm_joelho"}),
        ("put", f"{base}/autorizacoes/{p['id']}/justificativa", {"frases": []}),
        ("post", f"{base}/autorizacoes/{p['id']}/aprovar", None),
        ("put", f"{base}/autorizacoes/{p['id']}/status", {"status": "enviado"}),
        ("get", f"{base}/autorizacoes/{p['id']}/export.pdf", None),
        ("get", f"{base}/autorizacoes/{p['id']}/export.json", None),
    ):
        res = client.request(verbo, url, json=corpo, headers=outro)
        assert res.status_code == 403, (url, res.status_code)


def test_outra_organizacao_nao_ve_consulta_nem_pedido(client, auth):
    from tests.test_multiorg import cadastro_completo

    ha = auth()
    _, cid, _ = preparado(client, ha)
    p = gerar(client, ha, cid).json()
    hb, _ = cadastro_completo(client)
    base = f"/consultations/{cid}"
    for verbo, url, corpo in (
        ("get", f"{base}/autorizacoes", None),
        ("post", f"{base}/autorizacoes", {"regra_id": "rm_joelho"}),
        ("post", f"{base}/autorizacoes/{p['id']}/aprovar", None),
        ("get", f"{base}/autorizacoes/{p['id']}/export.json", None),
    ):
        assert client.request(verbo, url, json=corpo, headers=hb).status_code == 404, url
    with db_session.SessionLocal() as db:
        assert db.get(AutorizacaoPrevia, p["id"]).status == "rascunho"


def test_pedido_de_outra_consulta_do_mesmo_medico_e_404(client, auth):
    h = auth()
    _, cid1, _ = preparado(client, h)
    _, cid2, _ = preparado(client, h)
    p = gerar(client, h, cid1).json()
    assert client.post(f"/consultations/{cid2}/autorizacoes/{p['id']}/aprovar", headers=h).status_code == 404
    assert client.get(f"/consultations/{cid2}/autorizacoes/{p['id']}/export.pdf", headers=h).status_code == 404


def test_evidencia_de_outro_paciente_ou_de_outra_consulta_nao_conta(client, auth):
    h = auth()
    # o diagnóstico existe só no paciente B; a fala de tratamento só na consulta anterior do paciente A
    paciente(client, h, nome="Paciente com diagnostico")
    pid_a = paciente(client, h, cid_codigo=None, nome="Paciente sem diagnostico")
    antiga = abrir(client, h, pid_a)
    falar(client, h, antiga, ("paciente", "Já fiz fisioterapia e tomei anti-inflamatório."))
    nova = abrir(client, h, pid_a)
    a = aut(falar(client, h, nova, ("medico", RM)))
    assert crit(a, "cid")["status"] == "faltando" and crit(a, "conservador")["status"] == "faltando"
    assert a["situacao"] == "incompleta"


# ---------- IA só reescreve ----------
def test_reescrita_da_ia_com_fato_novo_ou_ref_de_fora_e_descartada():
    orig = [
        {"texto": "Solicito autorização prévia para RM de joelho.", "refs": ["Fala/1"]},
        {"texto": "Paciente com 61 anos. CID M17.", "refs": ["Patient/p1"]},
    ]
    boa = [{"texto": "Solicito a autorização da RM de joelho.", "refs": ["Fala/1"]}]
    assert autorizacao.reescrita_segura(orig, boa)[0]["origem"] == "ia"
    for ruim in (
        [{"texto": "Solicito a RM.", "refs": ["Fala/7"]}],  # ref que não veio
        [{"texto": "Solicito a RM.", "refs": []}],  # sem fonte
        [{"texto": "Paciente com 70 anos.", "refs": ["Patient/p1"]}],  # número novo
        [{"texto": "Paciente com CID C50.", "refs": ["Patient/p1"]}],  # CID novo
        [],
    ):
        assert autorizacao.reescrita_segura(orig, ruim) is None, ruim


def test_ia_real_que_inventa_cai_nas_frases_das_regras(client, auth, monkeypatch):
    from app.consult import autorizacao_service as svc
    from app.llm.client import LLMResult

    h = auth()
    _, cid, _ = preparado(client, h)

    def falso(prompt, variables, schema, mock, effort=None, rules_only=False):
        saida = schema.model_validate(
            {"frases": [{"texto": "Paciente com 99 anos e tumor.", "refs": variables["frases"][0]["refs"]}]}
        )
        return LLMResult(output=saida, model="m", prompt_version="1", mode="real")

    monkeypatch.setattr(svc, "generate_structured", falso)
    p = gerar(client, h, cid).json()
    assert p["pacote"]["modo"] == "regras" and any("descartada" in a for a in p["pacote"]["avisos"])
    assert "99" not in " ".join(f["texto"] for f in p["pacote"]["frases"])


# ---------- migração ----------
def test_migracao_c306_sobe_e_desce(tmp_path, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    raiz = Path(__file__).resolve().parents[1]
    cfg = Config(str(raiz / "alembic.ini"))
    cfg.set_main_option("script_location", str(raiz / "app/db/migrations"))
    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    command.upgrade(cfg, "c306")
    cols = {c["name"] for c in inspect(create_engine(url)).get_columns("autorizacoes_previas")}
    assert {"consultation_id", "regra_id", "status", "pacote_json", "revisado_por", "organization_id"} <= cols
    command.downgrade(cfg, "c305")
    assert "autorizacoes_previas" not in inspect(create_engine(url)).get_table_names()
    assert date.today().year >= 2026
