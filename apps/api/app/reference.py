"""Tabelas de referência curadas (data/reference). Base de DEMONSTRAÇÃO, não exaustiva."""

import csv
import json
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
