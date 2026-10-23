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
from tests.test_consult import SCRIPT, accept_critical, say, start

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
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))  # pasta privada: o /tmp compartilhado muda sozinho
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


# ---------- verificação adversarial (privacidade: áudio de paciente + autenticação do WebSocket) ----------
def _token(sub: int, exp_delta: int = 3600, secret: str | None = None, **extra) -> str:
    import time

    import jwt

    s = get_settings()
    claims = {"sub": str(sub), "role": "medico", "exp": int(time.time()) + exp_delta} | extra
    return jwt.encode(claims, secret or s.jwt_secret, algorithm="HS256")


def _primeiro(client, cid, headers=None, path=""):
    with client.websocket_connect(f"/ws/consultations/{cid}/audio{path}", headers=headers or {}) as ws:
        m = ws.receive_json()
        return m


def _qtd_segmentos(client, h, cid):
    return len(client.get(f"/consultations/{cid}", headers=h).json()["segmentos"])


def test_ws_outra_organizacao_nao_transmite_nem_vaza(client, auth, fake):
    from tests.test_multiorg import cadastro_completo

    ha = auth("medico")
    cid = start(client, ha)["id"]
    antes = _qtd_segmentos(client, ha, cid)
    hb, _ = cadastro_completo(client)
    with client.websocket_connect(f"/ws/consultations/{cid}/audio?falante=paciente", headers=hb) as ws:
        m = ws.receive_json()
        assert m["tipo"] == "erro" and m["mensagem"] == "Consulta não encontrada"  # idêntica a um id que não existe
        with pytest.raises(WebSocketDisconnect):
            ws.send_bytes(stt_stream.sintetizar(["dor no peito"]))
            ws.receive_json()
    inexistente = _primeiro(client, 999999, hb)
    assert inexistente == m  # não distingue "de outra org" de "não existe"
    assert _qtd_segmentos(client, ha, cid) == antes
    # cookie de B + origem permitida: o mesmo
    client.cookies.set("housia_session", hb["Authorization"].split()[1])
    assert _primeiro(client, cid, {"Origin": "http://localhost:3000"})["mensagem"] == "Consulta não encontrada"
    client.cookies.clear()


def test_ws_credenciais_invalidas_nao_chegam_ao_pronto(client, auth, fake):
    h = auth()
    cid = start(client, h)["id"]
    uid = client.get("/auth/me", headers=h).json()["id"]
    casos = {
        "expirado": {"Authorization": f"Bearer {_token(uid, -10)}"},
        "segredo_errado": {"Authorization": f"Bearer {_token(uid, secret='x' * 40)}"},
        "lixo": {"Authorization": "Bearer abc.def.ghi"},
        "sem_sub": {"Authorization": "Bearer " + _token(uid).split(".")[0]},
        "usuario_inexistente": {"Authorization": f"Bearer {_token(987654)}"},
        "sub_nao_numerico": {"Authorization": f"Bearer {_token('x')}"},
        "esquema_basic": {"Authorization": "Basic " + _token(uid)},
        "bearer_vazio": {"Authorization": "Bearer "},
    }
    for nome, hdr in casos.items():
        m = _primeiro(client, cid, hdr)
        assert m["tipo"] == "erro", nome
        assert "segmentos" not in json.dumps(m) and "glibenclamida" not in json.dumps(m), nome
    # token alg=none
    import base64

    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    assert (
        _primeiro(client, cid, {"Authorization": f"Bearer {b({'alg': 'none'})}.{b({'sub': str(uid)})}."})["tipo"]
        == "erro"
    )


def test_ws_cookie_sem_origem_ou_origem_cruzada(client, auth, fake):
    h = auth()
    cid = start(client, h)["id"]
    client.cookies.set("housia_session", h["Authorization"].split()[1])
    for origem in ("http://evil.example", "null", "http://localhost:3000.evil.example", "HTTP://LOCALHOST:3000"):
        assert "Origem" in _primeiro(client, cid, {"Origin": origem})["mensagem"], origem
    # um cookie de token inválido com origem boa continua recusado
    client.cookies.set("housia_session", "lixo")
    assert _primeiro(client, cid, {"Origin": "http://localhost:3000"})["tipo"] == "erro"
    client.cookies.clear()
    assert _primeiro(client, cid)["tipo"] == "erro"  # nada de credencial


def test_ws_revogacao_no_meio_da_transmissao_para_antes_de_transcrever(client, auth, fake, monkeypatch):
    h = auth()
    cid = start(client, h)["id"]
    chamadas = []
    real = stt_stream.FakeMotor.transcrever
    monkeypatch.setattr(
        stt_stream.FakeMotor, "transcrever", lambda self, *a, **k: chamadas.append(1) or real(self, *a, **k)
    )
    pcm = stt_stream.sintetizar(["paciente toma glibenclamida de manhã"])
    with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers=h) as ws:
        assert ws.receive_json()["tipo"] == "pronto"
        ws.send_bytes(pcm[:BLOCO])
        assert client.post(f"/consultations/{cid}/marker", json={"tipo": "revogado"}, headers=h).status_code == 200
        n = len(chamadas)
        ws.send_bytes(pcm[BLOCO : 2 * BLOCO])
        m = ws.receive_json()
        while m["tipo"] == "parcial":
            m = ws.receive_json()
        assert m["tipo"] == "erro" and "revogado" in m["mensagem"].lower()
        assert len(chamadas) == n  # o bloco posterior à revogação nem chegou ao motor
    assert not [
        s for s in client.get(f"/consultations/{cid}", headers=h).json()["segmentos"] if s["origem"] == "servidor"
    ]


def test_ws_pausa_e_consulta_finalizada(client, auth, fake):
    h = auth()
    cid = start(client, h)["id"]
    assert client.post(f"/consultations/{cid}/marker", json={"tipo": "pausa"}, headers=h).status_code == 200
    assert "Pausa" in _primeiro(client, cid, h)["mensagem"]
    assert client.post(f"/consultations/{cid}/marker", json={"tipo": "retomada"}, headers=h).status_code == 200
    with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers=h) as ws:
        assert ws.receive_json()["tipo"] == "pronto"
        assert client.post(f"/consultations/{cid}/marker", json={"tipo": "pausa"}, headers=h).status_code == 200
        ws.send_bytes(stt_stream.sintetizar(["dor no peito"])[:BLOCO])
        assert "Pausa" in ws.receive_json()["mensagem"]
    c2 = start(client, h)["id"]
    hh = {"Authorization": h["Authorization"]}
    accept_critical(client, hh, c2, say(client, hh, c2, SCRIPT))
    assert client.post(f"/consultations/{c2}/finish", headers=hh).status_code == 200
    assert "finalizada" in _primeiro(client, c2, h)["mensagem"]


@pytest.mark.parametrize(
    "texto",
    [
        "isto não é json",
        "[1, 2]",
        "null",
        '"texto"',
        "123",
        "{}",
        '{"tipo": "falante"}',
        '{"tipo": "falante", "falante": ["paciente"]}',
        '{"tipo": "falante", "falante": {"a": 1}}',
        '{"tipo": "falante", "falante": "hacker"}',
        '{"tipo": ["fim"]}',
        '{"tipo": "desconhecido", "falante": "paciente"}',
        "\x00\x01",
    ],
)
def test_ws_mensagem_de_controle_malformada_nao_derruba(client, auth, fake, texto):
    h = auth()
    cid = start(client, h)["id"]
    with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers=h) as ws:
        assert ws.receive_json()["tipo"] == "pronto"
        ws.send_text(texto)
        ws.send_bytes(b"")  # quadro vazio
        ws.send_bytes(b"\x01")  # número ímpar de bytes
        ws.send_bytes(bytes(1001))
        ws.send_text(json.dumps({"tipo": "fim"}))
        assert ws.receive_json()["tipo"] == "fim"


def test_ws_quadro_gigante_e_texto_gigante_fecham(client, auth, fake):
    from app.consult import router as r

    h = auth()
    cid = start(client, h)["id"]
    with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers=h) as ws:
        assert ws.receive_json()["tipo"] == "pronto"
        ws.send_bytes(bytes(r.MAX_FRAME_BYTES + 2))
        assert "grande demais" in ws.receive_json()["mensagem"]
    with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers=h) as ws:
        assert ws.receive_json()["tipo"] == "pronto"
        ws.send_text("x" * (r.MAX_TEXTO_BYTES + 1))
        assert "grande demais" in ws.receive_json()["mensagem"]


def test_ws_teto_de_duracao_da_transmissao(client, auth, fake, monkeypatch):
    from app.consult import router as r

    monkeypatch.setattr(r, "MAX_SESSAO_BYTES", BLOCO * 3)
    h = auth()
    cid = start(client, h)["id"]
    with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers=h) as ws:
        assert ws.receive_json()["tipo"] == "pronto"
        for _ in range(4):
            ws.send_bytes(bytes(BLOCO))
        assert "Limite" in ws.receive_json()["mensagem"]


def test_ws_token_expira_durante_a_transmissao(client, auth, fake, monkeypatch):
    import time as _t

    h = auth()
    cid = start(client, h)["id"]
    uid = client.get("/auth/me", headers=h).json()["id"]
    with client.websocket_connect(
        f"/ws/consultations/{cid}/audio", headers={"Authorization": f"Bearer {_token(uid, 1)}"}
    ) as ws:
        assert ws.receive_json()["tipo"] == "pronto"
        real = _t.time
        monkeypatch.setattr("app.consult.router.time.time", lambda: real() + 120)
        ws.send_bytes(bytes(BLOCO))
        assert "expirada" in ws.receive_json()["mensagem"]


def test_ws_audio_nao_aparece_em_log_nem_auditoria(client, auth, fake, caplog):
    from sqlalchemy import select

    from app.db import session as db_session
    from app.db.models import AuditLog

    h = auth()
    cid = start(client, h)["id"]
    with caplog.at_level("DEBUG"):
        with client.websocket_connect(f"/ws/consultations/{cid}/audio", headers=h) as ws:
            conversar(ws, stt_stream.de_wav(WAV.read_bytes()))
    assert "glibenclamida" not in caplog.text
    with db_session.SessionLocal() as db:
        linhas = db.scalars(select(AuditLog).where(AuditLog.action == "consulta.audio_stream")).all()
    assert linhas and all(set(linha.payload_json) == {"bytes", "frases", "motor"} for linha in linhas)


def test_guard_do_paciente_avulso_continua_valendo_no_http(client, auth):
    from tests.test_multiorg import cadastro_completo

    ha = auth("medico")
    novo = client.post(
        "/patients", json={"nome": "Paciente Sintético", "nascimento": "1980-01-01", "sexo": "F"}, headers=ha
    )
    assert novo.status_code == 201, novo.text
    pid = novo.json()["id"]
    assert client.get(f"/patients/{pid}", headers=ha).status_code == 200
    assert client.get(f"/patients/{pid}").status_code == 401  # sem credencial
    hb, _ = cadastro_completo(client)
    assert client.get(f"/patients/{pid}", headers=hb).status_code == 404  # outra organização
