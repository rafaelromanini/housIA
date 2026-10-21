"""#11 transcrição em fluxo: WebSocket de áudio PCM → parcial/final, janela deslizante sem duplicar nem cortar palavra,
nada de áudio em disco. O motor é o falso e determinístico (tons por palavra); o caminho HTTP usa httpx simulado."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from app.config import get_settings
from app.consult import service as consult_service
from app.consult import stt, stt_stream
from tests.conftest import CONSENT
from tests.test_consult import start

WAV = Path(__file__).parent / "fixtures" / "fala_sintetica_glibenclamida.wav"
BLOCO = stt_stream.TAXA * 2 // 2  # 500 ms de PCM int16 = 16000 bytes


@pytest.fixture(autouse=True)
def _fresh_chart_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


@pytest.fixture
def fake(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "stt_engine", "fake")
    return s


def blocos(pcm: bytes, tam: int = BLOCO):
    for i in range(0, len(pcm), tam):
        yield pcm[i : i + tam]


def conversar(ws, pcm: bytes, tam: int = BLOCO) -> list[dict]:
    assert ws.receive_json()["tipo"] == "pronto"
    msgs: list[dict] = []
    for b in blocos(pcm, tam):
        ws.send_bytes(b)
    ws.send_text(json.dumps({"tipo": "fim"}))
    while True:
        m = ws.receive_json()
        msgs.append(m)
        if m["tipo"] in ("fim", "erro"):
            return msgs


def test_config_so_liga_com_servico(client, auth, fake, monkeypatch):
    h = auth()
    assert client.get("/consult/config", headers=h).json()["stt_servidor"] is True
    monkeypatch.setattr(fake, "stt_engine", "")
    assert client.get("/consult/config", headers=h).json()["stt_servidor"] is False
    monkeypatch.setattr(fake, "stt_url", "http://stt:8000/v1/audio/transcriptions")
    cfg = client.get("/consult/config", headers=h).json()
    assert cfg["stt_servidor"] is True and cfg["stt_streaming"] is True


def test_wav_sintetico_parcial_antes_do_final_e_glibenclamida(client, auth, fake):
    h = auth()
    cid = start(client, h)["id"]
    pcm = stt_stream.de_wav(WAV.read_bytes())
    with client.websocket_connect(f"/ws/consultations/{cid}/audio?falante=paciente", headers=h) as ws:
        msgs = conversar(ws, pcm)
    tipos = [m["tipo"] for m in msgs]
    assert "parcial" in tipos and "final" in tipos
    assert tipos.index("parcial") < tipos.index("final")
    finais = [m for m in msgs if m["tipo"] == "final"]
    assert "glibenclamida" in finais[0]["texto"]
    assert [f["texto"] for f in finais] == ["paciente toma glibenclamida de manhã", "refere tontura"]
    assert finais[-1]["insights"] and finais[-1]["total"] == len(finais)
    got = client.get(f"/consultations/{cid}", headers=h).json()
    servidor = [s for s in got["segmentos"] if s["origem"] == "servidor"]
    assert [s["texto"] for s in servidor] == [f["texto"] for f in finais]
    assert all(s["falante"] == "paciente" for s in servidor)


def test_parcial_so_traz_o_que_ainda_nao_foi_fechado(client, auth, fake):
    h = auth()
    cid = start(client, h)["id"]
    pcm = stt_stream.sintetizar(["paciente toma glibenclamida de manhã"])
    with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers=h) as ws:
        msgs = conversar(ws, pcm + bytes(stt_stream.TAXA * 2))
    parciais = [m["texto"] for m in msgs if m["tipo"] == "parcial"]
    assert parciais and all("paciente toma glibenclamida de manhã".startswith(p.split()[0]) for p in parciais)


FRASE = "paciente toma glibenclamida de manhã refere tontura desde ontem sem febre dor no peito"


@pytest.mark.parametrize("janela", [2.0, 2.5, 3.0, 4.0, 5.0])
@pytest.mark.parametrize("tam", [BLOCO, 3200, 12800])
def test_frase_que_cruza_a_janela_nao_duplica_nem_corta(fake, monkeypatch, janela, tam):
    monkeypatch.setattr(fake, "stt_janela_s", janela)
    pcm = stt_stream.sintetizar([FRASE])  # fala contínua de ~11 s: sem silêncio para fechar a frase
    sessao = stt_stream.Sessao(stt_stream.FakeMotor())
    eventos = []
    for b in blocos(pcm, tam):
        eventos += sessao.adicionar(b)
    eventos += sessao.encerrar()
    finais = [e.texto for e in eventos if e.tipo == "final"]
    assert len(finais) > 1  # de fato atravessou janelas
    assert " ".join(finais).split() == FRASE.split()


def test_fala_apos_silencio_e_silencio_puro(fake):
    sessao = stt_stream.Sessao(stt_stream.FakeMotor())
    assert sessao.adicionar(bytes(stt_stream.TAXA * 4)) == []  # 2 s de silêncio: nada
    assert sessao.buf == b""
    pcm = stt_stream.sintetizar(["dor no peito", "sem febre"])
    ev = []
    for b in blocos(pcm):
        ev += sessao.adicionar(b)
    ev += sessao.encerrar()
    assert [e.texto for e in ev if e.tipo == "final"] == ["dor no peito", "sem febre"]


def snapshot(*pastas: Path) -> dict[str, int]:
    out = {}
    for pasta in pastas:
        for raiz, _, nomes in os.walk(pasta):
            for n in nomes:
                p = Path(raiz) / n
                try:
                    out[str(p)] = p.stat().st_mtime_ns
                except OSError:
                    pass
    return out


def test_nenhum_audio_e_escrito_em_disco(client, auth, fake, tmp_path, monkeypatch):
    h = auth()
    cid = start(client, h)["id"]
    vigiadas = [Path(tempfile.gettempdir()), Path.cwd(), get_settings().data_dir, tmp_path]
    antes = {p: snapshot(p) for p in vigiadas}
    escritas = []
    import builtins

    real_open = builtins.open

    def espia(file, mode="r", *a, **kw):
        if (
            isinstance(file, str | Path)
            and any(c in mode for c in "wax+")
            and not str(file).endswith((".db", ".db-journal"))
        ):
            escritas.append(str(file))
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", espia)
    monkeypatch.setattr(tempfile, "mkstemp", lambda *a, **k: pytest.fail("áudio em arquivo temporário"))
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", lambda *a, **k: pytest.fail("áudio em arquivo temporário"))
    with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers=h) as ws:
        msgs = conversar(ws, stt_stream.de_wav(WAV.read_bytes()))
    assert any(m["tipo"] == "final" for m in msgs)
    monkeypatch.undo()
    assert escritas == []
    for p in vigiadas:
        depois = snapshot(p)
        novos = {k for k in depois if k not in antes[p]}
        assert not [k for k in novos if not k.endswith((".db", "-journal", ".pyc"))], novos


def test_recusas(client, auth, fake):
    h = auth()
    cid = start(client, h)["id"]
    with client.websocket_connect(f"/ws/consultations/{cid}/audio") as ws:  # sem credencial
        assert ws.receive_json()["tipo"] == "erro"
    with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers=auth("gestor")) as ws:
        assert "restrita" in ws.receive_json()["mensagem"]
    sem = start(
        client,
        h,
        consentimento=CONSENT | {"finalidades": CONSENT["finalidades"] | {"transcricao": False, "ia_externa": False}},
    )
    with client.websocket_connect(f"/ws/consultations/{sem['id']}/audio", headers=h) as ws:
        assert "não autorizou" in ws.receive_json()["mensagem"]
    with client.websocket_connect(
        f"/ws/consultations/{cid}/audio", headers=h | {"Origin": "http://evil.example"}
    ) as ws:  # com Bearer a origem não importa (cliente de API)
        assert ws.receive_json()["tipo"] == "pronto"


def test_cookie_exige_origem_permitida(client, auth, fake):
    h = auth()
    cid = start(client, h)["id"]
    token = h["Authorization"].split()[1]
    client.cookies.set("housia_session", token)
    with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers={"Origin": "http://evil.example"}) as ws:
        assert "Origem" in ws.receive_json()["mensagem"]
    with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers={"Origin": "http://localhost:3000"}) as ws:
        assert ws.receive_json()["tipo"] == "pronto"
    client.cookies.clear()


def test_sem_motor_configurado(client, auth):
    h = auth()
    cid = start(client, h)["id"]
    with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers=h) as ws:
        assert "não configurada" in ws.receive_json()["mensagem"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_http_motor_le_palavras_e_logprob(fake, monkeypatch):
    monkeypatch.setattr(fake, "stt_url", "http://stt:8000/v1/audio/transcriptions")
    chamadas = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "text": " toma glibenclamida",
                "segments": [{"avg_logprob": -0.2}, {"avg_logprob": -0.4}],
                "words": [
                    {"word": " toma", "start": 0.1, "end": 0.4, "probability": 0.9},
                    {"word": " glibenclamida", "start": 0.5, "end": 1.3},
                ],
            }

    def post(url, **kw):
        chamadas.append(kw)
        return Resp()

    monkeypatch.setattr(stt.httpx, "post", post)
    res = stt.transcribe_words(stt_stream.para_wav(bytes(3200)), "audio/wav", "pt-BR", "glibenclamida")
    assert [p.texto for p in res.palavras] == ["toma", "glibenclamida"]
    assert res.avg_logprob == pytest.approx(-0.3)
    assert chamadas[0]["data"]["response_format"] == "verbose_json"
    assert "word" in chamadas[0]["data"]["timestamp_granularities[]"]
    # e o motor HTTP entrega Palavra com tempos para a sessão deduplicar
    palavras = stt_stream.HttpMotor().transcrever(bytes(3200), corte_final=False, idioma="pt")
    assert [(p.texto, p.inicio, p.fim) for p in palavras] == [("toma", 0.1, 0.4), ("glibenclamida", 0.5, 1.3)]


def test_wav_so_aceita_16khz_mono():
    with pytest.raises(ValueError):
        stt_stream.sintetizar(["palavra inexistente"])


def test_probe_so_anuncia_servidor_que_responde(client, auth, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "stt_url", "http://stt:8000/v1/audio/transcriptions")
    monkeypatch.setattr(s, "stt_probe", True)
    vistos = []

    def get(url, **kw):
        vistos.append(url)
        if not subir:
            raise stt.httpx.ConnectError("sem perfil stt")
        return type("R", (), {"status_code": 200})()

    monkeypatch.setattr(stt.httpx, "get", get)
    subir = False
    monkeypatch.setattr(stt, "_probe_cache", (0.0, False))
    assert stt.enabled() is False  # `docker compose up` sem o perfil `stt`
    subir = True
    monkeypatch.setattr(stt, "_probe_cache", (0.0, False))
    assert stt.enabled() is True  # `docker compose --profile stt up`
    assert vistos == ["http://stt:8000/health"] * 2
    assert client.get("/consult/config", headers=auth()).json()["stt_servidor"] is True
