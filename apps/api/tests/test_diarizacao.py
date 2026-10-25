"""#40 diarização acústica com a voz do profissional cadastrada. Áudio sintético (vozes harmônicas geradas aqui) e
embeddings falsos; nenhum modelo, nenhuma voz real. Os números de similaridade do backend heurístico são medidos nos
próprios testes, não presumidos."""

import json
import math
from array import array

import httpx
import pytest
from sqlalchemy import select, text

from app.audit.service import log_action  # noqa: F401 - garante o mapeamento do audit_log
from app.config import get_settings
from app.consult import diarizacao as dz
from app.consult import service as consult_service
from app.consult import stt, stt_stream
from app.db import session as db_session
from app.db.models import AuditLog, PerfilVoz, User
from tests.test_consult import start

TAXA = stt_stream.TAXA
TERMO = {"X-Consentimento-Voz": dz.TERMO_VERSAO}
WAV = {"Content-Type": "audio/wav"}


@pytest.fixture(autouse=True)
def _limpa_cache():
    consult_service.clear_chart_cache()
    yield
    consult_service.clear_chart_cache()


@pytest.fixture
def heur(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "diarization_backend", "heuristico")
    return s


# ---------- áudio sintético ----------
def voz(f0: float, tilt: float, seg_s: float, amp: float = 0.25) -> bytes:
    """Vogal sintética: harmônicos de f0 com inclinação espectral `tilt`. Vozes diferentes = timbres diferentes."""
    harm = [(h, 1 / h**tilt) for h in range(1, int(3800 / f0) + 1)]
    tot = sum(a for _, a in harm)
    out = array("h")
    for k in range(int(seg_s * TAXA)):
        t = k / TAXA
        out.append(int(amp * 32767 * sum(a * math.sin(2 * math.pi * f0 * h * t + h) for h, a in harm) / tot))
    return out.tobytes()


def silencio(s: float) -> bytes:
    return bytes(2 * int(s * TAXA))


A = lambda s: voz(120, 1.2, s)  # noqa: E731 - o profissional
B = lambda s: voz(210, 0.5, s)  # noqa: E731 - outra pessoa, timbre bem diferente
D = lambda s: voz(130, 1.0, s)  # noqa: E731 - voz parecida com a do profissional


def conversa(turnos: list[tuple[str, float, str]], pausa: float = 0.6) -> tuple[bytes, stt.Transcricao]:
    """[(voz 'A'|'B'|'D', segundos, texto)] -> (WAV, transcrição com tempo por palavra, como o Whisper devolveria)."""
    fabrica = {"A": A, "B": B, "D": D}
    pcm, palavras, t = b"", [], 0.0
    for i, (quem, dur, texto) in enumerate(turnos):
        pcm += fabrica[quem](dur)
        ws = texto.split()
        for k, w in enumerate(ws):
            palavras.append(stt.PalavraSTT(w, t + dur * k / len(ws), t + dur * (k + 0.9) / len(ws), 0.95))
        t += dur
        if i < len(turnos) - 1:
            pcm += silencio(pausa)
            t += pausa
    return stt_stream.para_wav(pcm), stt.Transcricao(" ".join(p.texto for p in palavras), palavras, -0.1)


def dublar_stt(monkeypatch, transcricao: stt.Transcricao):
    monkeypatch.setattr(stt, "transcribe_words", lambda *a, **k: transcricao)


def cadastrar(client, h, audio: bytes | None = None, headers: dict | None = None):
    return client.put(
        "/consult/perfil-voz",
        content=audio if audio is not None else stt_stream.para_wav(A(8)),
        headers=h | (WAV | TERMO if headers is None else headers),
    )


def linhas(sql: str, **kw):
    with db_session.SessionLocal() as db:
        return db.execute(text(sql), kw).all()


def segmentos(client, h, cid) -> list[dict]:
    return [s for s in client.get(f"/consultations/{cid}", headers=h).json()["segmentos"] if s["origem"] == "servidor"]


# ---------- 1. consentimento ----------
def test_sem_consentimento_nada_e_guardado(client, auth, heur):
    h = auth()
    wav = stt_stream.para_wav(A(8))
    for cab in ({}, {"X-Consentimento-Voz": "voz-antigo"}, {"X-Consentimento-Voz": ""}):
        r = client.put("/consult/perfil-voz", content=wav, headers=h | WAV | cab)
        assert r.status_code == 428
    assert linhas("SELECT COUNT(*) FROM perfis_voz")[0][0] == 0
    assert client.get("/consult/perfil-voz", headers=h).json()["cadastrado"] is False


# ---------- 2. só o vetor, cifrado, auditado, nunca devolvido ----------
def test_guarda_so_o_vetor_cifrado_e_audita_sem_vetor(client, auth, heur, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_encryption_key", "k1:chave-de-teste-da-voz")
    h = auth()
    wav = stt_stream.para_wav(A(8))
    r = cadastrar(client, h, wav)
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["cadastrado"] and corpo["compativel"] and corpo["dim"] == 32 and corpo["backend"] == "heuristico"
    assert corpo["cifrado"] is True
    assert not {"v", "vetor", "embedding", "embedding_json"} & set(corpo)  # o vetor nunca sai pela API
    bruto = linhas("SELECT embedding_json FROM perfis_voz")[0][0]
    assert bruto.startswith("enc:v1:") and '"v"' not in bruto and "heuristico" not in bruto  # cifrado em repouso
    with db_session.SessionLocal() as db:
        perfil = dz.carregar_perfil(db, db.scalar(select(User.id).where(User.email == "medico@housia.dev")))
        assert perfil is not None and perfil.dim == 32
        assert wav[100:200] not in bruto.encode("latin-1", "ignore")  # nem sombra do áudio
        acoes = db.scalars(select(AuditLog).where(AuditLog.action == "consulta.voz_cadastrada")).all()
    assert len(acoes) == 1
    pay = json.dumps(acoes[0].payload_json)
    assert "termo_versao" in pay and 'v":' not in pay and "embedding" not in pay
    assert set(acoes[0].payload_json) == {"backend", "dim", "duracao_fala_s", "termo_versao", "atualizacao"}


# ---------- 3. perfil de outro usuário ----------
def test_perfil_e_so_do_proprio_profissional(client, auth, heur):
    medico, enf, gestor = auth(), auth("enfermagem"), auth("gestor")
    assert cadastrar(client, medico).status_code == 200
    assert client.get("/consult/perfil-voz", headers=enf).json()["cadastrado"] is False
    assert client.delete("/consult/perfil-voz", headers=enf).json()["cadastrado"] is False  # não apaga o do colega
    assert client.get("/consult/perfil-voz", headers=medico).json()["cadastrado"] is True
    with db_session.SessionLocal() as db:
        outro = db.scalar(select(User.id).where(User.email == "enfermeira@housia.dev"))
        assert dz.carregar_perfil(db, outro) is None
    for metodo in ("get", "delete"):  # perfil de voz não é do gestor nem de visitante
        assert getattr(client, metodo)("/consult/perfil-voz", headers=gestor).status_code == 403
        assert getattr(client, metodo)("/consult/perfil-voz").status_code == 401
    assert client.put("/consult/perfil-voz", content=b"x", headers=gestor | TERMO).status_code == 403


# ---------- 4. apagar remove de verdade ----------
def test_apagar_remove_o_vetor_e_a_exclusao_do_usuario_tambem(client, auth, heur):
    h = auth()
    cadastrar(client, h)
    assert linhas("SELECT COUNT(*) FROM perfis_voz")[0][0] == 1
    r = client.delete("/consult/perfil-voz", headers=h)
    assert r.status_code == 200 and r.json()["cadastrado"] is False
    assert linhas("SELECT COUNT(*) FROM perfis_voz")[0][0] == 0
    with db_session.SessionLocal() as db:
        assert db.scalars(select(AuditLog).where(AuditLog.action == "consulta.voz_apagada")).all()
    cadastrar(client, h)
    with db_session.SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.email == "medico@housia.dev"))
        dz.apagar_do_usuario(db, uid)
        db.commit()
    assert linhas("SELECT COUNT(*) FROM perfis_voz")[0][0] == 0


# ---------- 5. amostras ruins ----------
def test_amostra_vazia_gigante_invalida_silenciosa_ou_com_duas_vozes(client, auth, heur):
    h = auth()
    assert cadastrar(client, h, b"").status_code == 422
    assert cadastrar(client, h, b"\x00" * (dz.MAX_AMOSTRA_BYTES + 1)).status_code == 413
    assert cadastrar(client, h, b"isto nao e um wav").status_code == 422
    assert cadastrar(client, h, stt_stream.para_wav(A(8))[:3000]).status_code == 422  # WAV truncado
    assert cadastrar(client, h, stt_stream.para_wav(silencio(8))).status_code == 422  # só silêncio
    assert cadastrar(client, h, stt_stream.para_wav(A(2))).status_code == 422  # curto demais
    estereo = bytearray(stt_stream.para_wav(A(8)))
    estereo[22:24] = (2).to_bytes(2, "little")  # número de canais
    assert cadastrar(client, h, bytes(estereo)).status_code == 422
    duas = stt_stream.para_wav(A(3) + silencio(0.6) + B(3))
    r = cadastrar(client, h, duas)
    assert r.status_code == 422 and "mais de uma voz" in r.json()["detail"]
    assert linhas("SELECT COUNT(*) FROM perfis_voz")[0][0] == 0


def test_diarizacao_desligada_nao_cadastra(client, auth):
    assert get_settings().diarization_backend == "off"
    assert cadastrar(client, auth()).status_code == 409


# ---------- 6. embeddings inválidos ----------
@pytest.mark.parametrize(
    "vec",
    [
        [1.0] * 7,  # dimensão curta
        [1.0] * 2000,  # dimensão enorme
        [1.0, float("nan")] + [0.5] * 8,
        [1.0, float("inf")] + [0.5] * 8,
        [0.0] * 16,  # nulo
        [True] * 16,
        ["a"] * 16,
        "texto",
        None,
        [1e12] * 16,
    ],
)
def test_embedding_invalido_e_recusado(vec):
    with pytest.raises(ValueError):
        dz.normalizar(vec)


def test_cosseno_de_dimensoes_diferentes_levanta():
    with pytest.raises(ValueError):
        dz.cosseno(dz.normalizar([1.0] * 8), dz.normalizar([1.0] * 9))


# ---------- 7/8. serviço externo ----------
def _externo(monkeypatch, resposta, url="http://diariza.interno:9000/diarize", api_key=""):
    s = get_settings()
    monkeypatch.setattr(s, "diarization_backend", "externo")
    monkeypatch.setattr(s, "diarization_url", url)
    monkeypatch.setattr(s, "diarization_api_key", api_key)
    chamadas: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        chamadas.append(req)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta(req) if callable(resposta) else resposta

    monkeypatch.setattr(dz, "_transporte", httpx.MockTransport(handler))
    return chamadas


def emb(*vals: float, dim: int = 16) -> list[float]:
    return list(vals) + [0.0] * (dim - len(vals))


def seg(ini, fim, fid, e):
    return {"inicio": ini, "fim": fim, "falante_id": fid, "embedding": e}


FALHAS = {
    "caiu": httpx.ConnectError("recusou"),
    "lento": httpx.ReadTimeout("lento"),
    "erro500": httpx.Response(500, text="boom"),
    "nao_json": httpx.Response(200, text="<html>oi</html>"),
    "forma_errada": httpx.Response(200, json={"foo": 1}),
    "tempo_invertido": httpx.Response(200, json={"segmentos": [seg(5, 1, "a", emb(1))]}),
    "embedding_nan": httpx.Response(
        200, content=b'{"segmentos":[{"inicio":0,"fim":1,"falante_id":"a","embedding":[NaN,1,1,1,1,1,1,1,1]}]}'
    ),
    "dimensoes_misturadas": httpx.Response(
        200, json={"segmentos": [seg(0, 1, "a", emb(1)), seg(1, 2, "b", emb(1, dim=12))]}
    ),
    "segmentos_demais": httpx.Response(
        200, json={"segmentos": [seg(i, i + 0.5, "a", emb(1)) for i in range(dz.MAX_SEGMENTOS + 1)]}
    ),
    "redirect": httpx.Response(302, headers={"Location": "http://127.0.0.1/"}),
}


@pytest.mark.parametrize("nome", list(FALHAS))
def test_servico_externo_com_defeito_nunca_perde_a_fala(client, auth, monkeypatch, nome):
    _externo(monkeypatch, FALHAS[nome])
    assert dz.segmentar(b"RIFFxxxx", "audio/wav") == ([], "falhou")
    h = auth()
    cid = start(client, h)["id"]
    _, tr = conversa([("A", 2, "doutora estou com dor"), ("B", 2, "ha quanto tempo")])
    dublar_stt(monkeypatch, tr)
    r = client.post(f"/consultations/{cid}/audio?falante=paciente", content=b"audio-qualquer", headers=h)
    assert r.status_code == 200, r.text
    novos = r.json()["novos"]
    assert len(novos) == 1 and novos[0]["texto"] == tr.texto  # comportamento antigo: uma fala, falante do botão
    assert novos[0]["falante"] == "paciente" and novos[0]["falante_incerto"] is False
    assert novos[0]["falante_origem"] == "botao"


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://[fd00:ec2::254]/x",
        "http://user:senha@diariza.interno/x",
        "ftp://diariza.interno/x",
        "http://8.8.8.8/diarize",  # IP público com on-premise: o áudio iria para fora
        "http://0x08080808/diarize",  # IP público ofuscado em hex
    ],
)
def test_ssrf_no_servico_de_diarizacao(monkeypatch, url):
    chamadas = _externo(monkeypatch, httpx.Response(200, json={"segmentos": []}), url=url)
    assert dz.segmentar(b"RIFFxxxx", "audio/wav") == ([], "falhou")
    assert chamadas == []  # nem chegou a conectar
    assert dz.problemas_config()


def test_ssrf_externo_nao_on_premise_recusa_rede_interna(monkeypatch):
    s = get_settings()
    chamadas = _externo(monkeypatch, httpx.Response(200, json={"segmentos": []}), url="https://127.1/x")
    monkeypatch.setattr(s, "diarization_on_premise", False)
    assert dz.segmentar(b"RIFFxxxx", "audio/wav")[1] == "falhou" and chamadas == []
    monkeypatch.setattr(s, "diarization_on_premise", True)
    assert dz.problemas_config() == []  # local é o uso previsto
    assert dz.segmentar(b"RIFFxxxx", "audio/wav")[1] == "ok" and len(chamadas) == 1


# ---------- 9. limiar no limite ----------
def _perfil():
    return dz.Perfil(dz.normalizar(emb(1, dim=16)), "heuristico")


def _trecho_com_sim(sim: float, fid="S1", ini=0.0, fim=2.0):
    return dz.Trecho(ini, fim, fid, dz.normalizar(emb(sim, math.sqrt(1 - sim * sim), dim=16)))


def test_limiar_no_limite_decide_com_clareza(heur, monkeypatch):
    perfil = _perfil()
    sim = dz.cosseno(_trecho_com_sim(0.8).embedding, perfil.vetor)
    pal = [dz.PalavraT("oi", 0.9, 0.2, 0.5)]
    monkeypatch.setattr(heur, "diarization_margem", 0.10)

    monkeypatch.setattr(heur, "diarization_limiar", sim)  # exatamente no limiar: é o profissional, com confiança
    falas, _ = dz.atribuir(pal, [_trecho_com_sim(0.8), _trecho_com_sim(0.1, "S2", 3, 4)], perfil, "paciente")
    assert falas[0].falante == "medico" and not falas[0].incerto

    monkeypatch.setattr(heur, "diarization_limiar", math.nextafter(sim, 2))  # um grão abaixo do limiar: zona incerta
    falas, _ = dz.atribuir(pal, [_trecho_com_sim(0.8), _trecho_com_sim(0.1, "S2", 3, 4)], perfil, "paciente")
    assert falas[0].incerto

    monkeypatch.setattr(heur, "diarization_limiar", sim + 0.10 + 0.01)  # abaixo do piso: paciente, sem dúvida
    falas, _ = dz.atribuir(pal, [_trecho_com_sim(0.8), _trecho_com_sim(0.1, "S2", 3, 4)], perfil, "paciente")
    assert falas[0].falante == "paciente" and not falas[0].incerto


# ---------- 10. voz parecida fica incerta, nunca errada em silêncio ----------
def test_voz_parecida_fica_incerta_sai_das_metricas_e_o_profissional_confirma(client, auth, heur, monkeypatch):
    h = auth()
    cadastrar(client, h)
    with db_session.SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.email == "medico@housia.dev"))
        perfil = dz.carregar_perfil(db, uid)
    sim_d = dz.cosseno(dz.embedding_heuristico(D(2)), perfil.vetor)
    assert heur.diarization_limiar - heur.diarization_margem <= sim_d < heur.diarization_limiar  # cai na faixa incerta

    cid = start(client, h)["id"]
    wav, tr = conversa([("D", 2, "doutora sinto muita dor"), ("B", 2, "ha quanto tempo")])
    dublar_stt(monkeypatch, tr)
    r = client.post(f"/consultations/{cid}/audio?falante=desconhecido", content=wav, headers=h | WAV)
    assert r.status_code == 200, r.text
    novos = r.json()["novos"]
    assert len(novos) == 2
    incerta, certa = novos
    assert incerta["falante_incerto"] is True and incerta["falante_origem"] == "diarizacao"
    assert incerta["falante_confianca"] == 0.5
    assert certa["falante"] == "paciente" and certa["falante_incerto"] is False and certa["falante_confianca"] > 0.5
    # fora das métricas de comunicação até o profissional conferir
    assert r.json()["insights"]["comunicacao"]["falas_incertas"] == 1
    n = incerta["n"]
    p = client.patch(f"/consultations/{cid}/segments/{n}", json={"falante": incerta["falante"]}, headers=h)
    assert p.status_code == 200, p.text
    seg_ok = next(s for s in p.json()["segmentos"] if s["n"] == n)
    assert seg_ok["falante_incerto"] is False and seg_ok["falante_origem"] == "correcao"
    assert p.json()["insights"]["comunicacao"]["falas_incertas"] == 0
    # corrigir o falante reroda as regras e guarda o original; o audit não leva texto
    p2 = client.patch(f"/consultations/{cid}/segments/{n}", json={"falante": "medico"}, headers=h)
    seg2 = next(s for s in p2.json()["segmentos"] if s["n"] == n)
    assert seg2["falante"] == "medico" and seg2["falante_original"] == incerta["falante"]
    with db_session.SessionLocal() as db:
        regs = db.scalars(
            select(AuditLog).where(AuditLog.action.in_(["consulta.fala_editada", "consulta.audio"]))
        ).all()
    tudo = json.dumps([r.payload_json for r in regs])
    assert "sinto muita dor" not in tudo and "falante_confirmado" in tudo


# ---------- 11. aceite: >=90% de falas com o falante certo, profissional reconhecido ----------
def test_duas_vozes_sinteticas_90_por_cento_certas(client, auth, heur, monkeypatch):
    h = auth()
    assert cadastrar(client, h, stt_stream.para_wav(A(10))).status_code == 200
    cid = start(client, h)["id"]
    roteiro = [
        ("A", 1.4, "bom dia como a senhora esta"),
        ("B", 1.1, "doutora estou com tontura"),
        ("A", 2.0, "desde quando isso acontece com a senhora"),
        ("B", 1.6, "desde ontem de manha"),
        ("A", 1.2, "toma algum remedio"),
        ("B", 2.2, "tomo metformina todos os dias"),
        ("A", 1.7, "vou pedir um exame de sangue"),
        ("B", 1.0, "tudo bem doutora"),
        ("A", 1.3, "retorne em duas semanas"),
        ("B", 1.5, "obrigada doutora"),
    ]
    wav, tr = conversa(roteiro)
    dublar_stt(monkeypatch, tr)
    r = client.post(f"/consultations/{cid}/audio?falante=medico", content=wav, headers=h | WAV)
    assert r.status_code == 200, r.text
    novos = r.json()["novos"]
    esperado = ["medico" if q == "A" else "paciente" for q, _, _ in roteiro]
    assert len(novos) == len(roteiro)  # uma fala por turno, na ordem
    certas = sum(1 for n, e in zip(novos, esperado, strict=True) if n["falante"] == e and not n["falante_incerto"])
    assert certas / len(roteiro) >= 0.9
    assert [n["texto"] for n in novos] == [t for _, _, t in roteiro]
    assert [n["n"] for n in novos] == sorted(n["n"] for n in novos)
    assert all(n["falante_origem"] == "diarizacao" for n in novos)
    medico = [n for n in novos if n["falante"] == "medico"]
    assert medico and all(n["falante_confianca"] >= heur.diarization_limiar for n in medico)
    # a trilha do audit não carrega texto
    with db_session.SessionLocal() as db:
        reg = db.scalars(select(AuditLog).where(AuditLog.action == "consulta.audio")).all()[-1]
    assert reg.payload_json["diarizacao"]["estado"] == "ok" and "metformina" not in json.dumps(reg.payload_json)


def test_sem_perfil_separa_os_turnos_mas_tudo_fica_incerto(client, auth, heur, monkeypatch):
    h = auth()
    cid = start(client, h)["id"]
    wav, tr = conversa([("A", 1.5, "como esta"), ("B", 1.5, "com dor"), ("A", 1.5, "desde quando")])
    dublar_stt(monkeypatch, tr)
    novos = client.post(f"/consultations/{cid}/audio?falante=medico", content=wav, headers=h | WAV).json()["novos"]
    assert [n["falante"] for n in novos] == ["medico", "paciente", "medico"]
    assert all(n["falante_incerto"] and n["falante_confianca"] is None for n in novos)


def test_perfil_de_outro_motor_ou_dimensao_nao_vale(heur):
    pal = [dz.PalavraT("a", 0.9, 0.2, 0.5), dz.PalavraT("b", 0.9, 3.2, 3.5)]
    tr = [_trecho_com_sim(0.99), _trecho_com_sim(0.99, "S2", 3, 4)]
    for perfil in (
        dz.Perfil(dz.normalizar(emb(1, dim=16)), "externo"),
        dz.Perfil(dz.normalizar(emb(1, dim=12)), "heuristico"),
    ):
        falas, info = dz.atribuir(pal, tr, perfil, "medico")
        assert falas and all(f.incerto for f in falas), info  # nunca "medico" com certeza por um vetor incomparável


# ---------- 12. dedupe da fila offline ----------
def test_reenvio_com_o_mesmo_client_id_nao_duplica_e_cabecalho_ruim_e_422(client, auth, heur, monkeypatch):
    h = auth()
    cadastrar(client, h)
    cid = start(client, h)["id"]
    wav, tr = conversa([("A", 1.5, "como esta"), ("B", 1.5, "com dor")])
    chamadas = []
    monkeypatch.setattr(stt, "transcribe_words", lambda *a, **k: chamadas.append(1) or tr)
    cab = h | WAV | {"X-Client-Id": "fila-001"}
    r1 = client.post(f"/consultations/{cid}/audio", content=wav, headers=cab)
    assert [s["client_id"] for s in r1.json()["novos"]] == ["fila-001:0", "fila-001:1"]
    r2 = client.post(f"/consultations/{cid}/audio", content=wav, headers=cab)
    assert r2.json()["novos"] == [] and r2.json()["duplicada"] is True and len(chamadas) == 1
    assert len(segmentos(client, h, cid)) == 2
    for ruim in ("a" * 80, "com espaco", "x;y", ""):
        assert (
            client.post(f"/consultations/{cid}/audio", content=wav, headers=h | WAV | {"X-Client-Id": ruim}).status_code
            == 422
        )


def test_diarizacao_desligada_mantem_o_comportamento_antigo(client, auth, monkeypatch):
    h = auth()
    cid = start(client, h)["id"]
    wav, tr = conversa([("A", 1.5, "como esta"), ("B", 1.5, "com dor")])
    dublar_stt(monkeypatch, tr)
    novos = client.post(f"/consultations/{cid}/audio?falante=paciente", content=wav, headers=h | WAV).json()["novos"]
    assert len(novos) == 1 and novos[0]["falante"] == "paciente" and novos[0]["falante_incerto"] is False


# ---------- externo, caminho feliz ----------
def test_externo_reconhece_o_profissional_pelo_embedding(client, auth, monkeypatch):
    h = auth()
    pro, outro = emb(1, 0.05), emb(0.1, 1)

    def resp(req: httpx.Request):
        assert req.headers["authorization"] == "Bearer segredo" and req.method == "POST"
        corpo = req.read()
        assert b"AMOSTRA" in corpo or b"CONVERSA" in corpo
        if b"AMOSTRA" in corpo:
            return httpx.Response(200, json={"segmentos": [seg(0, 9, "x", pro)]})
        return httpx.Response(200, json={"segmentos": [seg(0, 2, "0", pro), seg(2.5, 4.5, "1", outro)]})

    _externo(monkeypatch, resp, api_key="segredo")
    assert (
        cadastrar(client, h, b"AMOSTRA" + bytes(2000), headers={"Content-Type": "audio/webm"} | TERMO).status_code
        == 200
    )
    cid = start(client, h)["id"]
    palavras = [
        stt.PalavraSTT("bom", 0.1, 0.5, 0.9),
        stt.PalavraSTT("dia", 0.6, 1.0, 0.9),
        stt.PalavraSTT("dor", 3.0, 3.5, 0.9),
    ]
    dublar_stt(monkeypatch, stt.Transcricao("bom dia dor", palavras, -0.1))
    r = client.post(
        f"/consultations/{cid}/audio?falante=medico", content=b"CONVERSA", headers=h | {"Content-Type": "audio/webm"}
    )
    novos = r.json()["novos"]
    assert [(n["falante"], n["texto"], n["falante_incerto"]) for n in novos] == [
        ("medico", "bom dia", False),
        ("paciente", "dor", False),
    ]


# ---------- WebSocket do #11 ----------
class MotorRoteiro:
    """Motor de fala simulado: uma palavra por 0,5 s de cada turno (turnos separados por pausa), sempre as mesmas."""

    ultimo_logprob = None

    def transcrever(self, pcm, *, corte_final, idioma="pt", vocabulario=""):
        a = stt_stream._amostras(pcm)
        out = []
        for i, j in dz._turnos(a, 0.3):
            n = max(1, round((j - i) * stt_stream.QUADRO / 0.5))
            for k in range(n):
                ini = i * stt_stream.QUADRO + k * 0.5
                out.append(stt_stream.Palavra(f"p{round(ini, 1)}", ini, min(ini + 0.45, j * stt_stream.QUADRO), 0.9))
        return out


def _ws(client, h, cid, pcm, bloco=TAXA // 2 * 2):
    with client.websocket_connect(f"/ws/consultations/{cid}/audio?falante=medico", headers=h) as ws:
        assert ws.receive_json()["tipo"] == "pronto"
        for i in range(0, len(pcm), bloco):
            ws.send_bytes(pcm[i : i + bloco])
        ws.send_text(json.dumps({"tipo": "fim"}))
        msgs = []
        while True:
            m = ws.receive_json()
            msgs.append(m)
            if m["tipo"] in ("fim", "erro"):
                return msgs


def test_websocket_divide_a_frase_em_falas_por_falante_na_ordem(client, auth, heur, monkeypatch):
    monkeypatch.setattr(stt_stream, "motor", lambda: MotorRoteiro())
    h = auth()
    cadastrar(client, h)
    cid = start(client, h)["id"]
    # pausa de 0,5 s entre as vozes (< 0,7 s do VAD do fluxo): a mesma frase fechada traz os dois falantes
    pcm = A(2.0) + silencio(0.5) + B(2.0) + silencio(1.5)
    msgs = _ws(client, h, cid, pcm)
    finais = [m for m in msgs if m["tipo"] == "final"]
    assert len(finais) == 1 and len(finais[0]["novos"]) == 2
    a, b = finais[0]["novos"]
    assert (a["falante"], b["falante"]) == ("medico", "paciente") and a["n"] < b["n"]
    assert a["falante_origem"] == b["falante_origem"] == "diarizacao" and not a["falante_incerto"]
    assert finais[0]["falante"] == "medico" and finais[0]["texto"] == f"{a['texto']} {b['texto']}"
    with db_session.SessionLocal() as db:
        reg = db.scalars(select(AuditLog).where(AuditLog.action == "consulta.audio_stream")).all()[-1]
    assert reg.payload_json["diarizacao"] == "heuristico" and reg.payload_json["falas_incertas"] == 0
    assert "p0" not in json.dumps(reg.payload_json)


def test_websocket_com_diarizacao_quebrada_volta_ao_falante_unico(client, auth, heur, monkeypatch):
    monkeypatch.setattr(stt_stream, "motor", lambda: MotorRoteiro())

    def quebra(*a, **k):
        raise RuntimeError("modelo caiu")

    monkeypatch.setattr(dz, "segmentar_heuristico", quebra)
    h = auth()
    cid = start(client, h)["id"]
    msgs = _ws(client, h, cid, A(2.0) + silencio(0.5) + B(2.0) + silencio(1.5))
    finais = [m for m in msgs if m["tipo"] == "final"]
    assert len(finais) == 1 and len(finais[0]["novos"]) == 1  # a fala não se perde
    assert finais[0]["novos"][0]["falante"] == "medico" and finais[0]["novos"][0]["falante_origem"] == "botao"
    assert finais[0]["texto"].count("p") >= 2


# ---------- migração ----------
def test_migracao_c305_sobe_e_desce(tmp_path, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    raiz = Path(__file__).resolve().parents[1]
    cfg = Config(str(raiz / "alembic.ini"))
    cfg.set_main_option("script_location", str(raiz / "app/db/migrations"))
    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    command.upgrade(cfg, "c305")
    cols = {c["name"] for c in inspect(create_engine(url)).get_columns("perfis_voz")}
    assert {"user_id", "embedding_json", "termo_versao", "consentimento_em"} <= cols and "audio" not in cols
    command.downgrade(cfg, "c304")
    assert "perfis_voz" not in inspect(create_engine(url)).get_table_names()
    assert PerfilVoz.__tablename__ == "perfis_voz"


# ======================= verificação cética do #40 (entradas adversariais) =======================
def _wav_cru(pcm: bytes, canais=1, taxa=TAXA, bits=16, declarado: int | None = None) -> bytes:
    """WAV montado à mão para mentir no cabeçalho (tamanho declarado diferente do real, outra taxa, 8 bits...)."""
    n = len(pcm) if declarado is None else declarado
    return (
        b"RIFF"
        + (36 + n).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + canais.to_bytes(2, "little")
        + taxa.to_bytes(4, "little")
        + (taxa * canais * bits // 8).to_bytes(4, "little")
        + (canais * bits // 8).to_bytes(2, "little")
        + bits.to_bytes(2, "little")
        + b"data"
        + n.to_bytes(4, "little")
        + pcm
    )


def _dor():
    return stt.Transcricao(
        "tenho dor", [stt.PalavraSTT("tenho", 0, 0.4, 0.9), stt.PalavraSTT("dor", 0.5, 0.9, 0.9)], -0.1
    )


def test_corpo_gigante_e_recusado_sem_ler_tudo(client, auth, heur, monkeypatch):
    from app.consult import router as consult_router

    h = auth()
    cid = start(client, h)["id"]
    monkeypatch.setattr(consult_router, "MAX_AUDIO_BYTES", 5000)
    monkeypatch.setattr(dz, "MAX_AMOSTRA_BYTES", 5000)
    r = client.post(f"/consultations/{cid}/audio", content=bytes(6000), headers=h | WAV)
    assert r.status_code == 413
    assert cadastrar(client, h, bytes(6000)).status_code == 413

    def pedacos():  # sem Content-Length (corpo em pedaços): o limite vale do mesmo jeito
        for _ in range(10):
            yield bytes(1000)

    assert client.post(f"/consultations/{cid}/audio", content=pedacos(), headers=h | WAV).status_code == 413
    assert linhas("SELECT COUNT(*) FROM perfis_voz")[0][0] == 0


@pytest.mark.parametrize(
    "wav",
    [
        _wav_cru(bytes(3200), declarado=2_000_000_000),  # cabeçalho mente: promete 2 GB
        _wav_cru(bytes(3200), canais=2),
        _wav_cru(bytes(3200), taxa=8000),
        _wav_cru(bytes(3200), bits=8),
        b"RIFF\x00\x00\x00\x00WAVE",  # só o começo
        b"RIFF" + bytes(40),
    ],
)
def test_wav_malformado_nunca_perde_a_fala_nem_cadastra(client, auth, heur, monkeypatch, wav):
    h = auth()
    assert cadastrar(client, h, wav).status_code == 422
    cid = start(client, h)["id"]
    dublar_stt(monkeypatch, _dor())
    r = client.post(f"/consultations/{cid}/audio?falante=paciente", content=wav, headers=h | WAV)
    assert r.status_code == 200, r.text
    novos = r.json()["novos"]
    assert [(n["texto"], n["falante"], n["falante_incerto"]) for n in novos] == [("tenho dor", "paciente", False)]
    assert linhas("SELECT COUNT(*) FROM perfis_voz")[0][0] == 0


def test_heuristico_recusa_audio_longo_demais(heur):
    longo = bytes(int(dz.MAX_HEURISTICO_S * TAXA * 2) + 2)
    assert dz.segmentar_pcm(longo)[1] == "falhou"  # volta ao botão em vez de gastar minutos de CPU
    assert dz.segmentar(stt_stream.para_wav(longo), "audio/wav")[1] == "falhou"


def test_alergia_dita_por_falante_incerto_nao_e_descartada(client, auth, heur, monkeypatch):
    """Sem perfil, o 1º falante herda o botão. Se o paciente fala primeiro com o botão em "medico", a fala sai como
    médico incerto: a alergia dele precisa aparecer do mesmo jeito (com a Fala/N), nunca sumir em silêncio."""
    h = auth()
    cid = start(client, h)["id"]
    wav, tr = conversa([("B", 2.0, "tenho alergia a dipirona"), ("A", 2.0, "entendi vou examinar")])
    dublar_stt(monkeypatch, tr)
    r = client.post(f"/consultations/{cid}/audio?falante=medico", content=wav, headers=h | WAV)
    novos = r.json()["novos"]
    assert novos[0]["falante"] == "medico" and novos[0]["falante_incerto"] is True  # rótulo errado, mas sinalizado
    alergias = r.json()["insights"]["alergias"]
    assert any("dipirona" in a["alergeno"].lower() for a in alergias), alergias
    # a fala incerta continua na transcrição, citável por Fala/N, e fora das métricas
    assert novos[0]["texto"].startswith("tenho alergia a dipirona")
    assert r.json()["insights"]["comunicacao"]["falas_incertas"] == 2


def test_medicamento_planejado_dito_por_incerto_continua_valendo(client, auth, heur, monkeypatch):
    h = auth()
    cid = start(client, h)["id"]
    wav, tr = conversa([("A", 2.0, "vou passar dipirona para a senhora"), ("B", 2.0, "tenho alergia a dipirona")])
    dublar_stt(monkeypatch, tr)
    # botão "paciente": a fala do médico sai como paciente incerto; o remédio planejado não pode se perder
    r = client.post(f"/consultations/{cid}/audio?falante=paciente", content=wav, headers=h | WAV)
    ins = r.json()["insights"]
    assert r.json()["novos"][0]["falante_incerto"] is True
    assert any("dipirona" in a["alergeno"].lower() for a in ins["alergias"])
    alerta = [a for a in ins["alertas"] if a["texto"].startswith("Alergia a Dipirona")]
    assert alerta and "Fala/1" in alerta[0]["refs"], "o remédio dito pelo falante incerto precisa cruzar com a alergia"


def test_cliente_nao_forja_trilha_de_diarizacao_nem_pelo_patch(client, auth, heur):
    h = auth()
    cid = start(client, h)["id"]
    forjado = {
        "texto": "tenho alergia a dipirona",
        "falante": "medico",
        "origem": "servidor",
        "falante_origem": "diarizacao",
        "falante_confianca": 0.99,
        "falante_incerto": True,
    }
    r = client.post(f"/consultations/{cid}/segments", json={"segmentos": [forjado]}, headers=h)
    assert r.status_code == 200, r.text
    s = r.json()["novos"][0]
    assert s["falante_origem"] != "diarizacao" and s["falante_confianca"] is None and s["falante_incerto"] is False
    # PATCH ignora campos de trilha e carimba "correcao"
    p = client.patch(
        f"/consultations/{cid}/segments/{s['n']}",
        json={"falante": "paciente", "falante_origem": "diarizacao", "falante_confianca": 1.0},
        headers=h,
    )
    assert p.status_code == 200, p.text
    seg_ = next(x for x in p.json()["segmentos"] if x["n"] == s["n"])
    assert seg_["falante"] == "paciente" and seg_["falante_origem"] == "correcao" and seg_["falante_confianca"] is None


def test_chave_de_cifra_perdida_nao_derruba_nem_trava_o_recadastro(client, auth, heur, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "data_encryption_key", "k1:primeira-chave")
    h = auth()
    assert cadastrar(client, h).status_code == 200
    monkeypatch.setattr(s, "data_encryption_key", "k2:outra-chave")  # a k1 saiu da configuração
    r = client.get("/consult/perfil-voz", headers=h)
    assert r.status_code == 200 and r.json()["cadastrado"] is True and r.json()["compativel"] is False
    assert cadastrar(client, h).status_code == 200  # recadastrar substitui o perfil ilegível
    assert client.get("/consult/perfil-voz", headers=h).json()["compativel"] is True
    assert linhas("SELECT COUNT(*) FROM perfis_voz")[0][0] == 1


def test_apagar_com_perfil_ilegivel_funciona_e_nao_deixa_resto(client, auth, heur, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "data_encryption_key", "k1:primeira-chave")
    h = auth()
    cadastrar(client, h)
    monkeypatch.setattr(s, "data_encryption_key", "k2:outra-chave")
    assert client.delete("/consult/perfil-voz", headers=h).status_code == 200
    assert linhas("SELECT COUNT(*) FROM perfis_voz")[0][0] == 0


def test_perfil_nao_vaza_para_outra_organizacao_nem_para_o_audit(client, auth, heur):
    from tests.test_multiorg import cadastro_completo

    medico = auth()
    assert cadastrar(client, medico).status_code == 200
    outra, _ = cadastro_completo(client, "dra.bia@clinica.test")
    assert client.get("/consult/perfil-voz", headers=outra).json()["cadastrado"] is False
    assert client.delete("/consult/perfil-voz", headers=outra).json()["cadastrado"] is False
    assert linhas("SELECT COUNT(*) FROM perfis_voz")[0][0] == 1  # o da outra org segue intacto
    with db_session.SessionLocal() as db:
        trilha = json.dumps([a.payload_json for a in db.scalars(select(AuditLog))], default=str)
    assert "embedding" not in trilha and '"v"' not in trilha


def test_externo_resposta_gigante_ou_falante_id_enorme_cai_no_botao(client, auth, monkeypatch):
    h = auth()
    cid = start(client, h)["id"]
    dublar_stt(monkeypatch, _dor())
    gigante = b'{"segmentos":[],"lixo":"' + b"x" * (dz.MAX_RESPOSTA_BYTES + 10) + b'"}'
    respostas = [
        httpx.Response(200, content=gigante),
        httpx.Response(200, json={"segmentos": [seg(0, 1, "f" * 200, emb(1))]}),
        httpx.Response(200, json={"segmentos": [seg(0, 1, 10**80, emb(1))]}),
        httpx.Response(200, content=b'{"segmentos":[{"inicio":0,"fim":1e999,"falante_id":"a"}]}'),
        httpx.Response(200, content=b'{"segmentos":' + b"[" * 3000 + b"]" * 3000 + b"}"),
        httpx.Response(200, content=b"\xff\xfe\x00"),
    ]
    for resposta in respostas:
        _externo(monkeypatch, resposta)
        r = client.post(
            f"/consultations/{cid}/audio?falante=paciente",
            content=b"x" * 50,
            headers=h | {"Content-Type": "audio/webm"},
        )
        assert r.status_code == 200, r.text
        assert [(n["texto"], n["falante"]) for n in r.json()["novos"]][-1] == ("tenho dor", "paciente")
        assert dz.segmentar(b"x", "audio/webm")[1] == "falhou"


def test_incerta_nao_some_da_transcricao_so_das_metricas(client, auth, heur, monkeypatch):
    h = auth()
    cid = start(client, h)["id"]
    wav, tr = conversa([("A", 1.5, "como esta"), ("B", 1.5, "com dor de cabeca")])
    dublar_stt(monkeypatch, tr)
    r = client.post(f"/consultations/{cid}/audio?falante=medico", content=wav, headers=h | WAV)
    assert all(n["falante_incerto"] for n in r.json()["novos"])
    c = client.get(f"/consultations/{cid}", headers=h).json()
    textos = [s["texto"] for s in c["segmentos"] if s["origem"] == "servidor"]
    assert textos == ["como esta", "com dor de cabeca"]  # tudo na transcrição, na ordem
    assert c["insights"]["comunicacao"]["falas_incertas"] == 2


def test_replay_com_audio_diferente_e_mesmo_id_nao_duplica_e_ids_sao_estaveis(client, auth, heur, monkeypatch):
    h = auth()
    cid = start(client, h)["id"]
    wav1, tr1 = conversa([("A", 1.5, "primeira fala"), ("B", 1.5, "segunda fala")])
    dublar_stt(monkeypatch, tr1)
    cab = h | WAV | {"X-Client-Id": "q-1"}
    r1 = client.post(f"/consultations/{cid}/audio", content=wav1, headers=cab)
    ids1 = [s["client_id"] for s in r1.json()["novos"]]
    wav2, tr2 = conversa([("B", 1.5, "outra coisa"), ("A", 1.5, "totalmente diferente"), ("B", 1.5, "terceira")])
    dublar_stt(monkeypatch, tr2)
    r = client.post(f"/consultations/{cid}/audio", content=wav2, headers=cab)
    assert r.json()["duplicada"] is True and r.json()["novos"] == []
    assert [s["client_id"] for s in segmentos(client, h, cid)] == ids1 == ["q-1:0", "q-1:1"]
    r3 = client.post(f"/consultations/{cid}/audio", content=wav2, headers=h | WAV | {"X-Client-Id": "q-2"})
    assert [n["n"] for n in r3.json()["novos"]] == [3, 4, 5]
