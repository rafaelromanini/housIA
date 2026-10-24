"""Transcrição no servidor (opcional), plugável por configuração.

Qualquer serviço compatível com `POST /v1/audio/transcriptions` (Whisper on-premise como faster-whisper-server,
ou um provedor contratado) funciona: STT_URL, STT_API_KEY e STT_MODEL. O áudio não é gravado pelo HousIA: é
repassado e descartado. Sem STT_URL, a tela usa o reconhecimento de fala do navegador (ver ADR 0009).
"""

import time
from dataclasses import dataclass, field

import httpx

from app.config import get_settings

EXTENSIONS = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/mpeg": "mp3", "audio/mp4": "m4a"}
MAX_BYTES = 15 * 1024 * 1024


class STTUnavailable(RuntimeError):
    """Mensagem amigável para a interface."""


_probe_cache: tuple[float, bool] = (0.0, False)
PROBE_TTL_S = 15.0


def _alcancavel(url: str) -> bool:
    """O serviço de fala responde? (GET /health na raiz do servidor, resultado guardado por 15 s)"""
    global _probe_cache
    now = time.monotonic()
    if now - _probe_cache[0] < PROBE_TTL_S and _probe_cache[0]:
        return _probe_cache[1]
    base = url.split("/v1/")[0].rstrip("/")
    try:
        ok = httpx.get(f"{base}/health", timeout=1.5).status_code < 500
    except httpx.HTTPError:
        ok = False
    _probe_cache = (now, ok)
    return ok


def enabled() -> bool:
    s = get_settings()
    if s.stt_engine == "fake":
        return True
    return bool(s.stt_url) and (not s.stt_probe or _alcancavel(s.stt_url))


def transcribe(audio: bytes, content_type: str, language: str, vocabulary: str = "") -> str:
    """`vocabulary`: dica de termos (glossário clínico) no campo `prompt` da API, que o Whisper usa como contexto."""
    settings = get_settings()
    if not settings.stt_url:
        raise STTUnavailable("Transcrição no servidor não configurada (STT_URL). Use o microfone do navegador.")
    if not audio:
        return ""
    if len(audio) > MAX_BYTES:
        raise STTUnavailable("Trecho de áudio grande demais (máx. 15 MB por envio).")
    mime = (content_type or "audio/webm").split(";")[0].strip()
    headers = {"Authorization": f"Bearer {settings.stt_api_key}"} if settings.stt_api_key else {}
    try:
        r = httpx.post(
            settings.stt_url,
            files={"file": (f"trecho.{EXTENSIONS.get(mime, 'webm')}", audio, mime)},
            data={"model": settings.stt_model, "language": language.split("-")[0], "response_format": "json"}
            | ({"prompt": vocabulary} if vocabulary else {}),
            headers=headers,
            timeout=60,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise STTUnavailable(f"Falha na transcrição do servidor: {exc}") from exc
    return str(r.json().get("text", "")).strip()


@dataclass(frozen=True)
class PalavraSTT:
    texto: str
    inicio: float
    fim: float
    prob: float | None = None


@dataclass
class Transcricao:
    texto: str
    palavras: list[PalavraSTT] = field(default_factory=list)
    avg_logprob: float | None = None  # média dos segmentos devolvidos (None: o serviço não informou)


def transcribe_words(audio: bytes, content_type: str, language: str, vocabulary: str = "") -> Transcricao:
    """Como `transcribe`, mas pede `verbose_json` com timestamps por palavra e lê `avg_logprob` dos segmentos."""
    settings = get_settings()
    if not settings.stt_url:
        raise STTUnavailable("Transcrição no servidor não configurada (STT_URL). Use o microfone do navegador.")
    if not audio:
        return Transcricao("")
    if len(audio) > MAX_BYTES:
        raise STTUnavailable("Trecho de áudio grande demais (máx. 15 MB por envio).")
    mime = (content_type or "audio/webm").split(";")[0].strip()
    headers = {"Authorization": f"Bearer {settings.stt_api_key}"} if settings.stt_api_key else {}
    try:
        r = httpx.post(
            settings.stt_url,
            files={"file": (f"trecho.{EXTENSIONS.get(mime, 'webm')}", audio, mime)},
            data={
                "model": settings.stt_model,
                "language": language.split("-")[0],
                "response_format": "verbose_json",
                "timestamp_granularities[]": ["word", "segment"],
            }
            | ({"prompt": vocabulary} if vocabulary else {}),
            headers=headers,
            timeout=60,
        )
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise STTUnavailable(f"Falha na transcrição do servidor: {exc}") from exc
    words = [
        PalavraSTT(
            str(w.get("word", "")).strip(), float(w.get("start", 0)), float(w.get("end", 0)), w.get("probability")
        )
        for w in (body.get("words") or [w for seg in body.get("segments") or [] for w in seg.get("words") or []])
        if str(w.get("word", "")).strip()
    ]
    logprobs = [float(s["avg_logprob"]) for s in body.get("segments") or [] if s.get("avg_logprob") is not None]
    return Transcricao(str(body.get("text", "")).strip(), words, sum(logprobs) / len(logprobs) if logprobs else None)
