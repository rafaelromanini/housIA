"""Dados curados da jornada (data/reference/journey_*.yaml): regras, parâmetros e roteiro do contato pós-alta."""

from functools import lru_cache

import yaml

from app.reference import _ref_dir

PAPEIS = ("medico", "enfermagem", "gestor")
PAPEL_LABEL = {"medico": "Médico(a)", "enfermagem": "Enfermagem", "gestor": "Gestão"}
ANSWERS = ("sim", "nao", "nao_se_aplica")


def _load(name: str) -> dict:
    with open(_ref_dir() / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache
def config() -> dict:
    return _load("journey_rules.yaml")


def params() -> dict:
    return config()["parametros"]


def rule_meta(rule: str) -> dict:
    return config()["regras"][rule]


@lru_cache
def script() -> tuple[dict, ...]:
    return tuple(_load("journey_contact_script.yaml")["perguntas"])


def validate() -> list[str]:
    """Problemas de consistência dos arquivos (usado nos testes)."""
    problems = []
    for rid, meta in config()["regras"].items():
        if meta["papel"] not in PAPEIS:
            problems.append(f"{rid}: papel inválido")
        if not meta.get("fonte") or not meta.get("titulo") or int(meta["prazo_horas"]) <= 0:
            problems.append(f"{rid}: regra sem fonte, título ou prazo")
    ids = [q["id"] for q in script()]
    problems += [f"pergunta repetida: {i}" for i in set(ids) if ids.count(i) > 1]
    for q in script():
        if q["tipo"] not in ("confirmacao", "teach_back", "retirada", "sinal_alarme"):
            problems.append(f"{q['id']}: tipo inválido")
        if q["tipo"] == "sinal_alarme" and not q.get("alarme"):
            problems.append(f"{q['id']}: sinal de alarme sem rótulo")
    return problems
