"""Ditado e adendo (#41): regras puras, sem banco.

O profissional dita (ou digita) o que quer acrescentar a uma seção da nota, mesmo depois de a consulta ser finalizada.
Cada fala ditada vira uma `Fala/N` de origem "adendo" e cada frase nova da seção cita essa fala. Nenhuma IA entra:
o texto é o do profissional, palavra por palavra, e a fonte é a fala dele. Os comandos de voz só valem quando são a
fala inteira (nunca no meio de uma frase) e agem só sobre a seção alvo.
"""

from __future__ import annotations

import re
import unicodedata

from app.consult import notas
from app.consult.text import norm

MAX_SECAO = 8000  # caracteres por seção depois do ditado
MAX_PILHA = 20  # "desfazer" guarda os últimos passos de toda a consulta
SEM_REGISTRO = "Sem registro na consulta."
_INVISIVEIS = re.compile("[​-‏‪-‮⁠-⁤﻿­]")
_CONTROLE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

COMANDOS = {
    "proximo_campo": re.compile(r"^(proximo campo|proxima secao|proximo|avancar)$"),
    "apagar_ultima": re.compile(r"^(apagar|apague|remover|remova) (a )?ultima frase$"),
    "desfazer": re.compile(r"^(desfazer|desfaca)( ultima acao)?$"),
    "nova_linha": re.compile(r"^(nova linha|novo paragrafo|quebra de linha)$"),
}
_PONTUACAO = " .,;:!?\"'“”"


def limpar(texto: str) -> str:
    """Texto ditado sem caracteres de controle ou invisíveis; as quebras de linha viram espaço."""
    t = unicodedata.normalize("NFC", texto or "")
    t = _INVISIVEIS.sub("", _CONTROLE.sub(" ", t))
    return re.sub(r"\s+", " ", t).strip()


def comando(texto: str) -> str | None:
    """O comando de voz que a fala inteira é (ou None). "Próximo campo" no meio de uma frase é texto, não comando."""
    n = norm(texto).strip(_PONTUACAO)
    return next((nome for nome, rx in COMANDOS.items() if rx.match(n)), None)


def frases_do_ditado(texto: str, ref: str) -> list[dict]:
    """Uma frase por sentença ditada, cada uma com a fala do adendo como fonte (origem do profissional)."""
    return [notas.frase(s, [ref], "profissional") for s in notas.split_sentences(texto)]


def _frases(sec: dict) -> list[dict]:
    texto = sec.get("texto", "")
    if notas.is_placeholder(texto):
        return []
    return [dict(f) for f in sec.get("frases") or notas.derive(texto, sec.get("refs", []))]


def acrescentar(sec: dict, novas: list[dict], quebra: bool) -> dict:
    base = "" if notas.is_placeholder(sec.get("texto", "")) else sec.get("texto", "").rstrip()
    texto = base
    for i, f in enumerate(novas):
        if not texto:
            texto = f["texto"]
            continue
        sep = "\n" if (quebra and i == 0) or "\n" in base else "; " if "; " in base else " "
        texto = texto + sep + f["texto"]
    refs = list(dict.fromkeys([*sec.get("refs", []), *(r for f in novas for r in f["refs"])]))
    return sec | {"texto": texto, "refs": refs, "frases": [*_frases(sec), *novas]}


def sem_ultima(sec: dict) -> tuple[dict, dict] | None:
    """(seção sem a última frase, a frase removida); None se não há frase."""
    frases = _frases(sec)
    if not frases:
        return None
    ultima, resto = frases[-1], frases[:-1]
    texto = sec.get("texto", "").rstrip()
    alvo = ultima["texto"].strip()
    if texto.endswith(alvo):
        novo = texto[: len(texto) - len(alvo)].rstrip(" ;\n")
    else:
        novo = " ".join(f["texto"] for f in resto)
    refs = list(dict.fromkeys(r for f in resto for r in f["refs"]))
    if not resto:
        return sec | {"texto": SEM_REGISTRO, "refs": [], "frases": []}, ultima
    return sec | {"texto": novo, "refs": refs, "frases": resto}, ultima
