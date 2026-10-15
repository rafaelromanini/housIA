import json
from datetime import UTC, datetime
from functools import lru_cache

from app.config import get_settings
from app.seed import background
from app.seed.builder import build_all


@lru_cache
def load_scenarios() -> list[dict]:
    """Cenários roteirizados (data/scenarios/*.json), em ordem de nome de arquivo."""
    folder = get_settings().data_dir / "scenarios"
    out = []
    for path in sorted(folder.glob("*.json")):
        with open(path, encoding="utf-8") as fh:
            out.append(json.load(fh))
    return out


def scenario_meta() -> dict[str, dict]:
    return {
        s["id"]: {"id": s["id"], "tag": s.get("tag"), "titulo": s.get("titulo"), "esperado": s.get("esperado")}
        for s in load_scenarios()
    }


def build_demo_resources(anchor: datetime | None = None, include_background: bool = True) -> list[dict]:
    scenarios = list(load_scenarios())
    if include_background:
        scenarios += background.generate()
    return build_all(scenarios, anchor or datetime.now(UTC))
