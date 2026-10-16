#!/usr/bin/env python3
"""Monta data/reference/cid10.csv: categorias e subcategorias da CID-10 (revisão 2008, a que o DATASUS publica).

Origem dos dados: Ministério da Saúde / DATASUS, http://www2.datasus.gov.br/cid10/V2008/downloads/CID10CSV.zip
(arquivos CID-10-CATEGORIAS.CSV e CID-10-SUBCATEGORIAS.CSV, latin-1, separador ";"). São dados públicos.

Duas formas de rodar:

  1. Direto do zip do DATASUS (quando a rede alcança o servidor):
       python3 scripts/build_cid10.py --zip CID10CSV.zip
  2. Das tabelas JSON do pacote npm "@br-health-kit/cid10" (MIT), que redistribui o mesmo conteúdo do DATASUS sem
     alterar os dados (npm pack @br-health-kit/cid10 && tar xzf *.tgz):
       python3 scripts/build_cid10.py --json package/data/2008/tables

Saída (UTF-8, ordenada por código): codigo (com ponto nas subcategorias: J06.9), descricao, tipo (categoria ou
subcategoria) e sexo (F ou M quando o código só vale para um sexo; vazio nos demais). Tanto a categoria de 3
caracteres quanto a subcategoria são códigos válidos: a regra do app é "o código existe nesta tabela".
"""

import argparse
import csv
import io
import json
import sys
import zipfile
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "reference" / "cid10.csv"
SEX = {"M": "M", "F": "F", "male": "M", "female": "F"}
# Descrições erradas na origem (conferidas contra a CID-10 da OMS): a origem repete a de J20.9 em J21.9. O código existe
# e vale; só o texto mostrado ao profissional é corrigido.
CORRECOES = {"J21.9": "Bronquiolite aguda não especificada"}


def dotted(code: str) -> str:
    """A000 -> A00.0 (o zip do DATASUS traz a subcategoria sem ponto)."""
    code = code.strip().upper()
    return code if "." in code or len(code) <= 3 else f"{code[:3]}.{code[3:]}"


def from_zip(path: Path) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    with zipfile.ZipFile(path) as z:
        names = {n.upper(): n for n in z.namelist()}

        def read(suffix: str):
            name = next(n for u, n in names.items() if u.endswith(suffix))
            return csv.DictReader(io.StringIO(z.read(name).decode("latin-1")), delimiter=";")

        for r in read("CATEGORIAS.CSV"):
            rows.append((r["CAT"].strip(), r["DESCRICAO"].strip(), "categoria", ""))
        for r in read("SUBCATEGORIAS.CSV"):
            rows.append(
                (dotted(r["SUBCAT"]), r["DESCRICAO"].strip(), "subcategoria", SEX.get(r.get("RESTRSEXO", "").strip(), ""))
            )
    return rows


def from_json(folder: Path) -> list[tuple[str, str, str, str]]:
    rows = []
    for table, kind in (("categories", "categoria"), ("subcategories", "subcategoria")):
        for r in json.loads((folder / f"{table}.json").read_text(encoding="utf-8")):
            rows.append((r["code"], r["description"].strip(), kind, SEX.get(r.get("sexRestriction") or "", "")))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--zip", type=Path, help="CID10CSV.zip do DATASUS")
    src.add_argument("--json", type=Path, help="pasta com categories.json e subcategories.json")
    ap.add_argument("--saida", type=Path, default=OUT)
    args = ap.parse_args()
    rows = from_zip(args.zip) if args.zip else from_json(args.json)
    rows = [(c, CORRECOES.get(c, d), t, x) for c, d, t, x in rows]
    rows = sorted(set(rows), key=lambda r: r[0])
    codes = [r[0] for r in rows]
    if len(codes) != len(set(codes)):
        sys.exit("código repetido na origem")
    with open(args.saida, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["codigo", "descricao", "tipo", "sexo"])
        w.writerows(rows)
    print(f"{len(rows)} códigos gravados em {args.saida}")


if __name__ == "__main__":
    main()
