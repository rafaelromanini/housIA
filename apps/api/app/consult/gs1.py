"""Leitura da etiqueta GS1 (DataMatrix ou GS1-128) do implante: GTIN, lote, validade e série.

A câmera do tablet entrega o texto lido; o backend o interpreta. Aceita as duas formas usuais:
- legível, com os identificadores de aplicação (AI) entre parênteses: `(01)07891234567895(17)271231(10)LOTE123(21)SN1`;
- bruta, como o leitor devolve: identificadores sem parênteses, campos de tamanho variável terminados pelo
  separador FNC1 (caractere GS, \\x1d) e, às vezes, o identificador de simbologia (`]d2`, `]C1`, `]Q3`) na frente.

Identificadores tratados: 01 (GTIN), 02 (GTIN do conteúdo), 10 (lote), 11 (fabricação), 17 (validade), 21 (série).
Os demais identificadores do padrão GS1 são lidos (para não quebrar a leitura) e devolvidos em `outros`.
O dígito verificador do GTIN (módulo 10) é conferido e informado, sem recusar a etiqueta: quem decide é o cirurgião.
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date

GS = "\x1d"
SYMBOLOGY = re.compile(r"^\](?:d2|C1|Q3|e0|E0)")
# AI de tamanho fixo (dígitos) — GS1 General Specifications, tabela 3.2 (os usados em produtos para saúde e logística)
FIXED: dict[str, int] = {
    "00": 18,
    "01": 14,
    "02": 14,
    "11": 6,
    "12": 6,
    "13": 6,
    "15": 6,
    "16": 6,
    "17": 6,
    "20": 2,
}
MEASURE = re.compile(r"3[1-6]\d\d")  # 310n–369n: medidas (peso, comprimento…), 6 dígitos
# AI de tamanho variável: (tamanho máximo). 3 e 4 dígitos escolhidos pelos primeiros dígitos.
VARIABLE: dict[str, int] = {
    "10": 20,
    "21": 20,
    "22": 20,
    "30": 8,
    "37": 8,
    "240": 30,
    "241": 30,
    "242": 6,
    "250": 30,
    "251": 30,
    "400": 30,
    "401": 30,
    "7003": 10,
    "8008": 12,
    "8010": 30,
    "8011": 12,
    "90": 30,
    "91": 90,
    "92": 90,
    "93": 90,
    "94": 90,
    "95": 90,
    "96": 90,
    "97": 90,
    "98": 90,
    "99": 90,
}
LABEL = {
    "01": "gtin",
    "02": "gtin_conteudo",
    "10": "lote",
    "11": "fabricacao",
    "17": "validade",
    "21": "serie",
}
HUMAN = re.compile(r"\((\d{2,4})\)([^()]*)")


class Gs1Error(ValueError):
    pass


@dataclass
class Etiqueta:
    """Etiqueta interpretada. `validade` e `fabricacao` em ISO (AAAA-MM-DD); dia 00 vira o último dia do mês."""

    bruto: str
    gtin: str | None = None
    lote: str | None = None
    validade: str | None = None
    fabricacao: str | None = None
    serie: str | None = None
    gtin_valido: bool | None = None
    avisos: list[str] = field(default_factory=list)
    outros: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "gtin": self.gtin,
            "lote": self.lote,
            "validade": self.validade,
            "fabricacao": self.fabricacao,
            "serie": self.serie,
            "gtin_valido": self.gtin_valido,
            "avisos": self.avisos,
        }


def check_digit(body: str) -> int:
    """Dígito verificador GS1 (módulo 10) do corpo do GTIN (sem o último dígito)."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10


def gtin_ok(gtin: str) -> bool:
    return gtin.isdigit() and len(gtin) in (8, 12, 13, 14) and check_digit(gtin[:-1]) == int(gtin[-1])


def parse_date(yymmdd: str, today: date | None = None) -> date:
    """AAMMDD do GS1. Dia 00 = último dia do mês; o século segue a regra GS1 (dentro de 50 anos de hoje)."""
    if not re.fullmatch(r"\d{6}", yymmdd):
        raise Gs1Error(f"Data GS1 inválida: {yymmdd!r}")
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    if not 1 <= mm <= 12:
        raise Gs1Error(f"Mês inválido na data GS1: {yymmdd!r}")
    today = today or date.today()
    century = today.year // 100 * 100
    year = century + yy
    if year - today.year > 50:
        year -= 100
    elif today.year - year >= 50:
        year += 100
    last = monthrange(year, mm)[1]
    if dd == 0:
        dd = last
    if dd > last:
        raise Gs1Error(f"Dia inválido na data GS1: {yymmdd!r}")
    return date(year, mm, dd)


def _pairs_human(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2).strip()) for m in HUMAN.finditer(text)]


def _pairs_raw(text: str) -> list[tuple[str, str]]:
    """Lê `0107891234567895 17271231 10LOTE123<GS>21SN1`: AI fixo pelo tamanho, variável até o GS ou o fim."""
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(text):
        if text[i] == GS:
            i += 1
            continue
        ai = next((text[i : i + n] for n in (2, 3, 4) if text[i : i + n] in FIXED or text[i : i + n] in VARIABLE), None)
        if ai is None and MEASURE.match(text, i):
            ai = text[i : i + 4]
        if ai is None:
            raise Gs1Error(f"Identificador GS1 desconhecido na posição {i}: {text[i : i + 4]!r}")
        i += len(ai)
        if ai in FIXED or MEASURE.fullmatch(ai):
            size = FIXED.get(ai, 6)
            value = text[i : i + size]
            if len(value) < size:
                raise Gs1Error(f"Campo ({ai}) incompleto na etiqueta")
            i += size
        else:
            end = text.find(GS, i)
            end = len(text) if end < 0 else end
            value = text[i:end][: VARIABLE[ai]]
            i += len(value)
        pairs.append((ai, value))
    return pairs


def parse(text: str, today: date | None = None) -> Etiqueta:
    """Interpreta a leitura da etiqueta. Levanta Gs1Error se não houver nenhum identificador reconhecível."""
    raw = (text or "").strip()
    if not raw:
        raise Gs1Error("Leitura da etiqueta vazia")
    body = SYMBOLOGY.sub("", raw).replace("␝", GS).replace("<GS>", GS).replace("{GS}", GS)
    pairs = _pairs_human(body) if "(" in body else _pairs_raw(body)
    if not pairs:
        raise Gs1Error("Nenhum identificador GS1 encontrado na etiqueta")
    et = Etiqueta(bruto=raw)
    for ai, value in pairs:
        key = LABEL.get(ai)
        if key is None:
            et.outros[ai] = value
        elif key in ("validade", "fabricacao"):
            try:
                setattr(et, key, parse_date(value, today).isoformat())
            except Gs1Error as exc:
                et.avisos.append(str(exc))
        elif key == "gtin_conteudo":
            et.outros[ai] = value
        else:
            setattr(et, key, value)
    if et.gtin is None and "02" in et.outros:
        et.gtin = et.outros["02"]
    if et.gtin is not None:
        et.gtin_valido = gtin_ok(et.gtin)
        if not et.gtin_valido:
            et.avisos.append(f"Dígito verificador do GTIN {et.gtin} não confere: conferir a etiqueta.")
    if et.gtin is None and et.lote is None and et.serie is None:
        raise Gs1Error("A etiqueta não traz GTIN, lote nem série")
    return et
