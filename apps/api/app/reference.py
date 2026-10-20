"""Tabelas de referência curadas (data/reference). Base de DEMONSTRAÇÃO, não exaustiva."""

import csv
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import get_settings


def _ref_dir() -> Path:
    return get_settings().data_dir / "reference"


def _read_csv(name: str) -> list[dict[str, str]]:
    with open(_ref_dir() / name, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@dataclass(frozen=True)
class Drug:
    ingredient: str
    name: str
    atc: str
    drug_class: str


@lru_cache
def drugs() -> dict[str, Drug]:
    return {
        r["principio_ativo"]: Drug(r["principio_ativo"], r["nome"], r["atc"], r["classe"])
        for r in _read_csv("drugs.csv")
    }


@lru_cache
def class_labels() -> dict[str, str]:
    return {r["classe"]: r["rotulo"] for r in _read_csv("drug_classes.csv")}


def class_label(drug_class: str) -> str:
    """Nome legível da classe terapêutica (o código fica para as regras)."""
    return class_labels().get(drug_class, drug_class.replace("_", " "))


@lru_cache
def drugs_by_atc() -> dict[str, Drug]:
    return {d.atc: d for d in drugs().values()}


@lru_cache
def interactions() -> list[dict[str, str]]:
    return _read_csv("interactions.csv")


@lru_cache
def high_alert() -> list[dict[str, str]]:
    return _read_csv("high_alert_meds.csv")


@lru_cache
def charlson() -> list[dict[str, str]]:
    return _read_csv("charlson_icd10.csv")


@lru_cache
def adverse_patterns() -> list[dict[str, str]]:
    return _read_csv("adverse_patterns.csv")


@dataclass(frozen=True)
class Cid10Entry:
    code: str
    description: str
    kind: str  # "categoria" (3 caracteres) ou "subcategoria"
    sex: str  # "F" ou "M" quando o código só vale para um sexo


@lru_cache
def cid10() -> dict[str, Cid10Entry]:
    """CID-10 oficial (DATASUS, revisão 2008): categorias e subcategorias, por código com ponto (J06.9).

    Montada por scripts/build_cid10.py a partir dos arquivos públicos do DATASUS.
    """
    return {r["codigo"]: Cid10Entry(r["codigo"], r["descricao"], r["tipo"], r["sexo"]) for r in _read_csv("cid10.csv")}


def norm_icd(code: str | None) -> str:
    """ "j45.9", "J459" e " J45,9 " viram "J45.9"; o que não tem o formato de um código vira ""."""
    c = (code or "").strip().upper().replace(",", ".").replace(" ", "")
    if len(c) >= 4 and c[3] != ".":
        c = f"{c[:3]}.{c[3:]}"
    return c if re.fullmatch(r"[A-Z]\d{2}(\.\d{1,2})?", c) else ""


def valid_icd(code: str | None) -> bool:
    """O código existe na CID-10 oficial (categoria ou subcategoria)? Código inventado ("J99.9") não existe."""
    return norm_icd(code) in cid10()


def icd_entry(code: str | None) -> Cid10Entry | None:
    return cid10().get(norm_icd(code))


def icd_problem(code: str | None, sex: str = "", primary: bool = False) -> str | None:
    """Motivo para recusar o código num diagnóstico/hipótese do paciente, ou None se pode entrar.

    `primary`: hipótese ou diagnóstico principal. O capítulo XX (causas externas, V01–Y98) só complementa um
    diagnóstico e não pode ser a hipótese.
    """
    entry = icd_entry(code)
    if entry is None:
        return "CID inexistente"
    if primary and entry.code[0] in "VWXY":
        return "CID de causa externa não é diagnóstico"
    if entry.sex and sex in ("F", "M") and entry.sex != sex:
        return f"CID restrito ao sexo {'feminino' if entry.sex == 'F' else 'masculino'}"
    return None


@lru_cache
def social_weights() -> dict:
    with open(_ref_dir() / "social_weights.json", encoding="utf-8") as fh:
        return json.load(fh)


def matches(token: str, ingredient: str, drug_class: str) -> bool:
    """Token da tabela: '@classe' casa pela classe terapêutica; senão, pelo princípio ativo."""
    if token.startswith("@"):
        return token[1:] == drug_class
    return token == ingredient


def high_alert_reason(ingredient: str, drug_class: str) -> str | None:
    """Motivo de alto alerta (MPP) se o medicamento constar em high_alert_meds.csv."""
    for row in high_alert():
        if (row["tipo"] == "classe" and row["item"] == drug_class) or (
            row["tipo"] == "ingrediente" and row["item"] == ingredient
        ):
            return row["motivo"]
    return None
