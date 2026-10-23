#!/usr/bin/env python3
"""WER da transcrição no CI (#38): áudio sintético do cenário cen6 → motor de fala → texto → WER.

Sem baixar modelo: o áudio é sintetizado pelo `stt_stream.sintetizar` (um tom por palavra, vocabulário do próprio
cenário) e decodificado pelo `FakeMotor` dentro da mesma `Sessao` do WebSocket (janela deslizante, VAD,
deduplicação), seguida da correção por similaridade (`stt_vocab.corrigir`). O número mede, portanto, a fidelidade do
caminho de transmissão do HousIA (fronteira de janela, palavra cortada ou repetida, correção), não a acurácia de um
modelo. Com `--stt-url` (e áudio real em `--audio`) o mesmo cálculo mede um serviço Whisper de verdade.

Uso (a partir de apps/api): PYTHONPATH=. python ../../scripts/wer.py [--max-wer 0.15] [--falas 12] [--json saida.json]
Sai com código 1 se o WER passar do limite (STT_WER_MAX, padrão 0,15).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "apps" / "api"))

from app.config import get_settings  # noqa: E402
from app.consult import stt_stream, stt_vocab  # noqa: E402

BLOCO = stt_stream.TAXA  # 500 ms de PCM int16 = 16000 bytes
MAX_PALAVRAS_DISTINTAS = 150  # o motor falso usa um tom por palavra: acima disso as frequências se aproximam demais


def tokens(texto: str) -> list[str]:
    return re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)*|\d+(?:[.,]\d+)?", texto.lower())


def wer(ref: list[str], hip: list[str]) -> float:
    """(substituições + remoções + inserções) / palavras da referência, por distância de edição."""
    if not ref:
        return 0.0 if not hip else 1.0
    anterior = list(range(len(hip) + 1))
    for i, r in enumerate(ref, 1):
        atual = [i]
        for j, h in enumerate(hip, 1):
            atual.append(min(anterior[j] + 1, atual[j - 1] + 1, anterior[j - 1] + (r != h)))
        anterior = atual
    return anterior[-1] / len(ref)


def falas_cen6(n: int) -> list[str]:
    dados = json.loads((RAIZ / "data" / "consultations" / "cen6_consulta.json").read_text(encoding="utf-8"))
    falas: list[str] = []
    distintas: set[str] = set()
    for f in dados["falas"]:
        t = tokens(f["texto"])
        if len(falas) >= n or len(distintas | set(t)) > MAX_PALAVRAS_DISTINTAS:
            break
        falas.append(" ".join(t))
        distintas |= set(t)
    return falas


def transmitir(frases: list[str]) -> list[str]:
    vocab = sorted({p for f in frases for p in f.split()})
    pcm = stt_stream.sintetizar(frases, vocab=vocab)
    sessao = stt_stream.Sessao(stt_stream.FakeMotor(vocab))
    finais: list[str] = []
    for i in range(0, len(pcm), BLOCO):
        finais += [e.texto for e in sessao.adicionar(pcm[i : i + BLOCO]) if e.tipo == "final"]
    finais += [e.texto for e in sessao.encerrar()]
    return finais


def medir(n_falas: int = 12) -> dict:
    frases = falas_cen6(n_falas)
    ref = [p for f in frases for p in f.split()]
    finais = transmitir(frases)
    bruto = " ".join(finais)
    corrigido, correcoes = stt_vocab.corrigir(bruto)
    hip = tokens(corrigido)
    return {
        "cenario": "cen6",
        "falas": len(frases),
        "palavras_referencia": len(ref),
        "wer_bruto": round(wer(ref, tokens(bruto)), 4),
        "wer": round(wer(ref, hip), 4),
        "correcoes": len(correcoes),
        "motor": "FakeMotor (sintético, determinístico)",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-wer", type=float, default=None, help="limite (padrão: STT_WER_MAX)")
    ap.add_argument("--falas", type=int, default=12)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    limite = args.max_wer if args.max_wer is not None else get_settings().stt_wer_max
    res = medir(args.falas) | {"limite": limite}
    res["ok"] = res["wer"] <= limite
    texto = json.dumps(res, ensure_ascii=False, indent=1)
    print(texto)
    if args.json:
        args.json.write_text(texto, encoding="utf-8")
    resumo = os.environ.get("GITHUB_STEP_SUMMARY")
    if resumo:
        with open(resumo, "a", encoding="utf-8") as fh:
            fh.write(f"### WER da transcrição (cen6 sintético)\n\nWER **{res['wer']:.2%}** (limite {limite:.0%}), ")
            fh.write(f"{res['palavras_referencia']} palavras, motor sintético.\n")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
