"""Memória de estilo da nota (#46): o que o profissional costuma mudar na forma, aprendido dos diffs.

Cada nota atestada que difere do rascunho da IA vira uma contribuição de três números: a razão de tamanho (palavras da
versão final sobre o rascunho), a variação de siglas por 100 palavras e se o profissional reordenou frases. Não se
guarda **nenhum texto** de paciente nem da nota, só esses números, por profissional (e, por consequência, por
organização: a linha é da organização do usuário e o isolamento é o do resto do sistema). O perfil só vale depois de
`MIN_NOTAS` notas editadas e vai ao prompt da reescrita como preferência de forma, nunca como fato clínico.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.consult import notas
from app.consult.edicao import edit_percent
from app.db.models import EstiloNota, User

MIN_NOTAS = 3  # notas editadas para o perfil passar a valer
MAX_NOTAS = 30  # contribuições guardadas (as mais antigas saem)
_SIGLA = re.compile(r"\b[A-ZÁÉÍÓÚ]{2,6}\b")
_PALAVRA = re.compile(r"\w+")


def _texto(note: dict | None, key: str) -> str:
    sec = (note or {}).get(key) or {}
    return sec.get("texto", "") if isinstance(sec, dict) else ""


def _por_secao(note: dict | None) -> dict[str, str]:
    out = {}
    for key in notas.SECTION_KEYS:
        texto = _texto(note, key)
        if texto.strip() and not notas.is_placeholder(texto):
            out[key] = texto
    return out


def _densidade_siglas(textos: list[str]) -> float:
    palavras = sum(len(_PALAVRA.findall(t)) for t in textos)
    return 100 * sum(len(_SIGLA.findall(t)) for t in textos) / palavras if palavras else 0.0


def _reordenou(a: dict[str, str], b: dict[str, str]) -> bool:
    for key in a.keys() & b.keys():
        fa = [notas._key(f) for f in notas.split_sentences(a[key])]
        fb = [notas._key(f) for f in notas.split_sentences(b[key])]
        comum = [f for f in fa if f in fb]
        if len(comum) >= 2 and comum != [f for f in fb if f in comum]:
            return True
    return False


def contribuicao(rascunho: dict | None, final: dict | None) -> dict | None:
    """Os três números desta nota, ou None se ela não foi editada (nada a aprender) ou não tem texto."""
    if not rascunho or not final or edit_percent(rascunho, final)[1] == 0:
        return None
    a, b = _por_secao(rascunho), _por_secao(final)
    mexidas = [k for k in a.keys() | b.keys() if notas._key(a.get(k, "")) != notas._key(b.get(k, ""))]
    palavras_a = sum(len(_PALAVRA.findall(a.get(k, ""))) for k in mexidas)  # o tamanho se mede onde ele mexeu
    palavras_b = sum(len(_PALAVRA.findall(b.get(k, ""))) for k in mexidas)
    if not palavras_a:
        return None
    return {
        "tamanho": round(palavras_b / palavras_a, 3),
        "siglas": round(_densidade_siglas(list(b.values())) - _densidade_siglas(list(a.values())), 2),
        "reordena": 1 if _reordenou(a, b) else 0,
    }


def perfil(dados: dict | None) -> dict:
    """Perfil derivado das contribuições: {"ativo", "notas", "tamanho", "siglas", "ordem"} (só rótulos de forma)."""
    dados = dados or {}
    contribs = list((dados.get("notas") or {}).values())
    base = {"notas": len(contribs), "minimo": MIN_NOTAS, "desligado": bool(dados.get("desligado"))}
    if len(contribs) < MIN_NOTAS or base["desligado"]:
        return base | {"ativo": False, "tamanho": "igual", "siglas": "igual", "ordem": "mantem"}
    n = len(contribs)
    tam = sum(c["tamanho"] for c in contribs) / n
    sig = sum(c["siglas"] for c in contribs) / n
    ordem = sum(c["reordena"] for c in contribs) / n
    return base | {
        "ativo": True,
        "tamanho": "mais_curto" if tam < 0.9 else "mais_longo" if tam > 1.1 else "igual",
        "siglas": "mais_siglas" if sig >= 1 else "menos_siglas" if sig <= -1 else "igual",
        "ordem": "reordena" if ordem >= 0.5 else "mantem",
    }


def dicas(p: dict) -> list[str]:
    """Preferências de forma em linguagem natural para o prompt (vazio se o perfil não vale)."""
    if not p.get("ativo"):
        return []
    out = []
    if p["tamanho"] == "mais_curto":
        out.append("Prefere seções mais curtas que o rascunho.")
    elif p["tamanho"] == "mais_longo":
        out.append("Prefere seções mais detalhadas que o rascunho.")
    if p["siglas"] == "mais_siglas":
        out.append("Usa siglas médicas com frequência.")
    elif p["siglas"] == "menos_siglas":
        out.append("Evita siglas: escreve por extenso.")
    if p["ordem"] == "reordena":
        out.append("Costuma reordenar as frases da seção.")
    return out


# ---------- persistência ----------
def _row(db: Session, user: User) -> EstiloNota | None:
    return db.scalar(select(EstiloNota).where(EstiloNota.user_id == user.id))


def obter(db: Session, user: User) -> dict:
    row = _row(db, user)
    return perfil(row.dados_json if row else None)


def registrar(db: Session, user: User, consulta_id: int, rascunho: dict | None, final: dict | None) -> dict:
    """Atualiza a contribuição desta consulta (salvar de novo substitui, não soma). Devolve o perfil resultante."""
    row = _row(db, user)
    dados = dict(row.dados_json) if row else {}
    contribs = dict(dados.get("notas") or {})
    c = contribuicao(rascunho, final)
    if c is None:
        contribs.pop(str(consulta_id), None)
    else:
        contribs.pop(str(consulta_id), None)
        contribs[str(consulta_id)] = c  # reinserir põe a mais recente no fim
    dados["notas"] = dict(list(contribs.items())[-MAX_NOTAS:])
    if row is None:
        db.add(EstiloNota(user_id=user.id, dados_json=dados, updated_at=clock.now()))
    else:
        row.dados_json, row.updated_at = dados, clock.now()
    return perfil(dados)


def esquecer(db: Session, user: User) -> None:
    """Zera o perfil do profissional."""
    row = _row(db, user)
    if row is not None:
        db.delete(row)


def esquecer_consultas(db: Session, consulta_ids: list[int]) -> None:
    """Exclusão a pedido do titular: tira das contribuições (de toda a organização) as consultas apagadas."""
    ids = {str(i) for i in consulta_ids}
    for row in db.scalars(select(EstiloNota)):
        dados = dict(row.dados_json)
        mantidas = {k: v for k, v in (dados.get("notas") or {}).items() if k not in ids}
        if len(mantidas) != len(dados.get("notas") or {}):
            row.dados_json = dados | {"notas": mantidas}


def desligar(db: Session, user: User, desligado: bool) -> dict:
    row = _row(db, user)
    if row is None:
        row = EstiloNota(user_id=user.id, dados_json={}, updated_at=clock.now())
        db.add(row)
    row.dados_json = dict(row.dados_json) | {"desligado": desligado}
    return perfil(row.dados_json)
