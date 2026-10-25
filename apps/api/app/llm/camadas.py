"""Pseudonimização em camadas antes de enviar texto a provedor EXTERNO de IA (#54).

Camadas (a contagem de cada uma vai para o audit_log, sem PII):
  1. `estruturado`: identificadores do FHIR já trocados por app/llm/pseudonymize.py ([PACIENTE], [CPF]...).
  2. `regex`: CPF, CNS, telefone, e-mail, CEP, data de nascimento e números de documento, mesmo sem estarem no FHIR.
  3. `regras_ner`: nomes e endereços ditos na fala, por regras (NÃO é modelo de linguagem): nomes conhecidos do
     prontuário, títulos e parentesco ("dona", "seu", "minha vizinha"), gatilhos de apresentação ("me chamo"),
     endereço ("rua ... número") e dicionário de prenomes e sobrenomes comuns (data/reference/nomes_br.csv).
  4. Reidentificação local: cada valor vira um token estável na chamada ([NOME_1], [ENDERECO_1]...); a resposta do
     provedor volta pelo mapa, que existe só em memória e morre com a chamada. Token que o provedor inventa ou altera
     vira "[dado omitido]": nunca vaza, nunca quebra o JSON.

O que NÃO cobre (limites reais, ver STATUS): nome comum escrito em minúsculas sem título (fala transcrita sem
capitalização), nome raro ou estrangeiro fora do dicionário e sem gatilho, apelidos, cidade e estabelecimento (ex.:
"mercado do Zé"), endereço sem tipo de logradouro ("moro lá perto da igreja"), número falado por extenso fora de
endereço (telefone ditado), referência indireta que identifica por contexto ("o prefeito da cidade"). A camada reduz
o risco; não anonimiza no sentido da LGPD (art. 12).
"""

from __future__ import annotations

import contextvars
import csv
import re
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import get_settings

OMITIDO = "[dado omitido]"
TIPOS = ("NOME", "ENDERECO", "CPF", "CNS", "TELEFONE", "EMAIL", "CEP", "NASC", "DOC")
TOKEN_RE = re.compile(r"\[(" + "|".join(TIPOS) + r")[_ \-]?(\d{1,4})\]", re.IGNORECASE)
ESTRUTURADO_RE = re.compile(r"\[(?:PACIENTE|FAMILIAR|CPF|TELEFONE|EMAIL|ENDERECO)\]")

# Nomes conhecidos do prontuário registrados pelo Pseudonymizer na requisição (reforço da camada de saída)
_conhecidos: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar("nomes_conhecidos", default=None)


def registrar_nomes(nomes: list[str]) -> None:
    atuais = _conhecidos.get() or frozenset()
    _conhecidos.set(atuais | frozenset(n for n in nomes if n and len(n.strip()) >= 3))


def nomes_registrados() -> frozenset[str]:
    return _conhecidos.get() or frozenset()


def norm(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower()


_lock = threading.Lock()


@lru_cache(maxsize=4)
def _dicionario(caminho: str) -> tuple[frozenset[str], frozenset[str]]:
    """(nomes não ambíguos, nomes ambíguos), já sem acento e em minúsculas."""
    firmes: set[str] = set()
    ambiguos: set[str] = set()
    p = Path(caminho)
    if not p.exists():
        return frozenset(), frozenset()
    with p.open(encoding="utf-8", newline="") as f:
        linhas = (ln for ln in f if ln.strip() and not ln.startswith("#"))
        for row in csv.DictReader(linhas):
            nome = norm(row["nome"])
            (ambiguos if row.get("ambiguo") == "1" else firmes).add(nome)
    return frozenset(firmes), frozenset(ambiguos)


def dicionario() -> tuple[frozenset[str], frozenset[str]]:
    return _dicionario(str(get_settings().data_dir / "reference" / "nomes_br.csv"))


# ---------------- camada 2: regex ----------------
_NUMEXT = (
    r"(?:um|uma|dois|duas|tr[eê]s|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|treze|quatorze|catorze|quinze|"
    r"dezesseis|dezessete|dezoito|dezenove|vinte|trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa|cem|cento|"
    r"duzentos|trezentos|quatrocentos|quinhentos|seiscentos|setecentos|oitocentos|novecentos|mil)"
)
NUMEXT_RE = rf"{_NUMEXT}(?:\s+e\s+{_NUMEXT})*"

# dígitos ditos por extenso ("meia" = seis): telefone, CPF e CNS ditados número a número
_DIGITO_EXT = r"(?:zero|um|uma|dois|duas|tr[eê]s|quatro|cinco|seis|meia|sete|oito|nove)"
_ITEM_DIGITADO = rf"(?:{_DIGITO_EXT}|\d)"
_SEP_DIGITADO = r"(?:\s*(?:,|-|\.|\bponto\b|\btra[cç]o\b|\bh[ií]fen\b)\s*|\s+(?:e\s+)?)"
_DIGITADO = rf"(?i:\b{_ITEM_DIGITADO}(?:{_SEP_DIGITADO}{_ITEM_DIGITADO}){{6,}}\b)"

_REGEX: list[tuple[str, re.Pattern, int]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), 0),
    # e-mail ofuscado: "joao arroba gmail ponto com", "maria(at)gmail.com", "maria [at] gmail [dot] com"
    (
        "EMAIL",
        re.compile(
            r"(?i:\b[\w.+-]+\s*(?:\(\s*at\s*\)|\[\s*at\s*\]|\{\s*at\s*\}|\s+arroba\s+|\s+at\s+)\s*[\w-]+"
            r"(?:\s*(?:\.|\(\s*dot\s*\)|\[\s*dot\s*\]|\s+ponto\s+|\s+dot\s+)\s*[\w-]+)+\b)"
        ),
        0,
    ),
    ("CNS", re.compile(r"(?<![\w/.-])\d{3}\s?\d{4}\s?\d{4}\s?\d{4}(?![\d-])"), 0),
    ("CPF", re.compile(r"(?<![\w/.-])\d{3}[.\s]?\d{3}[.\s]?\d{3}[-.\s]?\d{2}(?![\d-])"), 0),
    ("DOC", re.compile(_DIGITADO), 0),
    ("CEP", re.compile(r"(?i:\bcep\b)[\s:nº.°-]*\d{5}-?\d{3}\b|(?<![\d-])\d{5}-\d{3}(?![\d-])"), 0),
    (
        "TELEFONE",
        # só formatos de telefone: um "5000 7500" de dose ou um id "1234-5678" de referência FHIR não pode virar token
        re.compile(
            r"(?<![\w/.-])(?:\+?55[\s.-]?)\(?\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4}(?![\d-])"  # com +55
            r"|(?<![\w/.-])\(\d{2}\)\s?9?\d{4}[\s.-]?\d{4}(?![\d-])"  # (DDD) xxxx-xxxx
            r"|(?<![\w/.-])\d{2}[\s.-]?9[\s.-]?\d{4}[\s.-]?\d{4}(?![\d-])"  # DDD + celular (também "11 9 8765 4321")
            r"|(?<![\w/.-])\d{2}[\s.-][2-5]\d{3}[\s.-]\d{4}(?![\d-])"  # DDD + fixo com separador
            r"|(?<![\w/.-])9?\d{4}-\d{4}(?![\d-])"  # xxxx-xxxx
            r"|(?<![\w/.-])9\d{4}[\s.]\d{4}(?![\d-])"  # celular sem DDD
            r"|(?<![\w/.-])\(?\d{2}\)?\s?9\d{8}(?!\d)"
        ),
        0,
    ),
    (
        "NASC",
        re.compile(
            r"(?i:\b(?:nasc(?:eu|i|ido|ida|imento)?|d\.?\s?n\.?)\b)[\s:,]*(?i:(?:em|de|dia|é|foi)\s+)?"
            r"(\d{1,2}[/.\-\s]\d{1,2}[/.\-\s]\d{2,4}|\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2}|"
            r"\d{1,2}\s+de\s+[a-zç]+\s+de\s+\d{4}|\d{1,2}\s+de\s+[a-zç]+\s+\d{4})",
            re.IGNORECASE,
        ),
        1,
    ),
    (
        "DOC",
        re.compile(
            r"(?i:\b(?:rg|cnh|cnpj|identidade|passaporte|ctps|t[ií]tulo de eleitor|carteira de trabalho|"
            r"registro geral)\b)[\s:nº.°,-]*(?i:(?:é|numero|número)\s+)?([A-Z]{0,3}\d[\w.\-/]{4,})"
        ),
        1,
    ),
]
_CPF_PURO = re.compile(r"^\d{11}$")
_PURO11 = re.compile(r"(?<![\w/.-])\d{11}(?![\w-])")


# ---------------- camada 3: regras ----------------
_PARTICULAS = r"(?:d[aeo]s?)"
_PALAVRA = r"[A-Za-zÀ-ÿ'’]+"
_CAP = r"[A-ZÀ-Ý][A-Za-zÀ-ÿ'’]+"
_RESTO_NOME = rf"(?:\s+{_PALAVRA}){{0,4}}"

# gatilhos que apontam o nome logo depois
_FORTES = (
    r"me chamo|meu nome (?:é|e)|nome dele (?:é|e)|nome dela (?:é|e)|se chama|chama-se|chamad[oa]|"
    r"conhecid[oa] como|apelid[oa] de"
)
_FRACOS = (
    r"dona|dna|seu|sr\.?|sra\.?|srta\.?|senhor|senhora|doutor|doutora|dr\.?|dra\.?|tio|tia|"
    r"vizinh[oa]|prim[oa]|sogr[oa]|cunhad[oa]|gen?ro|nora|marido|esposa|esposo|companheir[oa]|namorad[oa]|"
    r"filh[oa]|irm[ãa]o?|m[ãa]e|pai|av[óo]|av[ôo]|amig[oa]|colega|patr[ãa]o|patroa|chefe|"
    r"enfermeir[oa]|m[ée]dic[oa]|professor[a]?|padre|pastor[a]?|madrinha|padrinho|neto|neta|sobrinh[oa]|"
    r"genro|cuidador[a]?|diarista|empregada"
)
_STOP_FORTE = {
    "que", "de", "do", "da", "um", "uma", "o", "a", "e", "muito", "bem", "mal", "assim", "isso", "essa", "esse",
    "por", "para", "pra", "com", "sem", "nao", "não", "sim", "ja", "já", "tambem", "também", "mais", "menos",
}  # fmt: skip
# o gatilho captura só a primeira palavra; o fim do nome é decidido por _fim_do_nome sobre o texto original, para que
# um segundo gatilho logo adiante ("dona Rosa e dona Rosa") não seja engolido pelo primeiro
_FORTE_RE = re.compile(rf"(?i:\b(?:{_FORTES}))\s+(?:(?i:d[aeo]s?)\s+)?({_PALAVRA})")
_FRACO_RE = re.compile(rf"(?i:\b(?:{_FRACOS}))\s+(?:(?i:d[aeo])\s+)?({_PALAVRA})")
_NOME_RE = re.compile(rf"{_PALAVRA}{_RESTO_NOME}")

_VIA = r"(?i:rua|r\.|avenida|av\.?|alameda|travessa|tv\.|pra[çc]a|rodovia|estrada|largo|beco|viela|quadra|bairro)"
_NUMERO = (
    rf"(?:\s*,?\s*(?i:n[ºo°.]*\s*|n[úu]mero\s*)(?:\d{{1,5}}|(?i:{NUMEXT_RE}))\b"
    rf"|\s*,\s*\d{{1,5}}\b|\s+\d{{1,5}}\b)?"
)
_COMPL = r"(?:\s*,?\s*(?i:apto?\.?|apartamento|bloco|casa|fundos|ap\.)\s*\w+)?"
_ENDERECO_RE = re.compile(
    rf"\b{_VIA}\s+(?:{_PARTICULAS}\s+|(?i:{_PARTICULAS})\s+)?{_PALAVRA}"
    rf"(?:\s+{_PARTICULAS}\s+{_PALAVRA}|\s+{_CAP}){{0,3}}{_NUMERO}{_COMPL}"
)
_PALAVRA_RE = re.compile(_PALAVRA)
_PARTICULA_SET = {"da", "das", "de", "do", "dos", "e"}


_TOKEN_ANY = re.compile(r"\[[A-Za-z]{3,12}(?:[_ \-]?\d{1,4})?\]")


def _spans_tokens(texto: str) -> list[tuple[int, int]]:
    return [m.span() for m in _TOKEN_ANY.finditer(texto)]


def _em_token(spans: list[tuple[int, int]], ini: int, fim: int) -> bool:
    """A palavra está DENTRO de um token de verdade ([PACIENTE], [NOME_1])? "[Maria Silva]" não é token."""
    return any(a <= ini and fim <= b for a, b in spans)


class Escudo:
    """Uma instância por chamada ao provedor. O mapa vive só aqui."""

    def __init__(self, nomes_conhecidos: list[str] | None = None, ativo: bool = True):
        self.ativo = ativo
        self._mapa: dict[str, str] = {}
        self._rev: dict[tuple[str, str], str] = {}
        self._n: dict[str, int] = {}
        self.contagem = {"estruturado": 0, "regex": 0, "regras_ner": 0}
        extras = list(nomes_conhecidos or []) + list(nomes_registrados())
        self._vistos: set[str] = set()
        self._conhecidos = {norm(p) for n in extras for p in n.split() if len(p) >= 3 and norm(p) not in _PARTICULA_SET}

    # ---- tokens ----
    def _token(self, tipo: str, original: str, camada: str) -> str:
        chave = (tipo, norm(original).strip())
        tok = self._rev.get(chave)
        if tok is None:
            self._n[tipo] = self._n.get(tipo, 0) + 1
            tok = f"[{tipo}_{self._n[tipo]}]"
            self._rev[chave] = tok
            self._mapa[tok] = original
        self.contagem[camada] += 1
        return tok

    # ---- saída (pseudonimiza) ----
    def proteger(self, texto: str, contar_estruturado: bool = True) -> str:
        if contar_estruturado:  # o prompt de sistema cita [PACIENTE] como instrução: não conta como dado
            self.contagem["estruturado"] += len(ESTRUTURADO_RE.findall(texto))
        if not self.ativo or not texto:
            return texto
        # token forjado na fala ("[NOME_1]") não pode ser reidentificado como outra pessoa: vira "(NOME_1)"
        texto = TOKEN_RE.sub(lambda m: "(" + m.group(0)[1:-1] + ")", texto)
        texto = self._regex(texto)
        texto = self._enderecos(texto)
        texto = self._gatilhos(texto)
        return self._dicionario(texto)

    def proteger_dados(self, dado: Any) -> Any:
        if isinstance(dado, str):
            return self.proteger(dado)
        if isinstance(dado, list):
            return [self.proteger_dados(v) for v in dado]
        if isinstance(dado, dict):
            return {k: self.proteger_dados(v) for k, v in dado.items()}
        return dado

    def _regex(self, texto: str) -> str:
        for tipo, padrao, grupo in _REGEX:

            def troca(m: re.Match, tipo: str = tipo, grupo: int = grupo) -> str:
                if grupo:
                    ini, fim = m.span(grupo)
                    base = m.start()
                    return m.group(0)[: ini - base] + self._token(tipo, m.group(grupo), "regex")
                return self._token(tipo, m.group(0), "regex")

            texto = padrao.sub(troca, texto)
        # 11 dígitos soltos que sobraram: celular sem máscara ou CPF sem máscara
        return _PURO11.sub(lambda m: self._token("CPF", m.group(0), "regex"), texto)

    def _enderecos(self, texto: str) -> str:
        return _ENDERECO_RE.sub(lambda m: self._token("ENDERECO", m.group(0), "regras_ner"), texto)

    def _valida_nome(self, palavra: str, forte: bool) -> bool:
        n = norm(palavra)
        if len(n) < 2 or n in _STOP_FORTE or n in _PARTICULA_SET:
            return False
        if forte:
            return True
        firmes, ambiguos = dicionario()
        return palavra[:1].isupper() or n in firmes or n in ambiguos or n in self._conhecidos

    def _gatilhos(self, texto: str) -> str:
        texto = self._varrer(texto, _FORTE_RE, True)
        return self._varrer(texto, _FRACO_RE, False)

    def _varrer(self, texto: str, rx: re.Pattern, forte: bool) -> str:
        spans = _spans_tokens(texto)
        saida: list[str] = []
        pos = i = 0
        while (m := rx.search(texto, i)) is not None:
            ini, fim_primeira = m.span(1)
            i = m.end()
            primeira = m.group(1)
            if _em_token(spans, ini, fim_primeira) or not self._valida_nome(primeira, forte):
                continue
            # "seu médico", "dona de casa": palavra comum minúscula fora do dicionário não é nome
            if not forte and primeira[:1].islower() and norm(primeira) not in self._firmes_ou_amb():
                continue
            nome_m = _NOME_RE.match(texto, ini)
            nome = nome_m.group(0) if nome_m else primeira
            fim = ini + self._fim_do_nome(nome)
            saida += [texto[pos:ini], self._token("NOME", texto[ini:fim], "regras_ner")]
            pos = i = fim
            self._lembrar(texto[ini:fim])
        saida.append(texto[pos:])
        return "".join(saida)

    def _lembrar(self, nome: str) -> None:
        """Nome dito com título vale para as outras menções (sem título) da mesma chamada."""
        for p in _PALAVRA_RE.findall(nome):
            n = norm(p)
            if len(n) >= 3 and n not in _PARTICULA_SET:
                self._vistos.add(n)

    def _fim_do_nome(self, nome: str) -> int:
        """Onde o nome acaba: a primeira palavra vale; as seguintes só se forem capitalizadas ou do dicionário."""
        firmes, amb = dicionario()
        palavras = list(_PALAVRA_RE.finditer(nome))
        fim = palavras[0].end()
        i = 1
        while i < len(palavras):
            w = palavras[i].group(0)
            n = norm(w)
            if n in _PARTICULA_SET and i + 1 < len(palavras):
                prox = palavras[i + 1].group(0)
                if prox[:1].isupper() or norm(prox) in firmes | amb:
                    fim = palavras[i + 1].end()
                    i += 2
                    continue
                break
            if w[:1].isupper() or n in firmes or n in amb or n in self._conhecidos:
                fim = palavras[i].end()
                i += 1
                continue
            break
        return fim

    def _firmes_ou_amb(self) -> frozenset[str]:
        firmes, amb = dicionario()
        return firmes | amb | frozenset(self._conhecidos)

    def _dicionario(self, texto: str) -> str:
        firmes, ambiguos = dicionario()
        spans = _spans_tokens(texto)
        palavras = [m for m in _PALAVRA_RE.finditer(texto) if not _em_token(spans, m.start(), m.end())]

        def qualifica(m: re.Match) -> int:
            """0 não é nome; 1 nome firme; 2 só vale ao lado de outro nome (ambíguo)."""
            w = m.group(0)
            n = norm(w)
            if len(n) < 3 or n in _PARTICULA_SET:
                return 0
            if n in self._conhecidos:
                return 1  # conhecido do prontuário vale em qualquer caixa
            if not (w[:1].isupper()):
                return 0
            if n in self._vistos:
                return 1  # já apareceu como nome nesta chamada ("dona Rosa" ... "Rosa disse")
            if n in firmes:
                return 1
            if n in ambiguos:
                return 2
            return 0

        marcas = [qualifica(m) for m in palavras]
        # grupos: palavras vizinhas separadas por um espaço (ou partícula entre duas palavras de nome)
        usar = [False] * len(palavras)
        for i, v in enumerate(marcas):
            if v == 1:
                usar[i] = True
        mudou = True
        while mudou:  # ambíguo vizinho de nome firme (ou de outro já marcado) também entra
            mudou = False
            for i, v in enumerate(marcas):
                if v == 2 and not usar[i]:
                    viz = [
                        j
                        for j in (i - 1, i + 1)
                        if 0 <= j < len(palavras) and usar[j] and self._colado(texto, palavras, i, j)
                    ]
                    if viz:
                        usar[i] = mudou = True
        # sobrenome fora do dicionário colado a um nome firme ("Ana D'Ávila"): até 2 palavras capitalizadas depois
        for i, v in enumerate(marcas):
            if v != 1 or not usar[i]:
                continue
            for j in (i + 1, i + 2):
                if j >= len(palavras) or marcas[j] != 0 or not palavras[j].group(0)[:1].isupper():
                    break
                if len(norm(palavras[j].group(0))) < 3 or not self._colado(texto, palavras, j - 1, j):
                    break
                usar[j] = True
        # junta runs contíguos
        runs: list[tuple[int, int]] = []
        i = 0
        while i < len(palavras):
            if not usar[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(palavras) and usar[j + 1] and self._colado(texto, palavras, j, j + 1):
                j += 1
            runs.append((palavras[i].start(), palavras[j].end()))
            i = j + 1
        for ini, fim in reversed(runs):
            texto = texto[:ini] + self._token("NOME", texto[ini:fim], "regras_ner") + texto[fim:]
        return texto

    @staticmethod
    def _colado(texto: str, palavras: list[re.Match], i: int, j: int) -> bool:
        a, b = (i, j) if i < j else (j, i)
        meio = texto[palavras[a].end() : palavras[b].start()]
        return meio.strip() == "" and "\n" not in meio and len(meio) <= 2

    # ---- volta (reidentifica) ----
    def reidentificar_texto(self, texto: str, escapar_json: bool = False) -> str:
        def sub(m: re.Match) -> str:
            tok = f"[{m.group(1).upper()}_{int(m.group(2))}]"
            original = self._mapa.get(tok)
            if original is None:
                return OMITIDO
            if escapar_json:
                import json

                return json.dumps(original, ensure_ascii=False)[1:-1]
            return original

        return TOKEN_RE.sub(sub, texto)

    def reidentificar_json(self, texto_json: str) -> str:
        """Reidentifica dentro de um JSON em texto, escapando o valor (aspas e quebras não quebram o documento)."""
        return self.reidentificar_texto(texto_json, escapar_json=True)

    @property
    def total(self) -> int:
        return sum(self.contagem.values())
