"""Registro de Smart Apps da consulta (#21).

Cada app é um módulo desta pasta com `MANIFEST` (id, nome, aba, gatilho, especialidades, recursos FHIR lidos, base
curada e versão, camada regra ou IA, schema de saída) e `run(ctx)`, que devolve itens com refs (ou None se o app não
se aplica). O registro descobre os módulos, valida o manifest, roda os que o gatilho liberou e descarta item sem
referência válida. Criar um app novo: ver docs/guias/como-criar-um-smart-app.md.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from app.config import get_settings
from app.consult import knowledge
from app.consult.apps.base import AppContext, Manifest, RunFn
from app.consult.schemas import AppItem, AppResult

log = logging.getLogger(__name__)
_SKIP = {"base"}


@dataclass(frozen=True)
class SmartApp:
    manifest: Manifest
    run: RunFn


_registry: dict[str, SmartApp] = {}
_loaded = False


def validate(manifest: Manifest, extra_dirs: tuple[Path, ...] = ()) -> None:
    """Confere o que o pydantic não alcança: especialidades existentes e base curada presente em data/reference
    (um plug-in local pode trazer o CSV na própria pasta)."""
    unknown = [s for s in manifest.especialidades if s not in knowledge.SPECIALTIES]
    if unknown:
        raise ValueError(f"app {manifest.id}: especialidade desconhecida {unknown}")
    if manifest.base is not None:
        dirs = (get_settings().data_dir / "reference", *extra_dirs)
        if not any((d / manifest.base.arquivo).exists() for d in dirs):
            raise ValueError(f"app {manifest.id}: base curada ausente ({manifest.base.arquivo})")


def register(module: ModuleType, extra_dirs: tuple[Path, ...] = ()) -> SmartApp:
    manifest = getattr(module, "MANIFEST", None)
    run = getattr(module, "run", None)
    if not isinstance(manifest, Manifest) or not callable(run):
        raise ValueError(f"{module.__name__}: o módulo precisa de MANIFEST (Manifest) e run(ctx)")
    validate(manifest, extra_dirs)
    if manifest.id in _registry and _registry[manifest.id].run is not run:
        raise ValueError(f"app duplicado: {manifest.id}")
    app = SmartApp(manifest, run)
    _registry[manifest.id] = app
    return app


def discover() -> dict[str, SmartApp]:
    """Importa todos os módulos desta pasta (uma vez)."""
    global _loaded
    if not _loaded:
        for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
            if info.name not in _SKIP:
                register(importlib.import_module(f"{__name__}.{info.name}"))
        _loaded = True
    return _registry


def load_file(path: Path) -> SmartApp:
    """Registra um módulo de app que está fora do pacote (plug-in local e testes)."""
    name = f"housia_smart_app_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"não consegui carregar {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return register(module, (path.parent,))


def unregister(app_id: str) -> None:
    _registry.pop(app_id, None)


def all_apps() -> list[SmartApp]:
    return sorted(discover().values(), key=lambda a: (a.manifest.ordem, a.manifest.id))


def catalog(specialty: str | None = None) -> list[dict]:
    """Manifests para GET /smart-apps (a tela monta abas e o menu de perfil por eles)."""
    out = []
    for a in all_apps():
        m = a.manifest
        if specialty and m.especialidades and specialty not in m.especialidades:
            continue
        out.append(m.model_dump(mode="json"))
    return out


def applies(m: Manifest, ctx: AppContext) -> bool:
    g = m.gatilho
    r = ctx.record
    if ctx.kind not in g.tipos:
        return False
    if m.especialidades and ctx.specialty not in m.especialidades:
        return False
    if g.internado is not None and ctx.internado != g.internado:
        return False
    if g.sexo and r.sex != g.sexo:
        return False
    if r.age is not None and (
        (g.idade_min is not None and r.age < g.idade_min) or (g.idade_max is not None and r.age > g.idade_max)
    ):
        return False
    return True


def _clean(m: Manifest, items: list[AppItem], valid: set[str] | None) -> tuple[list[AppItem], list[dict]]:
    kept, dropped = [], []
    for it in items:
        refs = [r for r in it.refs if valid is None or r in valid]
        if m.refs_obrigatorias and not refs:
            dropped.append({"app": m.id, "texto": it.texto, "motivo": "sem referência válida"})
            continue
        kept.append(it.model_copy(update={"refs": refs}))
    return kept, dropped


def run_one(app: SmartApp, ctx: AppContext, valid: set[str] | None = None) -> AppResult | None:
    m = app.manifest
    base = dict(
        id=m.id,
        nome=m.nome,
        aba=m.aba,
        card=m.saida.card,
        camada=m.camada,
        versao=m.versao,
        base_versao=m.base.versao if m.base else "",
        colunas=m.saida.colunas,
        vazio=m.vazio,
    )
    try:
        items = app.run(ctx)
    except Exception:  # um app com defeito não derruba a consulta: as regras de segurança seguem
        log.exception("smart app %s falhou", m.id)
        return AppResult(**base, erro="O app falhou ao rodar; o restante da consulta segue normal.")
    if items is None:
        return None
    kept, dropped = _clean(m, items, valid)
    if m.saida.card == "tabela":
        n = len(m.saida.colunas)
        dropped += [
            {"app": m.id, "texto": i.texto, "motivo": "células fora do schema"} for i in kept if len(i.celulas) != n
        ]
        kept = [i for i in kept if len(i.celulas) == n]
    return AppResult(**base, itens=kept, descartados=dropped)


def run_all(ctx: AppContext, valid: set[str] | None = None) -> list[AppResult]:
    results = []
    for app in all_apps():
        if not applies(app.manifest, ctx):
            continue
        res = run_one(app, ctx, valid)
        if res is not None:
            results.append(res)
    return results
