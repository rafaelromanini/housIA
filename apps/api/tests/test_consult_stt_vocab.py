"""#38 transcrição que conhece o paciente: vocabulário no prompt, correção por similaridade ("corrigido"), confiança
por trecho, "confirmar o que foi ouvido" antes do alerta crítico e WER do cen6 sintético. Motor falso, sem rede."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.consult import service as consult_service
from app.consult import stt, stt_stream, stt_vocab
from tests.test_consult import SCRIPT, say, start

RAIZ = Path(__file__).resolve().parents[3]
BLOCO = stt_stream.TAXA


@pytest.fixture(autouse=True)
def _isolado(monkeypatch, tmp_path):
    consult_service.clear_chart_cache()
    s = get_settings()
    monkeypatch.setattr(s, "stt_engine", "fake")
    monkeypatch.setattr(s, "vocab_institucional_path", tmp_path / "vocab.csv")
    (tmp_path / "vocab.csv").write_text("termo,tipo,observacao\nHbA1c,exame,\nAmbulatório Sol,servico,\n", "utf-8")
    yield
    consult_service.clear_chart_cache()


def motor_falso(monkeypatch, vocab, probs=None, logprob=None):
    m = stt_stream.FakeMotor(vocab, probs, logprob)
    monkeypatch.setattr(stt_stream, "motor", lambda: m)
    return m


def falar(client, h, cid, frase, vocab, falante="paciente"):
    """Frase (palavras do vocab) → áudio sintético → WebSocket → mensagens recebidas."""
    pcm = stt_stream.sintetizar([frase], vocab=vocab)
    msgs = []
    with client.websocket_connect(f"/ws/consultations/{cid}/audio?falante={falante}", headers=h) as ws:
        assert ws.receive_json()["tipo"] == "pronto"
        for i in range(0, len(pcm), BLOCO):
            ws.send_bytes(pcm[i : i + BLOCO])
        ws.send_text(json.dumps({"tipo": "fim"}))
        while True:
            m = ws.receive_json()
            msgs.append(m)
            if m["tipo"] in ("fim", "erro"):
                return msgs


def ultimo_servidor(client, h, cid):
    got = client.get(f"/consultations/{cid}", headers=h).json()
    return [s for s in got["segmentos"] if s["origem"] == "servidor"][-1]


# ---------- correção por similaridade ----------
def test_glibem_clamida_vira_glibenclamida_com_marca_de_corrigido(client, auth, monkeypatch):
    vocab = sorted({"tomo", "glibem", "clamida", "de", "manha"})
    motor_falso(monkeypatch, vocab)
    h = auth()
    cid = start(client, h)["id"]
    msgs = falar(client, h, cid, "tomo glibem clamida de manha", vocab)
    final = next(m for m in msgs if m["tipo"] == "final")
    assert "glibenclamida" in final["texto"].lower() and "glibem" not in final["texto"]
    seg = ultimo_servidor(client, h, cid)
    assert [(c["de"], c["para"].lower()) for c in seg["corrigidos"]] == [("glibem clamida", "glibenclamida")]
    assert seg["texto"].startswith("tomo ") and seg["texto"].endswith(" de manha")  # só o termo mudou


@pytest.mark.parametrize(
    ("fala", "esperado"),
    [
        ("glibem clamida", "Glibenclamida"),
        ("tomo metformina e glibenclamida", None),  # já certos
        ("tomo metfornina de manhã", "metformina"),
        ("a dipirona me deu alergia", None),
        ("tomando o remédio da pressão desde ontem", None),
        ("consulta de retorno com a doutora", None),
        ("minha filha trouxe os exames", None),
        ("tenho tontura quando levanto", None),
        ("ela comprou paracetamol", None),
        ("glicose alta no exame", None),
        ("pressão arterial controlada", None),
    ],
)
def test_correcao_nao_inventa_remedio(fala, esperado):
    novo, cor = stt_vocab.corrigir(fala)
    if esperado is None:
        assert novo == fala and cor == []
    else:
        assert esperado.lower() in novo.lower() and cor


def test_correcao_usa_vocabulario_do_paciente_com_limiar_menor():
    # nome fora dos catálogos, uma letra trocada (ratio 0,83: abaixo do limiar de 0,88 do catálogo e acima do 0,82
    # do paciente): só o prontuário/vocabulário institucional o conhece
    assert "zolmax" not in stt_vocab._catalogo()
    novo, cor = stt_vocab.corrigir("uso zolmex", ["Zolmax 50 mg"])
    assert novo == "uso zolmax" and cor[0].de == "zolmex" and 0.82 <= cor[0].razao < stt_vocab.RAZAO_MINIMA
    assert stt_vocab.corrigir("uso zolmex") == ("uso zolmex", [])


def test_correcao_com_duas_letras_de_diferenca_nunca_vale_nem_com_o_vocabulario_do_paciente():
    # "cardiotase" → "cardiotrace" (ratio 0,86, 2 edições) deixou de ser corrigido de propósito: com 2 edições a
    # troca vira "prednisolona" → "prednisona" para quem toma prednisona, e a conduta muda
    assert stt_vocab.corrigir("uso cardiotase", ["Cardiotrace 50 mg"]) == ("uso cardiotase", [])
    assert stt_vocab.corrigir("uso prednisolona", ["Prednisona 20 mg"]) == ("uso prednisolona", [])


def test_palavra_seguinte_nao_e_engolida_pelo_par():
    novo, _ = stt_vocab.corrigir("tomo glibenclamida de manhã")
    assert novo == "tomo glibenclamida de manhã"


# ---------- vocabulário no prompt ----------
def test_vocabulario_institucional_e_do_paciente_chegam_ao_prompt_do_stt_no_fluxo(client, auth, monkeypatch):
    vocab = ["tomo", "metformina"]
    m = motor_falso(monkeypatch, vocab)
    h = auth()
    cid = start(client, h)["id"]
    falar(client, h, cid, "tomo metformina", vocab)
    enviado = m.vocabularios[-1]
    assert "HbA1c" in enviado and "Ambulatório Sol" in enviado  # institucional
    assert "Glibenclamida" in enviado or "glibenclamida" in enviado.lower()  # medicamento do prontuário (cen6)
    assert enviado.index("lib") < enviado.index("HbA1c")  # o paciente vem antes do institucional
    assert len(enviado) <= stt_vocab.MAX_PROMPT_CHARS + 200


def test_prompt_vai_no_campo_prompt_do_post_audio_com_verbose_json(client, auth, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "stt_engine", "")
    monkeypatch.setattr(s, "stt_url", "http://stt.local/v1/audio/transcriptions")
    h = auth("medico")
    cid = start(client, h)["id"]
    captured = {}

    def fake_post(url, **kw):
        captured.update(kw)
        corpo = {
            "text": "tomo glibem clamida",
            "segments": [{"avg_logprob": -0.1}],
            "words": [{"word": "tomo", "start": 0, "end": 0.3, "probability": 0.95}],
        }
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: corpo)

    monkeypatch.setattr(stt.httpx, "post", fake_post)
    res = client.post(
        f"/consultations/{cid}/audio?falante=paciente", content=b"\x00\x01", headers=h | {"Content-Type": "audio/webm"}
    )
    assert res.status_code == 200, res.text
    assert "HbA1c" in captured["data"]["prompt"] and captured["data"]["response_format"] == "verbose_json"
    nova = res.json()["novos"][0]
    assert "glibenclamida" in nova["texto"].lower() and nova["corrigidos"]
    assert nova["confianca"] == pytest.approx(0.905, abs=0.01)


def org_de(client, h) -> int:
    return client.get("/auth/me", headers=h).json()["organizacao"]["id"]


def test_endpoint_do_vocabulario_so_o_gestor_edita(client, auth):
    corpo = {"itens": [{"termo": "Clínica Aurora", "tipo": "servico"}, {"termo": "clínica aurora"}, {"termo": " "}]}
    assert client.put("/consult/vocabulario", json=corpo, headers=auth("medico")).status_code == 403
    res = client.put("/consult/vocabulario", json=corpo, headers=auth("gestor"))
    assert res.status_code == 200 and [i["termo"] for i in res.json()["itens"]] == ["Clínica Aurora"]
    assert [i["termo"] for i in client.get("/consult/vocabulario", headers=auth("medico")).json()["itens"]] == [
        "Clínica Aurora"
    ]
    assert "Clínica Aurora" in stt_vocab.prompt(None, "", org_de(client, auth("medico")))
    audit = client.get("/audit", headers=auth("gestor")).json()
    linhas = audit if isinstance(audit, list) else audit.get("itens", audit.get("items", []))
    assert any(a.get("action") == "consulta.vocabulario" for a in linhas)


# ---------- confiança e "confirmar o que foi ouvido" ----------
def fala_ibuprofeno(client, h, monkeypatch, probs=None, logprob=None, frase="tomo ibuprofeno 600 mg"):
    vocab = sorted(set(frase.split()))
    motor_falso(monkeypatch, vocab, probs, logprob)
    cid = start(client, h)["id"]
    say(client, h, cid, SCRIPT[:12])  # a próxima fala será a 13
    falar(client, h, cid, frase, vocab)
    return cid


def alerta_ibuprofeno(client, h, cid):
    ins = client.get(f"/consultations/{cid}", headers=h).json()["insights"]
    return [
        m for m in ins["sugestoes_medicamentos"] if m["tipo"] == "suspender" and m["texto"].startswith(("Ibup", "Conf"))
    ]


def test_medicamento_de_baixa_confianca_pede_confirmacao_antes_do_alerta_critico(client, auth, monkeypatch):
    h = auth()
    cid = fala_ibuprofeno(client, h, monkeypatch, probs={"ibuprofeno": 0.3})
    seg = ultimo_servidor(client, h, cid)
    assert seg["n"] == 13 and seg["palavras_baixas"] == ["ibuprofeno"] and seg["confirmar"] == ["ibuprofeno"]
    assert seg["confianca"] < 0.99
    alertas = [m for m in alerta_ibuprofeno(client, h, cid) if "Fala/13" in m["refs"]]
    assert alertas, "o alerta do ibuprofeno deveria existir"
    assert all(m["severidade"] == "critico" and m["confirmar_ouvido"] for m in alertas)  # nunca rebaixa
    assert all(m["texto"].startswith("Confirmar o que foi ouvido") for m in alertas)
    # confirmar a fala devolve a gravidade da regra
    res = client.post(f"/consultations/{cid}/segments/13/confirm", headers=h)
    assert res.status_code == 200, res.text
    assert ultimo_servidor(client, h, cid)["confirmado_por"]
    depois = [m for m in alerta_ibuprofeno(client, h, cid) if "Fala/13" in m["refs"]]
    assert any(m["severidade"] == "critico" and not m["texto"].startswith("Confirmar") for m in depois)
    assert not any(m["confirmar_ouvido"] for m in depois)
    assert client.post(f"/consultations/{cid}/segments/13/confirm", headers=h).status_code == 409


def test_dose_de_baixa_confianca_exige_confirmacao(client, auth, monkeypatch):
    h = auth()
    cid = fala_ibuprofeno(client, h, monkeypatch, probs={"600": 0.2})
    seg = ultimo_servidor(client, h, cid)
    assert seg["confirmar"] == ["600"] and seg["palavras_baixas"] == ["600"]
    ins = client.get(f"/consultations/{cid}", headers=h).json()["insights"]
    assert any(m["confirmar_ouvido"] and m["severidade"] == "critico" for m in ins["sugestoes_medicamentos"])


def test_confianca_alta_ou_palavra_comum_baixa_nao_atrapalha(client, auth, monkeypatch):
    h = auth()
    cid = fala_ibuprofeno(client, h, monkeypatch, probs={"tomo": 0.2, "mg": 0.99})
    seg = ultimo_servidor(client, h, cid)
    assert seg["palavras_baixas"] == ["tomo"] and seg["confirmar"] == []  # sublinha, mas não bloqueia
    ins = client.get(f"/consultations/{cid}", headers=h).json()["insights"]
    assert any(m["severidade"] == "critico" and "Fala/13" in m["refs"] for m in ins["sugestoes_medicamentos"])
    assert not any(m["confirmar_ouvido"] for m in ins["sugestoes_medicamentos"])


def test_avg_logprob_baixo_marca_as_palavras_criticas_do_trecho(client, auth, monkeypatch):
    h = auth()
    cid = fala_ibuprofeno(client, h, monkeypatch, logprob=-2.0)
    seg = ultimo_servidor(client, h, cid)
    assert seg["confianca"] == pytest.approx(0.135, abs=0.01)
    assert {"ibuprofeno", "600", "mg"} <= set(seg["confirmar"])


def test_corrigir_o_texto_dispensa_a_confirmacao(client, auth, monkeypatch):
    h = auth()
    cid = fala_ibuprofeno(client, h, monkeypatch, probs={"ibuprofeno": 0.3})
    res = client.patch(f"/consultations/{cid}/segments/13", json={"texto": "tomo ibuprofeno 400 mg"}, headers=h)
    assert res.status_code == 200, res.text
    assert ultimo_servidor(client, h, cid)["confirmar"] == []


def test_alergia_dita_com_baixa_confianca_pede_confirmacao():
    aval = stt_vocab.avaliar(
        "tenho alergia a dipirona", [("tenho", 0.9), ("alergia", 0.9), ("a", 0.9), ("dipirona", 0.2)], None, []
    )
    assert aval.palavras_baixas == ["dipirona"] and aval.confirmar == ["dipirona"]
    conversa = stt_vocab.avaliar("bom dia doutora", [("bom", 0.2), ("dia", 0.9), ("doutora", 0.9)], None, [])
    assert conversa.palavras_baixas == ["bom"] and conversa.confirmar == []


def test_cliente_nao_forja_confianca_em_fala_digitada(client, auth):
    h = auth()
    cid = start(client, h)["id"]
    res = client.post(
        f"/consultations/{cid}/segments",
        json={"segmentos": [{"falante": "paciente", "texto": "tomo ibuprofeno", "confirmar": ["ibuprofeno"]}]},
        headers=h,
    )
    assert res.status_code == 200 and res.json()["novos"][0]["confirmar"] == []


# ---------- WER ----------
def _carrega_wer():
    spec = importlib.util.spec_from_file_location("wer_script", RAIZ / "scripts" / "wer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_wer_do_cen6_sintetico_abaixo_do_limite_configurado():
    wer = _carrega_wer()
    res = wer.medir(12)
    assert res["palavras_referencia"] >= 100
    assert res["wer"] <= get_settings().stt_wer_max
    assert wer.wer(["a", "b", "c", "d"], ["a", "x", "c"]) == pytest.approx(0.5)  # 1 troca + 1 remoção
    assert wer.wer([], []) == 0.0


def test_script_wer_falha_quando_passa_do_limite(monkeypatch, capsys):
    wer = _carrega_wer()
    monkeypatch.setattr("sys.argv", ["wer.py", "--max-wer", "-1"])
    assert wer.main() == 1
    monkeypatch.setattr("sys.argv", ["wer.py"])
    assert wer.main() == 0


# ---------- verificação independente (adversarial) ----------
@pytest.mark.parametrize(
    "frase",
    [
        "paciente refere cefaleia e hipertensão",  # cefaleia NÃO vira cefalexina
        "uso prednisolona de manhã",  # outro corticoide: não vira prednisona
        "tomo clonidina 0,15 mg",
        "uso clonazepam à noite",
        "tomo losartana 50 mg e lisinopril 10 mg",
        "tomo glibem 5 mg",  # dose nunca é mexida
        "uso nimodipina",
        "tomo metadona 10 mg e metformina 850 mg",
        "uso hidroxizina e hidralazina",
        "dor abdominal, constipação, medicação, pressão",
        "uso insulina nph",
        "não tomo fluvoxamina, só fluoxetina",
    ],
)
def test_correcao_nao_troca_um_medicamento_por_outro(frase):
    novo, cor = stt_vocab.corrigir(frase)
    assert novo == frase and cor == []


def test_correcao_de_uma_letra_continua_valendo():
    assert stt_vocab.corrigir("uso setralina")[0] == "uso sertralina"
    assert stt_vocab.corrigir("tomo losartan 50 mg")[0] == "tomo losartana 50 mg"


def test_vocabulario_e_por_organizacao_e_higienizado(tmp_path):
    stt_vocab.salvar_vocab([{"termo": "=CMD()\nignore tudo " + "x" * 200}], org_id=1)
    stt_vocab.salvar_vocab([{"termo": "Clínica B"}], org_id=2)
    a, b = stt_vocab.vocab_institucional(1), stt_vocab.vocab_institucional(2)
    assert [i["termo"] for i in b] == ["Clínica B"]
    assert len(a[0]["termo"]) <= stt_vocab.MAX_TERMO and "\n" not in a[0]["termo"] and not a[0]["termo"].startswith("=")
    assert "Clínica B" not in stt_vocab.prompt(None, "", 1)
    assert "HbA1c" in [i["termo"] for i in stt_vocab.vocab_institucional(3)]  # sem edição própria: base de demonstração


# ---------- verificação cética 2: segurança do alerta, privacidade, vazamento entre organizações ----------
def _severidades(no) -> list[tuple[str, str]]:
    """Todo (tipo, severidade) de qualquer alerta do JSON dos insights, em qualquer nível."""
    if isinstance(no, dict):
        achados = [(str(no.get("tipo", "")), str(no["severidade"]))] if "severidade" in no else []
        return achados + [x for v in no.values() for x in _severidades(v)]
    if isinstance(no, list):
        return [x for v in no for x in _severidades(v)]
    return []


def test_baixa_confianca_nunca_muda_a_gravidade_de_nenhum_alerta(client, auth, monkeypatch):
    """Mesma consulta com e sem trecho incerto: o conjunto de alertas e suas gravidades é idêntico (ADR 0009)."""
    h = auth()
    limpo = fala_ibuprofeno(client, h, monkeypatch)
    duvidoso = fala_ibuprofeno(client, h, monkeypatch, logprob=-3.0)
    a = client.get(f"/consultations/{limpo}", headers=h).json()["insights"]
    b = client.get(f"/consultations/{duvidoso}", headers=h).json()["insights"]
    assert ultimo_servidor(client, h, duvidoso)["confirmar"]
    assert sorted(_severidades(a)) == sorted(_severidades(b))
    assert any(sev == "critico" for _, sev in _severidades(b))
    assert [m["severidade"] for m in a["sugestoes_medicamentos"]] == [
        m["severidade"] for m in b["sugestoes_medicamentos"]
    ]
    assert any(m["confirmar_ouvido"] for m in b["sugestoes_medicamentos"])
    assert not any(m["confirmar_ouvido"] for m in a["sugestoes_medicamentos"])


def test_nome_trocado_pela_correcao_pede_confirmacao_e_guarda_o_que_foi_ouvido(client, auth, monkeypatch):
    vocab = sorted({"tomo", "glibem", "clamida", "de", "manha"})
    motor_falso(monkeypatch, vocab)
    h = auth()
    cid = start(client, h)["id"]
    falar(client, h, cid, "tomo glibem clamida de manha", vocab)
    seg = ultimo_servidor(client, h, cid)
    assert seg["corrigidos"][0]["de"] == "glibem clamida"  # o ouvido fica guardado
    assert [c.lower() for c in seg["confirmar"]] == ["glibenclamida"]
    ins = client.get(f"/consultations/{cid}", headers=h).json()["insights"]
    citam = [m for m in ins["sugestoes_medicamentos"] if f"Fala/{seg['n']}" in m["refs"]]
    assert all(m["confirmar_ouvido"] for m in citam)
    assert client.post(f"/consultations/{cid}/segments/{seg['n']}/confirm", headers=h).status_code == 200


def test_correcao_preserva_a_negacao_e_nao_mexe_na_dose():
    novo, cor = stt_vocab.corrigir("não tomo glibem clamida 5 mg")
    assert novo.startswith("não tomo ") and novo.endswith(" 5 mg") and "glibenclamida" in novo.lower()
    assert [c.de for c in cor] == ["glibem clamida"]


def test_nenhum_nome_do_catalogo_e_alterado_pela_correcao():
    """Idempotência: nome conhecido (inclusive de duas palavras) passa intacto, com e sem vocabulário."""
    nomes = sorted(set(stt_vocab._catalogo().values()) | set(stt_vocab._catalogo()))
    assert len(nomes) > 100
    for nome in nomes:
        frase = f"uso {nome} de manhã"
        assert stt_vocab.corrigir(frase) == (frase, []), nome
        assert stt_vocab.corrigir(frase.lower(), [nome]) == (frase.lower(), []), nome


def test_alergeno_em_lista_longa_de_baixa_confianca_pede_confirmacao():
    texto = "tenho alergia a penicilina, sulfa, látex e dipirona"
    palavras = [(p, 0.2 if p == "dipirona" else 0.95) for p in texto.replace(",", "").split()]
    assert stt_vocab.avaliar(texto, palavras, None, []).confirmar == ["dipirona"]
    # alérgeno antes da palavra "alergia"; e palavra de outra oração não conta
    assert stt_vocab.avaliar("dipirona me dá alergia", [("dipirona", 0.2)], None, []).confirmar == ["dipirona"]
    assert stt_vocab.avaliar("tomo dieta. alergia não tenho", [("dieta", 0.2)], None, []).confirmar == []


def test_alergia_vem_primeiro_no_prompt_e_sobrevive_ao_limite():
    rec = SimpleNamespace(
        real_allergies=[{"code": {"text": "Ioexol"}}],
        active_meds=[SimpleNamespace(name=f"Medicamento{i:03d} 10 mg") for i in range(60)],
        active_conditions=[{"code": {"text": f"Diagnóstico longo número {i:03d}"}} for i in range(60)],
    )
    p = stt_vocab.prompt(rec)
    assert p.startswith("Ioexol") and len(p) <= stt_vocab.MAX_PROMPT_CHARS + 200


def test_confianca_vale_tambem_em_outro_idioma_sem_correcao():
    item = consult_service._item_servidor(
        None, "paciente", "tomo ibuprofeno 600 mg", None, [("ibuprofeno", 0.2)], None, "es-MX"
    )
    assert item.confirmar == ["ibuprofeno"] and item.corrigidos == []


def test_vocabulario_editado_no_mesmo_instante_nao_serve_cache_velho():
    import os

    stt_vocab.salvar_vocab([{"termo": "Alfa"}], org_id=7)
    arq = stt_vocab.caminho_vocab(7)
    antes = arq.stat()
    assert [i["termo"] for i in stt_vocab.vocab_institucional(7)] == ["Alfa"]
    arq.write_text("termo,tipo,observacao\nBeta Gama,,\n", "utf-8")  # outro processo editou; mesmo mtime (FS de 1-2 s)
    os.utime(arq, ns=(antes.st_atime_ns, antes.st_mtime_ns))
    assert [i["termo"] for i in stt_vocab.vocab_institucional(7)] == ["Beta Gama"]


def test_vocabulario_de_uma_organizacao_nao_aparece_para_outra_pela_api(client, auth):
    from tests.test_multiorg import cadastro_completo

    client.put("/consult/vocabulario", json={"itens": [{"termo": "Clínica Aurora"}]}, headers=auth("gestor"))
    hb, _ = cadastro_completo(
        client,
        "gestora@hospital.test",
        organizacao_tipo="hospital",
        conselho=None,
        conselho_numero=None,
        uf=None,
        organizacao_nome="Hospital Sul",
    )
    assert "Clínica Aurora" not in json.dumps(client.get("/consult/vocabulario", headers=hb).json())
    assert client.put("/consult/vocabulario", json={"itens": [{"termo": "Sul"}]}, headers=hb).status_code == 200
    assert [i["termo"] for i in client.get("/consult/vocabulario", headers=auth("medico")).json()["itens"]] == [
        "Clínica Aurora"
    ]  # a edição da outra organização não alterou a primeira


def test_confirmar_fala_de_outra_organizacao_e_404_e_o_audit_nao_tem_texto(client, auth, monkeypatch):
    from tests.test_multiorg import cadastro_completo

    h = auth()
    cid = fala_ibuprofeno(client, h, monkeypatch, probs={"ibuprofeno": 0.3})
    hb, _ = cadastro_completo(client, "dra.bia@clinica.test")
    assert client.post(f"/consultations/{cid}/segments/13/confirm", headers=hb).status_code == 404
    assert ultimo_servidor(client, h, cid)["confirmado_por"] is None  # nada mudou
    assert client.post(f"/consultations/{cid}/segments/13/confirm", headers=h).status_code == 200
    audit = client.get("/audit", headers=auth("gestor")).json()
    linhas = audit if isinstance(audit, list) else audit.get("itens", audit.get("items", []))
    registrado = json.dumps([a for a in linhas if a.get("action", "").startswith("consulta.")], ensure_ascii=False)
    assert "consulta.fala_confirmada" in registrado
    assert "ibuprofeno" not in registrado.lower()


def test_expurgo_apaga_palavras_baixas_corrigidos_e_a_conferir(client, auth, monkeypatch):
    from app.consult import purge
    from app.db import session as db_session
    from app.db.models import Consultation

    h = auth()
    cid = fala_ibuprofeno(client, h, monkeypatch, logprob=-3.0)
    with db_session.SessionLocal() as db:
        seg = db.get(Consultation, cid).segments_json[-1]
        assert seg["palavras_baixas"] and seg["confirmar"]  # tudo o que foi dito está nessas listas
    client.post(f"/consultations/{cid}/finish", headers=h)
    monkeypatch.setattr(get_settings(), "transcript_retention_days", "consulta=0")
    assert purge.main() == 1
    with db_session.SessionLocal() as db:
        seg = db.get(Consultation, cid).segments_json[-1]
    assert seg["texto"] == "" and seg["palavras_baixas"] == [] and seg["confirmar"] == [] and seg["corrigidos"] == []
    assert "ibuprofeno" not in json.dumps(client.get(f"/consultations/{cid}", headers=h).json()["segmentos"])
