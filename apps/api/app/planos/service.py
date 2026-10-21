"""Planos e teste grátis (#35): catálogo, direito de uso por organização, contador e módulos liberados.

O contador conta `consulta.iniciada` da organização na auditoria, sem o sandbox. O direito de uso é criado na
primeira leitura (consultório começa no teste grátis, hospital no plano hospitalar) e só muda por ativação manual
de um administrador da plataforma.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.auth.deps import get_current_user
from app.db.models import AuditLog, Entitlement, Organization, Plan, User
from app.db.session import get_db
from app.db.tenancy import current_org, set_org

LIMITE_TESTE = 10  # consultas do teste grátis
AVISO_A_PARTIR_DE = 8  # a partir da 8ª consulta o contador vira aviso

MODULOS = {
    "passagem_plantao": "Passagem de plantão",
    "reconciliacao": "Reconciliação medicamentosa",
    "alta": "Planejamento de alta",
}
CATALOGO: dict[str, dict] = {
    "teste": {"nome": "Teste grátis", "limite": LIMITE_TESTE, "modulos": []},
    "profissional": {"nome": "Profissional", "limite": None, "modulos": []},
    "clinica": {"nome": "Clínica", "limite": None, "modulos": ["passagem_plantao", "reconciliacao"]},
    "hospital": {"nome": "Hospital", "limite": None, "modulos": list(MODULOS)},
}


def _plano_inicial(org: Organization) -> str:
    return "teste" if org.tipo == "consultorio" else "hospital"


def catalogo(db: Session) -> dict[str, Plan]:
    """Garante as linhas do catálogo (a migração já as cria; bancos novos de teste as criam aqui)."""
    rows = {p.codigo: p for p in db.scalars(select(Plan))}
    novos = False
    for codigo, d in CATALOGO.items():
        if codigo not in rows:
            limite = LIMITE_TESTE if codigo == "teste" else d["limite"]
            rows[codigo] = Plan(codigo=codigo, nome=d["nome"], limite_consultas=limite, modulos_json=list(d["modulos"]))
            db.add(rows[codigo])
            novos = True
    if novos:
        db.commit()  # catálogo e direito inicial são idempotentes: persistem mesmo numa leitura (GET)
    return rows


def direito(db: Session, org: Organization) -> Entitlement:
    ent = db.scalar(select(Entitlement).where(Entitlement.organization_id == org.id))
    if ent is None:
        plano = catalogo(db)[_plano_inicial(org)]
        ent = Entitlement(
            organization_id=org.id,
            plano=plano.codigo,
            limite_consultas=plano.limite_consultas,
            modulos_json=list(plano.modulos_json),
            inicio=clock.now(),
        )
        db.add(ent)
        db.commit()
    return ent


def _vencido(ent: Entitlement) -> bool:
    return ent.fim is not None and clock.now() > ent.fim


def contagem(db: Session) -> tuple[int, int]:
    """(reais, sandbox): `consulta.iniciada` da organização da sessão (a auditoria já vem filtrada por organização)."""
    rows = list(db.scalars(select(AuditLog.payload_json).where(AuditLog.action == "consulta.iniciada")))
    sandbox = sum(1 for p in rows if (p or {}).get("sandbox"))
    return len(rows) - sandbox, sandbox


def _org(db: Session) -> Organization | None:
    org_id = current_org(db)
    return db.get(Organization, org_id) if org_id else None


def modulos_liberados(db: Session, org: Organization) -> tuple[Entitlement, list[str]]:
    ent = direito(db, org)
    return ent, [] if _vencido(ent) else list(ent.modulos_json or [])


def tem_modulo(db: Session, modulo: str) -> bool:
    """O plano da organização da sessão libera o módulo? (sessão sem organização: serviços internos, sim)."""
    org = _org(db)
    return org is None or modulo in modulos_liberados(db, org)[1]


def uso(db: Session, org: Organization | None = None) -> dict:
    """Contador do plano da organização (o sandbox fica de fora). Vale para qualquer organização: a contagem
    lê a auditoria sob o filtro da organização pedida, mesmo que a sessão seja de outra (ou de nenhuma)."""
    org = org or _org(db)
    if org is None:  # sessão sem organização (serviços internos): sem limite
        return {
            "plano": "hospital",
            "nome": "Hospital",
            "limite": None,
            "usadas": 0,
            "restantes": None,
            "sandbox": 0,
            "aviso": None,
            "vigencia_ate": None,
            "vencido": False,
            "modulos": list(MODULOS),
        }
    ent, modulos = modulos_liberados(db, org)
    antes = current_org(db)
    set_org(db, org.id)
    try:
        usadas, sandbox = contagem(db)
    finally:
        set_org(db, antes)
    limite = ent.limite_consultas
    restantes = None if limite is None else max(limite - usadas, 0)
    aviso = None
    if limite is not None and usadas >= min(AVISO_A_PARTIR_DE, limite):
        aviso = f"Você usou {usadas} de {limite} consultas grátis." + (
            " O teste grátis acabou: ative um plano para continuar."
            if restantes == 0
            else f" Restam {restantes}; ative um plano para não interromper o atendimento."
        )
    return {
        "plano": ent.plano,
        "nome": catalogo(db)[ent.plano].nome,
        "limite": limite,
        "usadas": usadas,
        "restantes": restantes,
        "sandbox": sandbox,
        "aviso": aviso,
        "vigencia_ate": ent.fim.isoformat() if ent.fim else None,
        "vencido": _vencido(ent),
        "modulos": modulos,
    }


# Serializa "conferir o limite + gravar consulta.iniciada": sem isso, duas aberturas simultâneas na 10ª vaga passam.
# O lock cobre os workers da mesma instância; entre instâncias, a linha do direito de uso é bloqueada (FOR UPDATE).
INICIO_LOCK = threading.RLock()


def confere_inicio(db: Session) -> None:
    """Antes de abrir uma consulta real: plano vigente e dentro do limite (senão 402). Chamar com INICIO_LOCK."""
    org = _org(db)
    if org is not None:
        db.execute(select(Entitlement.id).where(Entitlement.organization_id == org.id).with_for_update())
    info = uso(db)
    if info["vencido"]:
        raise HTTPException(402, f"O plano {info['nome']} venceu. Peça a renovação para abrir novas consultas.")
    if info["limite"] is not None and info["usadas"] >= info["limite"]:
        raise HTTPException(
            402,
            f"O teste grátis acabou ({info['limite']} consultas). Use o sandbox ou ative um plano.",
        )


def exige_modulo(modulo: str):
    """Dependência de rota: 403 quando o plano da organização não inclui o módulo."""

    def checker(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
        org = db.get(Organization, user.organization_id) if user.organization_id else None
        if org is None:
            return
        ent, modulos = modulos_liberados(db, org)
        if modulo not in modulos:
            nome = catalogo(db)[ent.plano].nome
            motivo = "venceu" if _vencido(ent) else "não inclui este módulo"
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"O plano {nome} {motivo}: {MODULOS[modulo]} está disponível em plano com módulos hospitalares.",
            )

    return checker


def trocar(
    db: Session,
    org: Organization,
    user: User,
    plano: str,
    *,
    vigencia_ate: datetime | None = None,
    limite: int | None = None,
    modulos: list[str] | None = None,
) -> Entitlement:
    """Ativação manual: aplica o padrão do plano (limite e módulos), com ajustes opcionais."""
    cat = catalogo(db)
    if plano not in cat:
        raise HTTPException(422, f"Plano inválido: use {', '.join(CATALOGO)}")
    if modulos is not None and any(m not in MODULOS for m in modulos):
        raise HTTPException(422, f"Módulo inválido: use {', '.join(MODULOS)}")
    if vigencia_ate is not None and vigencia_ate.tzinfo is None:
        vigencia_ate = vigencia_ate.replace(tzinfo=UTC)  # sem fuso: assume UTC, como o resto da auditoria
    if vigencia_ate is not None and vigencia_ate <= clock.now():
        raise HTTPException(422, "A vigência precisa estar no futuro")
    ent = direito(db, org)
    ent.plano = plano
    ent.limite_consultas = limite if limite is not None else cat[plano].limite_consultas
    ent.modulos_json = list(modulos) if modulos is not None else list(cat[plano].modulos_json)
    ent.inicio = clock.now()
    ent.fim = vigencia_ate
    ent.updated_by = user.id
    ent.updated_at = clock.now()
    db.flush()
    return ent
