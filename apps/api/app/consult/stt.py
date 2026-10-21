"""Transcrição no servidor (opcional), plugável por configuração.

Qualquer serviço compatível com `POST /v1/audio/transcriptions` (Whisper on-premise como faster-whisper-server,
ou um provedor contratado) funciona: STT_URL, STT_API_KEY e STT_MODEL. O áudio não é gravado pelo HousIA: é
repassado e descartado. Sem STT_URL, a tela usa o reconhecimento de fala do navegador (ver ADR 0009).
"""

import httpx

from app.config import get_settings

EXTENSIONS = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/mpeg": "mp3", "audio/mp4": "m4a"}
MAX_BYTES = 15 * 1024 * 1024


class STTUnavailable(RuntimeError):
    """Mensagem amigável para a interface."""


def enabled() -> bool:
    return bool(get_settings().stt_url)


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
