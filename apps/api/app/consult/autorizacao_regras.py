"""Base de regras da autorização prévia (#56): Rol da ANS, DUT e exigências de operadora, em dados versionados.

O que vem no repositório (`data/reference/rol_dut_exemplo.csv`, `operadoras_exemplo.csv`) é um conjunto SINTÉTICO e
ILUSTRATIVO, marcado `origem=exemplo`: não é o texto oficial de RN da ANS nem de operadora. A instituição
importa a tabela oficial pelo importador (`validar_regras`, `validar_operadoras`, `scripts/importar_rol_dut.py`), que
confere cada linha e devolve um relatório de erros; uma linha importada entra com `conferencia=pendente` até uma pessoa
conferir o texto contra a norma vigente. Linha inválida nunca entra na base ativa.

Gramática de uma regra de critério (`criterios`, itens separados por `|`, partes por `::`):
    id::texto::regra[::pergunta[::aplica]]
    regra:   cid:M17,M23   idade>=18 (>, <, <=)   fala:termo~termo   lab:chave>valor (ou <)
             exame:texto:termo~termo[@dias]
             exame:lab:chave[@dias]   manual (nunca é atendida pelo sistema: só a documentação do profissional)
    aplica:  uma regra (sem fala/manual) que, se não for atendida, torna o critério "não aplicável"
Documentos (`documentos`): `rótulo::justificativa` (o pacote gerado), `rótulo::exame:...` (evidência do prontuário) ou
`rótulo::anexo` (a pessoa anexa).
"""

from __future__ import annotations

import csv
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.consult.text import norm
from app.fhir_client import codes

log = logging.getLogger(__name__)

MAX_BYTES = 1_000_000
MAX_LINHAS = 3000
MAX_ERROS = 200
TIPOS = ("exame", "procedimento", "medicamento", "encaminhamento")
ORIGENS = ("exemplo", "importado")
CONFERENCIAS = ("pendente", "conferido")
COLUNAS_REGRAS = (
    "id,procedimento,tipo,termos,cids,idade_min,idade_max,criterios,documentos,fonte,versao,vigencia_inicio,"
    "vigencia_fim,origem,conferencia,observacao"
).split(",")
COLUNAS_OPERADORAS = (
    "operadora_id,nome,aliases,procedimento,documentos,justificativa_obrigatoria,prazo_dias,fonte,versao,"
    "vigencia_inicio,vigencia_fim,origem,conferencia"
).split(",")
ID_RE = re.compile(r"^[a-z0-9_]{2,60}$")
CID_RE = re.compile(r"^[A-Z]\d{0,2}(\.\d{1,2})?$")
IDADE_RE = re.compile(r"^idade(>=|<=|>|<)(\d{1,3})$")
LAB_RE = re.compile(r"^lab:(\w+)([<>])(\d+(?:\.\d+)?)$")
EXAME_RE = re.compile(r"^exame:(texto|lab):([^@]+)(?:@(\d{1,5}))?$")
FORMULA = ("=", "+", "-", "@", "\t", "\r")  # início de célula que uma planilha executa como fórmula
TERMO_RE = re.compile(r"^[a-z0-9 .\-/]{2,60}$")  # termos já sem acento nem maiúsculas (como a fala normalizada)


@dataclass(frozen=True)
class Criterio:
    id: str
    texto: str
    regra: str
    pergunta: str = ""
    aplica: str = ""


@dataclass(frozen=True)
class Documento:
    rotulo: str
    tipo: str  # justificativa | exame:... | anexo


@dataclass(frozen=True)
class Vigencia:
    inicio: date | None
    fim: date | None

    def vale(self, hoje: date) -> bool:
        return (self.inicio is None or hoje >= self.inicio) and (self.fim is None or hoje <= self.fim)

    def texto(self) -> str:
        ini = self.inicio.strftime("%d/%m/%Y") if self.inicio else "sem início"
        fim = self.fim.strftime("%d/%m/%Y") if self.fim else "sem fim"
        return f"{ini} a {fim}"


@dataclass(frozen=True)
class Regra:
    id: str
    procedimento: str
    tipo: str
    termos: tuple[str, ...]
    cids: tuple[str, ...]
    idade_min: int | None
    idade_max: int | None
    criterios: tuple[Criterio, ...]
    documentos: tuple[Documento, ...]
    fonte: str
    versao: str
    vigencia: Vigencia
    origem: str
    conferencia: str
    observacao: str = ""

    @property
    def oficial(self) -> bool:
        return self.origem != "exemplo" and self.conferencia == "conferido"


@dataclass(frozen=True)
class ExigenciaOperadora:
    operadora_id: str
    nome: str
    aliases: tuple[str, ...]
    procedimento: str  # id da regra ou "*"
    documentos: tuple[Documento, ...]
    justificativa_obrigatoria: bool
    prazo_dias: int | None
    fonte: str
    versao: str
    vigencia: Vigencia
    origem: str
    conferencia: str


@dataclass
class Base:
    regras: dict[str, Regra] = field(default_factory=dict)
    operadoras: list[ExigenciaOperadora] = field(default_factory=list)
    erros: list[dict] = field(default_factory=list)  # linhas recusadas ao carregar (não entram na base ativa)

    def aviso(self) -> str:
        """Fala sempre que a base ativa não é a oficial conferida."""
        if not self.regras:
            return "Nenhuma regra de autorização carregada."
        if any(not r.oficial for r in self.regras.values()):
            return (
                "Regras de EXEMPLO ou pendentes de conferência humana: não são o texto oficial do Rol da ANS nem de "
                "operadora. Importe e confira a tabela vigente antes de depender delas."
            )
        return ""


# ---------- leitura e validação ----------
class LinhaInvalida(ValueError):
    def __init__(self, campo: str, mensagem: str):
        super().__init__(mensagem)
        self.campo = campo
        self.mensagem = mensagem


def _limpo(valor: str | None, campo: str, obrigatorio: bool = False, maximo: int = 600) -> str:
    bruto = (valor or "").lstrip(" ")
    texto = bruto.strip()
    if obrigatorio and not texto:
        raise LinhaInvalida(campo, "campo obrigatório vazio")
    if len(texto) > maximo:
        raise LinhaInvalida(campo, f"texto longo demais (máximo de {maximo} caracteres)")
    if any(unicodedata.category(ch) == "Cc" and ch not in "\n" for ch in texto) or "\x00" in texto:
        raise LinhaInvalida(campo, "caractere de controle")
    if bruto.startswith(FORMULA):
        raise LinhaInvalida(campo, "célula começa com = + - @ ou tabulação (fórmula de planilha): recusada")
    return texto


def _data(valor: str, campo: str) -> date | None:
    if not valor:
        return None
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise LinhaInvalida(campo, f"data inválida: '{valor}' (use AAAA-MM-DD)") from None


def _vigencia(linha: dict) -> Vigencia:
    ini = _data(_limpo(linha.get("vigencia_inicio"), "vigencia_inicio", obrigatorio=True, maximo=10), "vigencia_inicio")
    fim = _data(_limpo(linha.get("vigencia_fim"), "vigencia_fim", maximo=10), "vigencia_fim")
    if ini and fim and fim < ini:
        raise LinhaInvalida("vigencia_fim", "a vigência termina antes de começar")
    return Vigencia(ini, fim)


def _int(valor: str, campo: str, minimo: int, maximo: int) -> int | None:
    if not valor:
        return None
    if not re.fullmatch(r"\d{1,5}", valor):
        raise LinhaInvalida(campo, f"número inválido: '{valor}'")
    n = int(valor)
    if not minimo <= n <= maximo:
        raise LinhaInvalida(campo, f"fora do intervalo {minimo} a {maximo}: {n}")
    return n


def validar_regra_criterio(regra: str, campo: str = "criterios", permite_fala: bool = True) -> None:
    """Confere a gramática de uma regra de critério; levanta LinhaInvalida."""
    regra = regra.strip()
    if regra == "manual":
        if not permite_fala:
            raise LinhaInvalida(campo, "'manual' não pode ser condição de aplicação")
        return
    if regra.startswith("cid:"):
        prefixos = [p for p in regra[4:].split(",") if p]
        if not prefixos or not all(CID_RE.match(p) for p in prefixos):
            raise LinhaInvalida(campo, f"CID inválido na regra '{regra}'")
        return
    if IDADE_RE.match(regra):
        return
    if regra.startswith("fala:"):
        if not permite_fala:
            raise LinhaInvalida(campo, "'fala:' não pode ser condição de aplicação (sem fala, nada é inferido)")
        termos = [t for t in regra[5:].split("~") if t]
        if not termos or not all(TERMO_RE.match(t) for t in termos):
            raise LinhaInvalida(campo, f"termos da fala inválidos em '{regra}' (minúsculas, sem acento)")
        return
    m = LAB_RE.match(regra)
    if m:
        if m.group(1) not in codes.LABS:
            raise LinhaInvalida(campo, f"exame laboratorial desconhecido: '{m.group(1)}'")
        return
    m = EXAME_RE.match(regra)
    if m:
        kind, spec = m.group(1), m.group(2)
        if kind == "lab" and spec not in codes.LABS:
            raise LinhaInvalida(campo, f"exame laboratorial desconhecido: '{spec}'")
        if kind == "texto":
            termos = [t for t in spec.split("~") if t]
            if not termos or not all(TERMO_RE.match(t) for t in termos):
                raise LinhaInvalida(campo, f"termos do exame inválidos em '{regra}'")
        if m.group(3) and not 1 <= int(m.group(3)) <= 36500:
            raise LinhaInvalida(campo, "prazo do exame fora de 1 a 36500 dias")
        return
    raise LinhaInvalida(campo, f"regra de critério desconhecida: '{regra}'")


def _criterios(cell: str) -> tuple[Criterio, ...]:
    out: list[Criterio] = []
    for item in [i for i in cell.split("|") if i.strip()]:
        partes = [p.strip() for p in item.split("::")]
        if not 3 <= len(partes) <= 5:
            raise LinhaInvalida(
                "criterios", f"critério mal formado: '{item[:60]}' (id::texto::regra[::pergunta[::aplica]])"
            )
        cid_, texto, regra = partes[:3]
        pergunta = partes[3] if len(partes) > 3 else ""
        aplica = partes[4] if len(partes) > 4 else ""
        if not ID_RE.match(cid_):
            raise LinhaInvalida("criterios", f"id de critério inválido: '{cid_}'")
        if not texto:
            raise LinhaInvalida("criterios", f"critério '{cid_}' sem texto")
        validar_regra_criterio(regra)
        if aplica:
            validar_regra_criterio(aplica, permite_fala=False)
        if any(c.id == cid_ for c in out):
            raise LinhaInvalida("criterios", f"id de critério repetido: '{cid_}'")
        out.append(Criterio(cid_, texto, regra, pergunta, aplica))
    return tuple(out)


def _documentos(cell: str) -> tuple[Documento, ...]:
    out = []
    for item in [i for i in cell.split("|") if i.strip()]:
        rotulo, _, tipo = (p.strip() for p in item.partition("::"))
        if not rotulo or not tipo:
            raise LinhaInvalida("documentos", f"documento mal formado: '{item[:60]}' (rótulo::tipo)")
        if tipo not in ("justificativa", "anexo"):
            validar_regra_criterio(tipo, "documentos", permite_fala=False)
            if not tipo.startswith("exame:"):
                raise LinhaInvalida("documentos", f"tipo de documento inválido: '{tipo}'")
        out.append(Documento(rotulo, tipo))
    return tuple(out)


def _origem(linha: dict, forcar_importado: bool) -> tuple[str, str]:
    origem = _limpo(linha.get("origem"), "origem", maximo=20) or "importado"
    if origem not in ORIGENS:
        raise LinhaInvalida("origem", f"origem inválida: '{origem}' (exemplo ou importado)")
    conferencia = _limpo(linha.get("conferencia"), "conferencia", maximo=20) or "pendente"
    if conferencia not in CONFERENCIAS:
        raise LinhaInvalida("conferencia", f"conferência inválida: '{conferencia}'")
    if forcar_importado:  # o arquivo que chega nunca se declara oficial nem conferido
        return "importado", "pendente"
    return origem, conferencia


def _linha_regra(linha: dict, forcar_importado: bool) -> Regra:
    rid = _limpo(linha.get("id"), "id", obrigatorio=True, maximo=60)
    if not ID_RE.match(rid):
        raise LinhaInvalida("id", f"id inválido: '{rid}' (minúsculas, números e _)")
    tipo = _limpo(linha.get("tipo"), "tipo", obrigatorio=True, maximo=20)
    if tipo not in TIPOS:
        raise LinhaInvalida("tipo", f"tipo inválido: '{tipo}' ({', '.join(TIPOS)})")
    termos = tuple(t for t in _limpo(linha.get("termos"), "termos", obrigatorio=True, maximo=1200).split("|") if t)
    if not termos or not all(TERMO_RE.match(t) for t in termos):
        raise LinhaInvalida("termos", "termos devem estar em minúsculas, sem acento (como a fala normalizada)")
    cids = tuple(c for c in _limpo(linha.get("cids"), "cids", maximo=300).replace(" ", "").split(",") if c)
    if not all(CID_RE.match(c) for c in cids):
        raise LinhaInvalida("cids", "CID inválido (use o prefixo, como M17 ou M25.5)")
    imin = _int(_limpo(linha.get("idade_min"), "idade_min", maximo=3), "idade_min", 0, 130)
    imax = _int(_limpo(linha.get("idade_max"), "idade_max", maximo=3), "idade_max", 0, 130)
    if imin is not None and imax is not None and imin > imax:
        raise LinhaInvalida("idade_max", "idade máxima menor que a mínima")
    criterios = _criterios(_limpo(linha.get("criterios"), "criterios", maximo=6000))
    documentos = _documentos(_limpo(linha.get("documentos"), "documentos", maximo=2000))
    fonte = _limpo(linha.get("fonte"), "fonte", obrigatorio=True, maximo=400)
    versao = _limpo(linha.get("versao"), "versao", obrigatorio=True, maximo=40)
    origem, conferencia = _origem(linha, forcar_importado)
    return Regra(
        rid,
        _limpo(linha.get("procedimento"), "procedimento", obrigatorio=True, maximo=160),
        tipo,
        termos,
        cids,
        imin,
        imax,
        criterios,
        documentos,
        fonte,
        versao,
        _vigencia(linha),
        origem,
        conferencia,
        _limpo(linha.get("observacao"), "observacao", maximo=400),
    )


def _linha_operadora(linha: dict, forcar_importado: bool) -> ExigenciaOperadora:
    oid = _limpo(linha.get("operadora_id"), "operadora_id", obrigatorio=True, maximo=60)
    if not ID_RE.match(oid):
        raise LinhaInvalida("operadora_id", f"id inválido: '{oid}'")
    proc = _limpo(linha.get("procedimento"), "procedimento", obrigatorio=True, maximo=60)
    if proc != "*" and not ID_RE.match(proc):
        raise LinhaInvalida("procedimento", f"procedimento inválido: '{proc}' (id da regra ou *)")
    just = _limpo(linha.get("justificativa_obrigatoria"), "justificativa_obrigatoria", maximo=3).lower()
    if just not in ("sim", "nao", ""):
        raise LinhaInvalida("justificativa_obrigatoria", "use sim ou nao")
    origem, conferencia = _origem(linha, forcar_importado)
    aliases = tuple(norm(a) for a in _limpo(linha.get("aliases"), "aliases", maximo=300).split("|") if a.strip())
    return ExigenciaOperadora(
        oid,
        _limpo(linha.get("nome"), "nome", obrigatorio=True, maximo=120),
        aliases,
        proc,
        _documentos(_limpo(linha.get("documentos"), "documentos", maximo=2000)),
        just == "sim",
        _int(_limpo(linha.get("prazo_dias"), "prazo_dias", maximo=4), "prazo_dias", 0, 3650),
        _limpo(linha.get("fonte"), "fonte", obrigatorio=True, maximo=400),
        _limpo(linha.get("versao"), "versao", obrigatorio=True, maximo=40),
        _vigencia(linha),
        origem,
        conferencia,
    )


@dataclass
class Relatorio:
    """Resultado da conferência de uma tabela: nada é gravado se `ok` for falso."""

    tabela: str
    linhas: int = 0
    aceitas: int = 0
    erros: list[dict] = field(default_factory=list)
    itens: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.erros and self.aceitas > 0

    def publico(self) -> dict:
        return {
            "tabela": self.tabela,
            "ok": self.ok,
            "linhas": self.linhas,
            "aceitas": self.aceitas,
            "erros": self.erros[:MAX_ERROS],
            "truncado": len(self.erros) > MAX_ERROS,
            "pendencia": "Toda linha importada fica com conferência pendente até uma pessoa conferir a norma vigente."
            if self.ok
            else "",
        }


def _ler(texto: str, tabela: str, colunas: list[str], criar, chave, forcar_importado: bool) -> Relatorio:
    rel = Relatorio(tabela)
    if len(texto.encode("utf-8", errors="replace")) > MAX_BYTES:
        rel.erros.append({"linha": 0, "campo": "arquivo", "mensagem": f"arquivo maior que {MAX_BYTES // 1000} KB"})
        return rel
    if "\x00" in texto:
        rel.erros.append({"linha": 0, "campo": "arquivo", "mensagem": "arquivo binário ou com byte nulo"})
        return rel
    leitor = csv.reader(io.StringIO(texto.lstrip("﻿"), newline=""))
    try:
        cabecalho = next(leitor)
    except (StopIteration, csv.Error):
        rel.erros.append({"linha": 1, "campo": "cabecalho", "mensagem": "arquivo vazio ou sem cabeçalho"})
        return rel
    cabecalho = [c.strip() for c in cabecalho]
    faltam = [c for c in colunas if c not in cabecalho]
    if faltam:
        rel.erros.append({"linha": 1, "campo": "cabecalho", "mensagem": "colunas ausentes: " + ", ".join(faltam)})
        return rel
    vistos: set = set()
    try:
        for n, valores in enumerate(leitor, start=2):
            if not any(v.strip() for v in valores):
                continue
            rel.linhas += 1
            if rel.linhas > MAX_LINHAS:
                rel.erros.append({"linha": n, "campo": "arquivo", "mensagem": f"mais de {MAX_LINHAS} linhas"})
                break
            if len(valores) != len(cabecalho):
                rel.erros.append(
                    {
                        "linha": n,
                        "campo": "linha",
                        "mensagem": f"{len(valores)} colunas, o cabeçalho tem {len(cabecalho)}",
                    }
                )
                continue
            linha = dict(zip(cabecalho, valores, strict=True))
            try:
                item = criar(linha, forcar_importado)
                k = chave(item)
                if k in vistos:
                    raise LinhaInvalida("id", f"linha repetida para '{k}'")
                vistos.add(k)
            except LinhaInvalida as exc:
                rel.erros.append({"linha": n, "campo": exc.campo, "mensagem": exc.mensagem})
                continue
            rel.itens.append(item)
            rel.aceitas += 1
    except csv.Error as exc:
        rel.erros.append({"linha": 0, "campo": "arquivo", "mensagem": f"CSV malformado: {exc}"})
    return rel


def validar_regras(texto: str, importado: bool = True) -> Relatorio:
    return _ler(texto, "regras", COLUNAS_REGRAS, _linha_regra, lambda r: r.id, importado)


def validar_operadoras(texto: str, importado: bool = True) -> Relatorio:
    return _ler(
        texto,
        "operadoras",
        COLUNAS_OPERADORAS,
        _linha_operadora,
        lambda e: (e.operadora_id, e.procedimento, e.vigencia.inicio),
        importado,
    )


def gravar_importacao(texto: str, tabela: str, destino: Path) -> Relatorio:
    """Grava a tabela só se não houver erro nenhum, normalizada (origem importado, conferência pendente)."""
    rel = validar_regras(texto) if tabela == "regras" else validar_operadoras(texto)
    if not rel.ok:
        raise ValueError(f"{len(rel.erros)} erro(s) na tabela; nada foi gravado")
    cols = COLUNAS_REGRAS if tabela == "regras" else COLUNAS_OPERADORAS
    leitor = csv.DictReader(io.StringIO(texto.lstrip("﻿"), newline=""))
    buf = io.StringIO()
    w = csv.DictWriter(buf, cols, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for linha in leitor:
        if not any((v or "").strip() for v in linha.values()):
            continue
        linha["origem"], linha["conferencia"] = "importado", "pendente"
        w.writerow({c: (linha.get(c) or "").strip() for c in cols})
    destino.write_text(buf.getvalue(), encoding="utf-8", newline="")
    return rel


# ---------- base ativa ----------
def _caminho(config: str, padrao: str) -> Path:
    return Path(config) if config else get_settings().data_dir / "reference" / padrao


def _ler_arquivo(caminho: Path) -> str:
    try:
        return caminho.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("autorização prévia: não consegui ler %s (%s)", caminho.name, type(exc).__name__)
        return ""


@lru_cache(maxsize=4)
def _carregar(regras_path: str, operadoras_path: str) -> Base:
    base = Base()
    r = _ler(_ler_arquivo(Path(regras_path)), "regras", COLUNAS_REGRAS, _linha_regra, lambda x: x.id, False)
    base.regras = {x.id: x for x in r.itens}
    base.erros += [{"tabela": "regras"} | e for e in r.erros]
    o = _ler(
        _ler_arquivo(Path(operadoras_path)),
        "operadoras",
        COLUNAS_OPERADORAS,
        _linha_operadora,
        lambda e: (e.operadora_id, e.procedimento, e.vigencia.inicio),
        False,
    )
    base.operadoras = list(o.itens)
    base.erros += [{"tabela": "operadoras"} | e for e in o.erros]
    if base.erros:
        log.warning("autorização prévia: %d linha(s) recusadas ao carregar a base", len(base.erros))
    return base


def base_ativa() -> Base:
    s = get_settings()
    return _carregar(
        str(_caminho(s.autorizacao_regras_csv, "rol_dut_exemplo.csv")),
        str(_caminho(s.autorizacao_operadoras_csv, "operadoras_exemplo.csv")),
    )


def limpar_cache() -> None:
    _carregar.cache_clear()


def achar_operadora(base: Base, nome: str | None) -> tuple[str, str] | None:
    """(id, nome) da operadora cadastrada que o texto nomeia (id, nome ou apelido, sem acento); None se não houver."""
    alvo = norm(nome or "")
    if not alvo:
        return None
    for e in base.operadoras:
        if alvo in (norm(e.operadora_id.replace("_", " ")), norm(e.nome), *e.aliases):
            return e.operadora_id, e.nome
    return None
