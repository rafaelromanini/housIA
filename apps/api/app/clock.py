"""Relógio clínico do HousIA. Tudo que depende de "agora" passa por aqui (testes congelam o horário)."""

from datetime import UTC, datetime

_frozen: datetime | None = None


def now() -> datetime:
    return _frozen or datetime.now(UTC)


def freeze(moment: datetime | None) -> None:
    global _frozen
    _frozen = moment
