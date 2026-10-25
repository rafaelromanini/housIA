"""Diarização acústica on-premise com a voz do profissional cadastrada (#40).

Quem fala deixa de ser só botão ou heurística de texto: o áudio de um trecho é dividido em turnos, cada turno vira um
vetor de voz (embedding) e o vetor é comparado, por similaridade cosseno, com o perfil de voz do profissional.

Backends, por `DIARIZATION_BACKEND`:
- `off` (padrão): comportamento antigo, um falante por trecho (botão ou heurística de texto).
- `heuristico`: local e determinístico, sem modelo. Turnos separados por pausa e um vetor de bandas espectrais. Serve
  para testes e como degradável; NÃO é um modelo de locutor (separa timbres bem diferentes, não vozes parecidas).
- `externo`: serviço on-premise em `DIARIZATION_URL` (ex.: speechbrain ECAPA atrás de uma API) que devolve
  `{"segmentos": [{"inicio", "fim", "falante_id", "embedding"}]}`. Mesma validação anti-SSRF do provedor de IA (#54).

Privacidade: o áudio não é guardado em lugar nenhum (nem o da amostra de cadastro). Do profissional guarda-se só o
vetor, cifrado, com o registro do consentimento (LGPD art. 5º, II e art. 11, I). Os vetores dos demais falantes vivem
só na memória da requisição. Qualquer falha cai no comportamento antigo: a fala nunca se perde.
"""

from __future__ import annotations

import json
import logging
import math
import time
from array import array
from dataclasses import dataclass

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.config import Settings, get_settings
from app.consult import stt_stream
from app.db import crypto
from app.db.models import PerfilVoz, User
from app.llm.errors import LLMError
from app.llm.providers import checar_destino, checar_url_estatica, fixar_destino

log = logging.getLogger("housia.diarizacao")

BACKENDS = ("off", "heuristico", "externo")
TERMO_VERSAO = "voz-2026.10"
TERMO = (
    "Autorizo o HousIA a calcular e guardar, cifrado, um vetor numérico da minha voz (dado biométrico, LGPD "
    "art. 5º, II) para reconhecer quando eu falo nas consultas. O áudio desta amostra não é guardado. "
    "Posso apagar meu perfil de voz a qualquer momento, e ele também é apagado se a minha conta for excluída. "
    "Sem o perfil, a captura continua funcionando com o botão de quem fala."
)
DIM_MIN, DIM_MAX = 8, 1024
MAX_SEGMENTOS = 500
MAX_RESPOSTA_BYTES = 2 * 1024 * 1024
MAX_AMOSTRA_BYTES = 5 * 1024 * 1024
AMOSTRA_MIN_S, AMOSTRA_MAX_S, FALA_MIN_S = 5.0, 60.0, 4.0
MAX_HEURISTICO_S = 300.0  # o heurístico é Python puro: áudio mais longo que isto volta ao falante do botão (anti-DoS)
LIMIAR_AGRUPAR = 0.9  # dois turnos do heurístico com cosseno >= isto são o mesmo falante
LADO_PROFISSIONAL = ("medico", "paciente", "acompanhante", "desconhecido")  # falantes que a diarização pode decidir


class DiarizacaoErro(RuntimeError):
    """Falha do backend (serviço fora, lento, resposta inválida). Nunca carrega áudio nem texto."""


@dataclass(frozen=True)
class Trecho:
    inicio: float  # s relativos ao início do áudio enviado
    fim: float
    falante_id: str
    embedding: tuple[float, ...] | None = None


@dataclass(frozen=True)
class PalavraT:
    texto: str
    prob: float | None
    inicio: float  # s, mesma referência dos trechos
    fim: float


@dataclass(frozen=True)
class Perfil:
    vetor: tuple[float, ...]
    backend: str

    @property
    def dim(self) -> int:
        return len(self.vetor)


@dataclass
class Fala:
    texto: str
    palavras: list[tuple[str, float | None]]
    inicio: float
    falante: str
    confianca: float | None
    incerto: bool
    origem: str = "diarizacao"


def backend(s: Settings | None = None) -> str:
    b = (s or get_settings()).diarization_backend.strip().lower()
    return b if b in BACKENDS else "off"


def ativo() -> bool:
    return backend() != "off"


def problemas_config(s: Settings | None = None) -> list[str]:
    s = s or get_settings()
    out: list[str] = []
    if s.diarization_backend.strip().lower() not in BACKENDS:
        out.append("DIARIZATION_BACKEND deve ser off, heuristico ou externo")
    if backend(s) == "externo":
        if not s.diarization_url.strip():
            out.append("DIARIZATION_BACKEND=externo exige DIARIZATION_URL")
        elif erro := checar_url_estatica(s.diarization_url.strip(), s.diarization_on_premise):
            out.append(
                erro.replace("LLM_BASE_URL", "DIARIZATION_URL").replace("LLM_ON_PREMISE", "DIARIZATION_ON_PREMISE")
            )
    if not 0.0 < s.diarization_limiar <= 1.0 or not 0.0 <= s.diarization_margem < s.diarization_limiar:
        out.append("DIARIZATION_LIMIAR deve estar em (0, 1] e DIARIZATION_MARGEM em [0, limiar)")
    return out


# ---------- vetores ----------
def normalizar(vec: object) -> tuple[float, ...]:
    """Vetor de voz válido (número finito, dimensão razoável, norma > 0) em norma unitária; senão ValueError."""
    if not isinstance(vec, (list, tuple)) or not DIM_MIN <= len(vec) <= DIM_MAX:
        raise ValueError(f"embedding precisa ser uma lista de {DIM_MIN} a {DIM_MAX} números")
    out: list[float] = []
    for x in vec:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > 1e6:
            raise ValueError("embedding com valor que não é número finito")
        out.append(float(x))
    norma = math.sqrt(sum(x * x for x in out))
    if norma < 1e-9:
        raise ValueError("embedding nulo")
    return tuple(x / norma for x in out)


def cosseno(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if len(a) != len(b):
        raise ValueError("dimensões diferentes")
    return sum(x * y for x, y in zip(a, b, strict=True))  # unitários


def _media(vetores: list[tuple[float, ...]], pesos: list[float]) -> tuple[float, ...]:
    dim = len(vetores[0])
    soma = [sum(v[i] * p for v, p in zip(vetores, pesos, strict=True)) for i in range(dim)]
    return normalizar(soma)


# ---------- backend heurístico ----------
_N_BANDAS = 32
_FREQS = [100.0 * (3800.0 / 100.0) ** (k / (_N_BANDAS - 1)) for k in range(_N_BANDAS)]
_QUADRO_N = 400  # 25 ms a 16 kHz
_MAX_QUADROS = 48
_JANELA = [0.54 - 0.46 * math.cos(2 * math.pi * i / (_QUADRO_N - 1)) for i in range(_QUADRO_N)]
_COEF = [2 * math.cos(2 * math.pi * f / stt_stream.TAXA) for f in _FREQS]


def _potencia(quadro: list[float]) -> list[float]:
    out = []
    for c in _COEF:
        s1 = s2 = 0.0
        for x in quadro:
            s1, s2 = x + c * s1 - s2, s1
        out.append(max(s1 * s1 + s2 * s2 - c * s1 * s2, 0.0))
    return out


def embedding_heuristico(pcm: bytes) -> tuple[float, ...] | None:
    """Perfil espectral (32 bandas, log, centrado) dos quadros com voz. None se não há fala."""
    a = stt_stream._amostras(pcm)
    limiar = stt_stream.LIMIAR_ENERGIA * 32768
    ativos = []
    for i in range(0, len(a) - _QUADRO_N, _QUADRO_N):
        q = a[i : i + _QUADRO_N]
        if math.sqrt(sum(x * x for x in q) / _QUADRO_N) >= limiar:
            ativos.append(i)
    if not ativos:
        return None
    passo = max(1, len(ativos) // _MAX_QUADROS)
    soma = [0.0] * _N_BANDAS
    n = 0
    for i in ativos[::passo][:_MAX_QUADROS]:
        quadro = [a[i + k] / 32768 * _JANELA[k] for k in range(_QUADRO_N)]
        for b, p in enumerate(_potencia(quadro)):
            soma[b] += p
        n += 1
    logs = [math.log10(p / n + 1e-9) for p in soma]
    media = sum(logs) / len(logs)
    try:
        return normalizar([x - media for x in logs])
    except ValueError:
        return None


def _turnos(a: array, pausa_s: float) -> list[tuple[int, int]]:
    """(início, fim) em quadros de 0,1 s de cada trecho de fala separado por pausa de ao menos `pausa_s`."""
    q = int(stt_stream.QUADRO * stt_stream.TAXA)
    voz = [not stt_stream.silencioso(a, k * q, (k + 1) * q) for k in range(len(a) // q)]
    minimo = max(1, math.ceil(pausa_s / stt_stream.QUADRO - 1e-9))
    turnos: list[list[int]] = []
    vazio = 0
    for k, v in enumerate(voz):
        if v:
            if turnos and vazio < minimo and vazio >= 0:
                turnos[-1][1] = k + 1
            else:
                turnos.append([k, k + 1])
            vazio = 0
        else:
            vazio += 1
    return [(i, j) for i, j in turnos if j - i >= 2]


def segmentar_heuristico(pcm: bytes) -> list[Trecho]:
    if len(pcm) > MAX_HEURISTICO_S * stt_stream.TAXA * 2:
        raise DiarizacaoErro("áudio longo demais para a diarização local")
    a = stt_stream._amostras(pcm)
    q = int(stt_stream.QUADRO * stt_stream.TAXA)
    bruto: list[tuple[float, float, tuple[float, ...]]] = []
    for i, j in _turnos(a, get_settings().diarization_pausa_s):
        emb = embedding_heuristico(a[i * q : j * q].tobytes())
        if emb is not None:
            bruto.append((i * stt_stream.QUADRO, j * stt_stream.QUADRO, emb))
    centros: list[tuple[float, ...]] = []
    ids: list[int] = []
    for _, _, emb in bruto:
        sims = [cosseno(emb, c) for c in centros]
        k = max(range(len(sims)), key=sims.__getitem__) if sims else -1
        if k >= 0 and sims[k] >= LIMIAR_AGRUPAR:
            ids.append(k)
        elif len(centros) < 6:
            centros.append(emb)
            ids.append(len(centros) - 1)
        else:
            ids.append(k)
    return [Trecho(i, f, f"S{k + 1}", emb) for (i, f, emb), k in zip(bruto, ids, strict=True)]


# ---------- backend externo ----------
_transporte: httpx.BaseTransport | None = None  # os testes injetam um transporte simulado


def _numero(x: object, nome: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise DiarizacaoErro(f"campo {nome} inválido na resposta da diarização")
    return float(x)


def interpretar_resposta(corpo: object) -> list[Trecho]:
    """Valida a resposta do serviço externo. Qualquer irregularidade invalida tudo (cai no comportamento antigo)."""
    if not isinstance(corpo, dict) or not isinstance(corpo.get("segmentos"), list):
        raise DiarizacaoErro("resposta da diarização fora do padrão")
    brutos = corpo["segmentos"]
    if len(brutos) > MAX_SEGMENTOS:
        raise DiarizacaoErro("segmentos demais na resposta da diarização")
    out: list[Trecho] = []
    dims: set[int] = set()
    for seg in brutos:
        if not isinstance(seg, dict):
            raise DiarizacaoErro("segmento fora do padrão")
        ini, fim = _numero(seg.get("inicio"), "inicio"), _numero(seg.get("fim"), "fim")
        if not 0 <= ini < fim <= 86400:
            raise DiarizacaoErro("tempos de segmento inválidos")
        fid = seg.get("falante_id")
        if isinstance(fid, bool) or not isinstance(fid, (str, int)) or not str(fid).strip() or len(str(fid)) > 64:
            raise DiarizacaoErro("falante_id inválido")
        emb = None
        if seg.get("embedding") is not None:
            try:
                emb = normalizar(seg["embedding"])
            except ValueError as exc:
                raise DiarizacaoErro(f"embedding inválido: {exc}") from exc
            dims.add(len(emb))
        out.append(Trecho(ini, fim, str(fid).strip(), emb))
    if len(dims) > 1:
        raise DiarizacaoErro("embeddings com dimensões diferentes na mesma resposta")
    return sorted(out, key=lambda t: t.inicio)


def segmentar_externo(audio: bytes, content_type: str) -> list[Trecho]:
    s = get_settings()
    url = s.diarization_url.strip()
    if not url:
        raise DiarizacaoErro("DIARIZATION_URL não configurada")
    try:
        ips = checar_destino(url, s.diarization_on_premise)
    except LLMError as exc:
        raise DiarizacaoErro(str(exc).replace("da IA", "da diarização")) from exc
    headers = {"Authorization": f"Bearer {s.diarization_api_key}"} if s.diarization_api_key else {}
    extensoes: dict[str, str] = {}
    if _transporte is None:  # conecta no IP validado (DNS rebinding); os testes injetam o transporte
        url, extra, extensoes = fixar_destino(url, ips)
        headers |= extra
    mime = (content_type or "audio/wav").split(";")[0].strip()
    try:
        with (
            httpx.Client(transport=_transporte, timeout=s.diarization_timeout_s, follow_redirects=False) as http,
            http.stream(
                "POST", url, files={"file": ("trecho", audio, mime)}, headers=headers, extensions=extensoes
            ) as r,
        ):
            if r.status_code >= 300:
                raise DiarizacaoErro(f"o serviço de diarização respondeu {r.status_code}")
            bruto = bytearray()
            limite_t = time.monotonic() + 3 * s.diarization_timeout_s  # o timeout do httpx é por leitura: pingo a pingo
            for pedaco in r.iter_bytes():  # corta no limite: resposta gigante não ocupa a memória
                bruto += pedaco
                if time.monotonic() > limite_t:
                    raise DiarizacaoErro("o serviço de diarização demorou demais")
                if len(bruto) > MAX_RESPOSTA_BYTES:
                    raise DiarizacaoErro("resposta da diarização grande demais")
    except httpx.TimeoutException as exc:
        raise DiarizacaoErro("o serviço de diarização demorou demais") from exc
    except httpx.HTTPError as exc:
        raise DiarizacaoErro("sem conexão com o serviço de diarização") from exc
    try:
        corpo = json.loads(bytes(bruto))
    except ValueError as exc:  # inclui JSON inválido, UnicodeDecodeError e inteiro gigante
        raise DiarizacaoErro("resposta da diarização não é JSON") from exc
    return interpretar_resposta(corpo)


def segmentar(audio: bytes, content_type: str) -> tuple[list[Trecho], str]:
    """(trechos, estado). Estado: off | ok | formato (heurístico só lê WAV 16 kHz mono) | falhou. Nunca levanta."""
    b = backend()
    if b == "off" or not audio:
        return [], "off"
    try:
        if b == "externo":
            return segmentar_externo(audio, content_type), "ok"
        mime = (content_type or "").split(";")[0].strip().lower()
        if mime not in ("audio/wav", "audio/x-wav", "audio/wave", "audio/l16"):
            return [], "formato"
        pcm = audio if mime == "audio/l16" else stt_stream.de_wav(audio)
        return segmentar_heuristico(pcm), "ok"
    except DiarizacaoErro as exc:
        log.warning("diarização falhou (%s): voltando ao falante do botão", exc)
        return [], "falhou"
    except Exception as exc:  # noqa: BLE001 - WAV truncado, formato estranho: nunca perder a fala por isso
        log.warning("diarização falhou (%s): voltando ao falante do botão", type(exc).__name__)
        return [], "falhou"


def segmentar_pcm(pcm: bytes) -> tuple[list[Trecho], str]:
    """Para o WebSocket: PCM 16 kHz mono int16 (o serviço externo recebe WAV)."""
    if not pcm or not ativo():
        return [], "off"
    if backend() == "externo":
        return segmentar(stt_stream.para_wav(pcm), "audio/wav")
    try:
        return segmentar_heuristico(pcm), "ok"
    except Exception as exc:  # noqa: BLE001
        log.warning("diarização falhou (%s): voltando ao falante do botão", type(exc).__name__)
        return [], "falhou"


# ---------- perfil de voz do profissional ----------
def carregar_perfil(db: Session, user_id: int) -> Perfil | None:
    """Só o perfil do próprio usuário. Vetor ilegível ou inválido vale como sem perfil."""
    try:
        row = db.scalar(select(PerfilVoz).where(PerfilVoz.user_id == user_id))
        if row is None:
            return None
        dados = row.embedding_json or {}
        return Perfil(normalizar(dados.get("v")), str(dados.get("backend", "")))
    except Exception:  # noqa: BLE001 - chave de cifra trocada/perdida, JSON estranho: vale como sem perfil, nunca 500
        return None


def _meta_perfil(db: Session, user_id: int) -> tuple[bool, dict | None, object]:
    """(existe, dados ou None se ilegível, linha). Perfil que não decifra (chave perdida) existe, mas não vale."""
    existe = db.scalar(select(PerfilVoz.id).where(PerfilVoz.user_id == user_id)) is not None
    if not existe:
        return False, None, None
    try:
        row = db.scalar(select(PerfilVoz).where(PerfilVoz.user_id == user_id))
        return True, dict(row.embedding_json or {}), row
    except Exception:  # noqa: BLE001
        return True, None, None


def estado_perfil(db: Session, user: User) -> dict:
    existe, dados, row = _meta_perfil(db, user.id)
    atual = backend()
    base = {
        "backend": atual,
        "diarizacao_ativa": atual != "off",
        "termo_versao": TERMO_VERSAO,
        "termo": TERMO,
        "limiar": get_settings().diarization_limiar,
        "cifrado": crypto.enabled(),  # sem DATA_ENCRYPTION_KEY o vetor fica em JSON puro (a tela avisa)
    }
    if not existe:
        return base | {"cadastrado": False}
    perfil = carregar_perfil(db, user.id)
    return base | {
        "cadastrado": True,
        "compativel": bool(perfil and perfil.backend == atual),  # outro backend ou ilegível: recadastrar
        "backend_do_perfil": (dados or {}).get("backend"),
        "dim": perfil.dim if perfil else None,
        "consentimento_em": row.consentimento_em.isoformat() if row is not None else None,
        "atualizado_em": row.updated_at.isoformat() if row is not None else None,
    }  # o vetor nunca sai por aqui


def _duracao_voz(trechos: list[Trecho]) -> dict[str, float]:
    d: dict[str, float] = {}
    for t in trechos:
        d[t.falante_id] = d.get(t.falante_id, 0.0) + (t.fim - t.inicio)
    return d


def _vetor_do_cluster(trechos: list[Trecho], fid: str) -> tuple[float, ...] | None:
    ts = [t for t in trechos if t.falante_id == fid and t.embedding]
    if not ts:
        return None
    dims = {len(t.embedding or ()) for t in ts}
    if len(dims) != 1:
        return None
    return _media([t.embedding for t in ts if t.embedding], [t.fim - t.inicio for t in ts])


def cadastrar_voz(db: Session, user: User, audio: bytes, content_type: str, termo_versao: str | None) -> dict:
    """Cadastra ou atualiza a voz do próprio profissional. Exige o consentimento da versão atual do termo."""
    if termo_versao != TERMO_VERSAO:
        raise HTTPException(
            428, "Consentimento explícito do uso da voz é obrigatório (aceite a versão atual do termo)."
        )
    if not ativo():
        raise HTTPException(409, "A diarização por voz está desativada neste servidor (DIARIZATION_BACKEND).")
    if not audio:
        raise HTTPException(422, "Amostra de voz vazia.")
    if len(audio) > MAX_AMOSTRA_BYTES:
        raise HTTPException(413, "Amostra de voz grande demais (máx. 5 MB).")
    if backend() == "heuristico":
        try:
            segundos = len(stt_stream.de_wav(audio)) / 2 / stt_stream.TAXA
        except Exception as exc:  # noqa: BLE001 - WAV truncado, outro formato, estéreo, outra taxa
            raise HTTPException(422, "Amostra inválida: envie WAV mono, 16 kHz, 16 bits.") from exc
        if not AMOSTRA_MIN_S <= segundos <= AMOSTRA_MAX_S:
            raise HTTPException(422, f"A amostra precisa ter de {int(AMOSTRA_MIN_S)} a {int(AMOSTRA_MAX_S)} segundos.")
    trechos, estado = segmentar(audio, content_type)
    if estado == "formato":
        raise HTTPException(415, "Com o backend local envie a amostra em WAV mono, 16 kHz, 16 bits.")
    if estado != "ok":
        raise HTTPException(503, "Não foi possível processar a amostra de voz agora. Tente novamente.")
    voz = _duracao_voz(trechos)
    total = sum(voz.values())
    if total < FALA_MIN_S:
        raise HTTPException(422, f"Pouca fala na amostra: grave ao menos {int(AMOSTRA_MIN_S)} segundos falando.")
    if total > AMOSTRA_MAX_S + 5:
        raise HTTPException(422, "Amostra longa demais (máx. 1 minuto de fala).")
    principal = max(voz, key=voz.__getitem__)
    if voz[principal] < 0.75 * total:
        raise HTTPException(422, "A amostra parece ter mais de uma voz. Grave sozinho(a), em ambiente silencioso.")
    vetor = _vetor_do_cluster(trechos, principal)
    if vetor is None:
        raise HTTPException(422, "O serviço de voz não devolveu o vetor da amostra.")
    existente = db.scalar(select(PerfilVoz.id).where(PerfilVoz.user_id == user.id))
    dados = {"v": list(vetor), "backend": backend(), "dim": len(vetor)}
    agora = clock.now()
    # troca (apaga e grava): um perfil antigo que não decifra mais (chave perdida) não pode travar o recadastro
    db.execute(delete(PerfilVoz).where(PerfilVoz.user_id == user.id))
    db.add(PerfilVoz(user_id=user.id, embedding_json=dados, termo_versao=TERMO_VERSAO, consentimento_em=agora))
    log_action(
        db,
        user,
        "consulta.voz_cadastrada",
        "user",
        user.id,
        {
            "backend": backend(),
            "dim": len(vetor),
            "duracao_fala_s": round(total, 1),
            "termo_versao": TERMO_VERSAO,
            "atualizacao": existente is not None,
        },
        commit=False,
    )
    db.commit()
    return estado_perfil(db, user)


def apagar_voz(db: Session, user: User, motivo: str = "pedido") -> bool:
    n = db.execute(delete(PerfilVoz).where(PerfilVoz.user_id == user.id)).rowcount
    if n:
        log_action(db, user, "consulta.voz_apagada", "user", user.id, {"motivo": motivo}, commit=False)
    db.commit()
    return bool(n)


def apagar_do_usuario(db: Session, user_id: int) -> None:
    """Exclusão da conta: o perfil de voz sai junto (o ON DELETE CASCADE cobre o banco; isto cobre o ORM/SQLite)."""
    db.execute(delete(PerfilVoz).where(PerfilVoz.user_id == user_id))


# ---------- atribuição do falante ----------
def atribuir(
    palavras: list[PalavraT], trechos: list[Trecho], perfil: Perfil | None, botao: str
) -> tuple[list[Fala], dict]:
    """Divide as palavras em falas por falante. Devolve ([], info) se não há o que decidir (comportamento antigo)."""
    s = get_settings()
    info: dict = {"clusters": 0}
    if not palavras or not trechos or botao not in LADO_PROFISSIONAL:
        return [], info
    ids = sorted({t.falante_id for t in trechos})
    info["clusters"] = len(ids)
    atual = backend()
    usar_perfil = perfil is not None and perfil.backend == atual
    if perfil is not None and not usar_perfil:
        info["perfil"] = "incompativel"
    sims: dict[str, float | None] = {}
    for fid in ids:
        v = _vetor_do_cluster(trechos, fid) if usar_perfil else None
        try:
            sims[fid] = cosseno(v, perfil.vetor) if v is not None and perfil is not None else None
        except ValueError:  # dimensão diferente do perfil: não compara
            sims[fid] = None
            info["perfil"] = "dimensao"
    comparavel = any(x is not None for x in sims.values())
    if len(ids) < 2 and not comparavel:
        return [], info  # um só falante e nada a comparar: o botão decide, como antes
    rotulo: dict[str, tuple[str, float | None, bool]] = {}
    duracao = _duracao_voz(trechos)
    ordem = list(dict.fromkeys(t.falante_id for t in sorted(trechos, key=lambda t: t.inicio)))
    if comparavel:
        limiar, piso = s.diarization_limiar, s.diarization_limiar - s.diarization_margem
        nao_prof: list[str] = []
        for fid in ids:
            sim = sims[fid]
            if sim is None:
                rotulo[fid] = ("desconhecido", None, True)
            elif sim >= limiar:
                rotulo[fid] = ("medico", round(min(sim, 1.0), 3), False)
            elif sim >= piso:
                rotulo[fid] = ("medico" if sim >= limiar - s.diarization_margem / 2 else "paciente", 0.5, True)
            else:
                rotulo[fid] = ("paciente", round(max(0.0, min(1.0, 1 - sim / piso)), 3), False)
                nao_prof.append(fid)
        nao_prof.sort(key=lambda f: -duracao[f])
        for k, fid in enumerate(nao_prof):
            lado = "acompanhante" if botao == "acompanhante" and k == 0 else "paciente"
            if k > 0:  # dois ou mais falantes que não são o profissional: quem é quem não se decide pela voz
                lado = "acompanhante" if lado == "paciente" else "paciente"
                rotulo[fid] = (lado, rotulo[fid][1], True)
            else:
                rotulo[fid] = (lado, rotulo[fid][1], rotulo[fid][2])
    else:
        # sem perfil utilizável: separa os turnos, mas quem é quem é palpite -> tudo incerto, a tela pede confirmação
        oposto = {"medico": "paciente", "paciente": "medico", "acompanhante": "medico"}.get(botao)
        for k, fid in enumerate(ordem):
            lado = botao if k == 0 else (oposto or "desconhecido")
            rotulo[fid] = (lado, None, True)
    # palavras -> trecho pelo ponto médio (ou o mais próximo); turnos consecutivos do mesmo falante viram uma fala
    ordenados = sorted(trechos, key=lambda t: t.inicio)

    def de_quem(p: PalavraT) -> str:
        meio = (p.inicio + p.fim) / 2
        dentro = [t for t in ordenados if t.inicio <= meio <= t.fim]
        t = dentro[0] if dentro else min(ordenados, key=lambda t: min(abs(meio - t.inicio), abs(meio - t.fim)))
        return t.falante_id

    falas: list[Fala] = []
    atual_id: str | None = None
    for p in palavras:
        fid = de_quem(p)
        if fid != atual_id:
            lado, conf, inc = rotulo[fid]
            falas.append(Fala("", [], p.inicio, lado, conf, inc))
            atual_id = fid
        falas[-1].palavras.append((p.texto, p.prob))
    for f in falas:
        f.texto = " ".join(t.strip() for t, _ in f.palavras).strip()
    info["falas"] = len(falas)
    info["incertas"] = sum(f.incerto for f in falas)
    return [f for f in falas if f.texto], info
