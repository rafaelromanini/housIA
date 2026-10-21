#!/usr/bin/env python3
"""Monta data/reference/sigtap_procedimentos.csv e sigtap_proc_cid.csv (procedimentos do SUS e relação com a CID-10).

Dois modos:

  1. Demonstração (padrão, sem rede): deriva as tabelas de data/reference/glosa_rules (procedimentos e CID
     compatíveis curados pelo HousIA). Os códigos são DEMO-SIGTAP-NNN: não são códigos oficiais.
       python3 scripts/build_sigtap.py

  2. Competência fixa do DATASUS: lê os arquivos de largura fixa do pacote de tabelas unificadas do SIGTAP
     (tb_procedimento.txt e rl_procedimento_cid.txt, latin-1) e mantém só os procedimentos mapeados em --mapa
     (arquivo "chave=CODIGO" por linha, a chave é a do tabela_procedimentos.csv). Idade em meses no SIGTAP, aqui em anos.
       python3 scripts/build_sigtap.py --dir SIGTAP_202610 --competencia 202610 --mapa mapa.txt

Layouts (DATASUS, layout dos arquivos do SIGTAP): tb_procedimento = CO_PROCEDIMENTO(10) NO_PROCEDIMENTO(250)
TP_COMPLEXIDADE(1) TP_SEXO(1) QT_MAXIMA_EXECUCAO(4) QT_DIAS_PERMANENCIA(4) QT_PONTOS(4) VL_IDADE_MINIMA(4)
VL_IDADE_MAXIMA(4); rl_procedimento_cid = CO_PROCEDIMENTO(10) CO_CID(4) ST_PRINCIPAL(1) CO_COMPETENCIA(6).
"""

import argparse
import csv
from pathlib import Path

REF = Path(__file__).resolve().parent.parent / "data" / "reference"
GLOSA = REF / "glosa_rules"
FONTE_DEMO = "Curadoria HousIA (demonstração): código DEMO-*, não oficial; substituir por uma competência do SIGTAP/DATASUS"
SEXO_DEMO = {"cesarea": "F", "histerectomia": "F", "rtu_prostata": "M"}  # restrição de sexo do procedimento
PROC_HEADER = ["codigo", "chave", "descricao", "sexo", "idade_min_anos", "idade_max_anos", "competencia", "fonte"]
CID_HEADER = ["codigo", "cid_prefixo", "fonte"]


def read(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write(name: str, header: list[str], rows: list[list[str]]) -> None:
    with open(REF / name, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    print(f"{name}: {len(rows)} linhas")


def demo() -> None:
    procs = read(GLOSA / "tabela_procedimentos.csv")
    rel = {r["procedimento"]: r["cid_prefixos"].split("|") for r in read(GLOSA / "tabela_cid_procedimento.csv")}
    write(
        "sigtap_procedimentos.csv",
        PROC_HEADER,
        [
            [p["codigo_sigtap_demo"], p["procedimento"], p["descricao"], SEXO_DEMO.get(p["procedimento"], ""), "", "", "DEMO", FONTE_DEMO]
            for p in procs
        ],
    )
    code = {p["procedimento"]: p["codigo_sigtap_demo"] for p in procs}
    write(
        "sigtap_proc_cid.csv",
        CID_HEADER,
        [[code[k], pre, FONTE_DEMO] for k, prefixes in rel.items() if k in code for pre in prefixes],
    )


def dotted(cid: str) -> str:
    cid = cid.strip().upper()
    return cid if len(cid) <= 3 else f"{cid[:3]}.{cid[3:]}"


def real(folder: Path, competencia: str, mapa: Path) -> None:
    chave_de = dict(line.strip().split("=", 1)[::-1] for line in mapa.read_text().splitlines() if "=" in line)
    fonte = f"SIGTAP/DATASUS, competência {competencia}"
    procs, cids = [], []
    for line in (folder / "tb_procedimento.txt").read_text(encoding="latin-1").splitlines():
        co = line[0:10]
        if co not in chave_de:
            continue
        sexo = {"M": "M", "F": "F"}.get(line[261:262], "")
        imin, imax = line[278:282].strip(), line[282:286].strip()
        anos = lambda v: "" if v in ("", "9999") else str(int(v) // 12)  # noqa: E731
        procs.append([co, chave_de[co], line[10:260].strip(), sexo, anos(imin), anos(imax), competencia, fonte])
    for line in (folder / "rl_procedimento_cid.txt").read_text(encoding="latin-1").splitlines():
        if line[0:10] in chave_de:
            cids.append([line[0:10], dotted(line[10:14]), fonte])
    write("sigtap_procedimentos.csv", PROC_HEADER, procs)
    write("sigtap_proc_cid.csv", CID_HEADER, cids)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path)
    ap.add_argument("--competencia", default="")
    ap.add_argument("--mapa", type=Path)
    a = ap.parse_args()
    if a.dir:
        if not (a.competencia and a.mapa):
            ap.error("--dir exige --competencia e --mapa")
        real(a.dir, a.competencia, a.mapa)
    else:
        demo()
