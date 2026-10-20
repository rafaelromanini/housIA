"""Criptografia de campo (AES-GCM) para as colunas JSON com texto clínico.

`DATA_ENCRYPTION_KEY` aceita uma ou mais chaves `id:segredo` separadas por vírgula; a primeira cifra, todas decifram
(rotação: coloque a nova chave na frente e mantenha a antiga até reprocessar). O segredo é uma chave de 32 bytes em
base64; qualquer outro texto vira chave por SHA-256. O valor guardado é `enc:v1:<id>:<nonce+texto cifrado em base64>`.
Sem chave, o valor segue como JSON puro (ambiente de desenvolvimento) e o painel de privacidade diz isso.
"""

import base64
import binascii
import hashlib
import json
import logging
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

PREFIX = "enc:v1:"
logger = logging.getLogger("housia")
_warned = False


def _key_bytes(secret: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
        if len(raw) == 32:
            return raw
    except (binascii.Error, ValueError):
        pass
    return hashlib.sha256(secret.encode()).digest()


def keys() -> list[tuple[str, bytes]]:
    """Chaves configuradas, a de cifrar primeiro."""
    out: list[tuple[str, bytes]] = []
    for i, part in enumerate(p.strip() for p in get_settings().data_encryption_key.split(",") if p.strip()):
        kid, sep, secret = part.partition(":")
        if not sep:  # só o segredo, sem id
            kid, secret = f"k{i + 1}", part
        if not kid.strip() or not secret.strip():  # "k1:" ou ":x" viraria uma chave adivinhável por SHA-256
            raise ValueError(
                f"DATA_ENCRYPTION_KEY inválida na posição {i + 1}: use id:segredo com os dois preenchidos."
            )
        if any(kid == k for k, _ in out):
            raise ValueError(f"DATA_ENCRYPTION_KEY repete o id '{kid}': cada chave precisa de um id único.")
        out.append((kid, _key_bytes(secret)))
    return out


def enabled() -> bool:
    return bool(keys())


def current_key_id() -> str | None:
    ks = keys()
    return ks[0][0] if ks else None


def is_encrypted(raw: Any) -> bool:
    return isinstance(raw, str) and raw.startswith(PREFIX)


def encrypt(value: Any) -> str:
    """JSON do valor, cifrado com a chave atual (ou o JSON puro se não houver chave)."""
    global _warned
    plain = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    ks = keys()
    if not ks:
        if not _warned:
            logger.warning("DATA_ENCRYPTION_KEY vazia: colunas clínicas gravadas sem criptografia.")
            _warned = True
        return plain
    kid, key = ks[0]
    nonce = os.urandom(12)
    blob = nonce + AESGCM(key).encrypt(nonce, plain.encode(), kid.encode())
    return f"{PREFIX}{kid}:{base64.urlsafe_b64encode(blob).decode()}"


def decrypt(raw: str) -> Any:
    """Inverso de `encrypt`; aceita JSON puro (dados anteriores à criptografia)."""
    if not is_encrypted(raw):
        return json.loads(raw)
    kid, _, payload = raw[len(PREFIX) :].partition(":")
    blob = base64.urlsafe_b64decode(payload)
    candidates = [k for i, k in keys() if i == kid]
    if not candidates:
        raise ValueError(f"Chave de criptografia '{kid}' não está em DATA_ENCRYPTION_KEY.")
    plain = AESGCM(candidates[0]).decrypt(blob[:12], blob[12:], kid.encode())
    return json.loads(plain)
