"""Pseudonimização antes de qualquer envio ao LLM.

Nome, CPF, telefone e endereço do paciente (e nomes/telefones de familiares) são trocados por tokens.
O LLM nunca recebe identificadores diretos — mesmo com dados sintéticos o fluxo já nasce assim.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
PHONE_RE = re.compile(r"\(\d{2}\)\s?9?\d{4}-?\d{4}|\b9\d{4}-\d{4}\b")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
STOPWORDS = {"da", "das", "de", "do", "dos", "e"}


def _strip_accents(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _variants(token: str) -> set[str]:
    return {token, _strip_accents(token)}


@dataclass
class Pseudonymizer:
    patient_names: list[str] = field(default_factory=list)
    family_names: list[str] = field(default_factory=list)
    cpf: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    replacements: int = 0

    def __post_init__(self) -> None:
        rules: list[tuple[re.Pattern, str]] = []
        for addr in sorted(self.addresses, key=len, reverse=True):
            rules.append((re.compile(re.escape(addr), re.IGNORECASE), "[ENDERECO]"))
        for value in self.cpf:
            rules.append((re.compile(re.escape(value)), "[CPF]"))
        for value in self.phones:
            rules.append((re.compile(re.escape(value)), "[TELEFONE]"))
        rules += self._name_rules(self.patient_names, "[PACIENTE]")
        rules += self._name_rules(self.family_names, "[FAMILIAR]")
        rules += [(CPF_RE, "[CPF]"), (EMAIL_RE, "[EMAIL]"), (PHONE_RE, "[TELEFONE]")]
        self._rules = rules

    @staticmethod
    def _name_rules(names: list[str], token: str) -> list[tuple[re.Pattern, str]]:
        # nomes completos primeiro, depois partes (prenome, sobrenomes) com 3+ letras
        full = sorted({n for n in names if " " in n}, key=len, reverse=True)
        parts = sorted(
            {p for n in names for p in n.split() if len(p) >= 3 and p.lower() not in STOPWORDS},
            key=len,
            reverse=True,
        )
        rules = []
        for item in [*full, *parts]:
            alts = "|".join(re.escape(v) for v in sorted(_variants(item), key=len, reverse=True))
            rules.append((re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE), token))
        return rules

    def text(self, value: str) -> str:
        for pattern, token in self._rules:
            value, n = pattern.subn(token, value)
            self.replacements += n
        return value

    def apply(self, data: Any) -> Any:
        """Aplica recursivamente em strings de dicts/listas (chaves preservadas)."""
        if isinstance(data, str):
            return self.text(data)
        if isinstance(data, list):
            return [self.apply(v) for v in data]
        if isinstance(data, dict):
            return {k: self.apply(v) for k, v in data.items()}
        return data
