#!/usr/bin/env python3
"""Confere e importa a tabela do Rol da ANS/DUT e/ou de exigências de operadora para a autorização prévia (#56).

Nada é gravado se houver qualquer erro: o relatório lista linha, campo e motivo. O que for gravado entra como
origem=importado e conferencia=pendente (uma pessoa confere o texto contra a norma vigente e muda para "conferido").
Depois, aponte AUTORIZACAO_REGRAS_CSV / AUTORIZACAO_OPERADORAS_CSV para os arquivos gerados.

  cd apps/api && .venv/bin/python ../../scripts/importar_rol_dut.py --regras rol.csv --operadoras ops.csv --saida ../../data/instituicao
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "api"))

from app.consult import autorizacao_regras as regras  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regras", type=Path, help="CSV do Rol/DUT (colunas em autorizacao_regras.COLUNAS_REGRAS)")
    ap.add_argument("--operadoras", type=Path, help="CSV de exigências de operadora")
    ap.add_argument("--saida", type=Path, default=Path("."), help="pasta onde gravar rol_dut_importado.csv e operadoras_importado.csv")
    ap.add_argument("--so-validar", action="store_true", help="só mostra o relatório")
    args = ap.parse_args()
    if not args.regras and not args.operadoras:
        ap.error("informe --regras e/ou --operadoras")
    falhou = False
    for tabela, origem, nome in (("regras", args.regras, "rol_dut_importado.csv"), ("operadoras", args.operadoras, "operadoras_importado.csv")):
        if not origem:
            continue
        texto = origem.read_text(encoding="utf-8-sig")
        rel = regras.validar_regras(texto) if tabela == "regras" else regras.validar_operadoras(texto)
        print(f"{tabela}: {rel.linhas} linha(s), {rel.aceitas} aceita(s), {len(rel.erros)} erro(s)")
        for e in rel.erros[: regras.MAX_ERROS]:
            print(f"  linha {e['linha']} [{e['campo']}]: {e['mensagem']}")
        if not rel.ok:
            falhou = True
        elif not args.so_validar:
            args.saida.mkdir(parents=True, exist_ok=True)
            regras.gravar_importacao(texto, tabela, args.saida / nome)
            print(f"  gravado em {args.saida / nome} (conferência pendente)")
    return 1 if falhou else 0


if __name__ == "__main__":
    raise SystemExit(main())
