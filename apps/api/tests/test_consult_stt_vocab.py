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
    # nome fora dos catálogos (ratio 0,86): só o prontuário/vocabulário institucional o conhece
    novo, cor = stt_vocab.corrigir("uso cardiotase", ["Cardiotrace 50 mg"])
    assert novo == "uso cardiotrace" and cor[0].de == "cardiotase"
    assert stt_vocab.corrigir("uso cardiotase") == ("uso cardiotase", [])


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


def test_endpoint_do_vocabulario_so_o_gestor_edita(client, auth):
    corpo = {"itens": [{"termo": "Clínica Aurora", "tipo": "servico"}, {"termo": "clínica aurora"}, {"termo": " "}]}
    assert client.put("/consult/vocabulario", json=corpo, headers=auth("medico")).status_code == 403
    res = client.put("/consult/vocabulario", json=corpo, headers=auth("gestor"))
    assert res.status_code == 200 and [i["termo"] for i in res.json()["itens"]] == ["Clínica Aurora"]
    assert [i["termo"] for i in client.get("/consult/vocabulario", headers=auth("medico")).json()["itens"]] == [
        "Clínica Aurora"
    ]
    assert "Clínica Aurora" in stt_vocab.prompt(None)
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
    assert all(m["severidade"] == "atencao" and m["confirmar_ouvido"] for m in alertas)
    assert all(m["texto"].startswith("Confirmar o que foi ouvido") for m in alertas)
    # confirmar a fala devolve a gravidade da regra
    res = client.post(f"/consultations/{cid}/segments/13/confirm", headers=h)
    assert res.status_code == 200, res.text
    assert ultimo_servidor(client, h, cid)["confirmado_por"]
    depois = [m for m in alerta_ibuprofeno(client, h, cid) if "Fala/13" in m["refs"]]
    assert any(m["severidade"] == "critico" and not m["texto"].startswith("Confirmar") for m in depois)
    assert client.post(f"/consultations/{cid}/segments/13/confirm", headers=h).status_code == 409


def test_dose_de_baixa_confianca_exige_confirmacao(client, auth, monkeypatch):
    h = auth()
    cid = fala_ibuprofeno(client, h, monkeypatch, probs={"600": 0.2})
    seg = ultimo_servidor(client, h, cid)
    assert seg["confirmar"] == ["600"] and seg["palavras_baixas"] == ["600"]
    ins = client.get(f"/consultations/{cid}", headers=h).json()["insights"]
    assert any(m["confirmar_ouvido"] and m["severidade"] == "atencao" for m in ins["sugestoes_medicamentos"])
    assert not any(m["severidade"] == "critico" and "Fala/13" in m["refs"] for m in ins["sugestoes_medicamentos"])


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
