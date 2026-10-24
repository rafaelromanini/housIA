"""Laudos em PDF ou foto viram exames com fonte (#48): extração on-premise, só por regras, sem IA externa.

O arquivo nunca é executado nem interpretado como instrução: PDF é lido por texto (pdfplumber; sem ele, um leitor
mínimo de fluxos Flate) e foto por OCR (tesseract em português; em `LLM_MODE=mock` o texto vem de um metadado
sintético do próprio arquivo, para o resultado ser determinístico). O texto do laudo é dado: só linhas que começam
com o nome de um exame conhecido, com valor e unidade compatíveis, viram valor; o resto é ignorado. Cada valor leva a
fonte `Documento/N p. X` e o trecho. O texto integral nunca é guardado nem auditado.
"""

from __future__ import annotations

import io
import re
import struct
import unicodedata
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache

from app.consult import knowledge
from app.consult import text as textutil
from app.fhir_client import codes

MAX_BYTES = 8 * 1024 * 1024
MAX_PAGES = 30
MAX_VALORES = 80
MAX_PIXELS = 40_000_000
MAX_STREAM = 8 * 1024 * 1024  # texto descomprimido por fluxo do PDF (contra bomba de descompressão)
MAX_TEXTO_PAGINA = 60_000

# conteúdo ativo ou criptografado: o arquivo é recusado, nunca "executado" ou contornado
ATIVO = (b"/JavaScript", b"/JS", b"/Launch", b"/OpenAction", b"/AA", b"/EmbeddedFile", b"/RichMedia", b"/XFA")


class LaudoInvalido(Exception):
    """Arquivo recusado; `status` é o código HTTP e `args[0]` a mensagem em português."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass
class Extraido:
    tipo: str  # "pdf" | "imagem"
    paginas: list[str]
    valores: list[dict] = field(default_factory=list)
    ignorados: list[dict] = field(default_factory=list)
    data_laudo: str | None = None
    nome_laudo: str | None = None
    avisos: list[str] = field(default_factory=list)


# ---------- tipo e segurança do arquivo ----------
def detectar(data: bytes) -> str:
    """Tipo pelos primeiros bytes (magic bytes); o Content-Type e a extensão informados não valem."""
    head = data[:16]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    raise LaudoInvalido("Formato não aceito: envie um PDF, uma foto PNG ou uma foto JPEG.", 415)


def conferir_pdf(data: bytes) -> None:
    if b"/Encrypt" in data:
        raise LaudoInvalido("PDF protegido por senha: não é possível ler. Envie uma cópia sem proteção.")
    achados = [t.decode() for t in ATIVO if t in data]
    if achados:
        raise LaudoInvalido("PDF com conteúdo ativo (script, ação automática ou anexo): recusado por segurança.")
    if data.count(b"/Type /Page") + data.count(b"/Type/Page") > 4 * MAX_PAGES + 8:
        raise LaudoInvalido(f"PDF com páginas demais (máximo {MAX_PAGES}).", 413)
    # bomba de descompressão: nenhum fluxo Flate pode passar do teto (vale para o pdfplumber também)
    total = 0
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            d = zlib.decompressobj()
            total += len(d.decompress(m.group(1), MAX_STREAM))
            if d.unconsumed_tail or total > 4 * MAX_STREAM:
                raise LaudoInvalido("PDF com conteúdo descomprimido grande demais.", 413)
        except zlib.error:
            continue  # não é Flate (imagem JPEG, texto puro): o leitor decide


def dimensoes(data: bytes, tipo: str) -> tuple[int, int]:
    """Largura e altura lidas do cabeçalho, para recusar imagem gigante antes de abri-la."""
    try:
        if tipo == "png":
            if data[12:16] != b"IHDR":
                raise ValueError
            return struct.unpack(">II", data[16:24])
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            size = struct.unpack(">H", data[i + 2 : i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return w, h
            i += 2 + size
    except (struct.error, ValueError):
        pass
    raise LaudoInvalido("Imagem ilegível ou corrompida.")


def limpar(text: str, limite: int = 300) -> str:
    """Sem caracteres de controle nem invisíveis (Cc/Cf/Cs/Co); espaços normalizados."""
    out = "".join(" " if ch in "\t\r " else ch for ch in text if ch in "\n\t\r" or unicodedata.category(ch)[0] != "C")
    return re.sub(r"[ ]+", " ", out).strip()[:limite]


# ---------- texto do PDF ----------
def paginas_pdf(data: bytes) -> list[str]:
    try:
        import pdfplumber  # noqa: PLC0415 (dependência opcional: sem ela entra o leitor mínimo)
    except ImportError:
        pdfplumber = None
    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                if len(pdf.pages) > MAX_PAGES:
                    raise LaudoInvalido(f"PDF com páginas demais (máximo {MAX_PAGES}).", 413)
                return [(p.extract_text() or "")[:MAX_TEXTO_PAGINA] for p in pdf.pages]
        except LaudoInvalido:
            raise
        except Exception:  # noqa: BLE001 - qualquer falha do leitor é "PDF ilegível"; nada do arquivo é repassado
            pass
    return _paginas_minimo(data)


_OBJ = re.compile(rb"(\d+)\s+\d+\s+obj\b(.*?)\bendobj", re.S)
_STR_HEX = re.compile(rb"<([0-9A-Fa-f\s]*)>\s*Tj")
_STR_LIT = re.compile(rb"\(((?:\\.|[^\\()])*)\)\s*(?:Tj|'|\")")
_TJ = re.compile(rb"\[((?:.|\n)*?)\]\s*TJ")
_TJ_PART = re.compile(rb"<([0-9A-Fa-f\s]*)>|\(((?:\\.|[^\\()])*)\)|(-?\d+(?:\.\d+)?)")


def _lit(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == 0x5C and i + 1 < len(raw):
            i += 1
            nxt = raw[i]
            mapa = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
            if nxt in mapa:
                out.append(mapa[nxt])
            elif 48 <= nxt <= 55:
                j = i
                while j < len(raw) and j < i + 3 and 48 <= raw[j] <= 55:
                    j += 1
                out.append(int(raw[i:j], 8) & 0xFF)
                i = j - 1
            else:
                out.append(nxt)
        else:
            out.append(ch)
        i += 1
    return out.decode("cp1252", errors="replace")


def _hex(raw: bytes) -> str:
    h = re.sub(rb"\s+", b"", raw)
    if len(h) % 2:
        h += b"0"
    return bytes.fromhex(h.decode()).decode("cp1252", errors="replace")


def _texto_fluxo(stream: bytes) -> str:
    lines: list[str] = []
    for line in stream.split(b"\n"):
        parts: list[str] = []
        for m in _STR_HEX.finditer(line):
            parts.append(_hex(m.group(1)))
        for m in _STR_LIT.finditer(line):
            parts.append(_lit(m.group(1)))
        for m in _TJ.finditer(line):
            seg = ""
            for p in _TJ_PART.finditer(m.group(1)):
                if p.group(1) is not None:
                    seg += _hex(p.group(1))
                elif p.group(2) is not None:
                    seg += _lit(p.group(2))
                elif float(p.group(3)) < -200:
                    seg += " "
            parts.append(seg)
        if parts:
            lines.append("".join(parts))
    return "\n".join(lines)


def _paginas_minimo(data: bytes) -> list[str]:
    """Leitor mínimo: páginas na ordem de /Kids, fluxos em texto ou Flate, strings hex ou literais. Não lê PDF com
    fluxos de objetos, fontes subconjunto ou CMap próprio (para esses vale o pdfplumber)."""
    objs: dict[int, bytes] = {int(m.group(1)): m.group(2) for m in _OBJ.finditer(data)}
    if not objs or b"%%EOF" not in data[-2048:] and b"trailer" not in data:
        raise LaudoInvalido("PDF corrompido ou incompleto.")

    def conteudo(num: int) -> str:
        body = objs.get(num, b"")
        m = re.search(rb"stream\r?\n(.*?)\r?\nendstream", body, re.S)
        if not m:
            return ""
        raw = m.group(1)
        if b"/FlateDecode" in body[: m.start()]:
            try:
                d = zlib.decompressobj()
                raw = d.decompress(raw, MAX_STREAM)
                if d.unconsumed_tail:
                    raise LaudoInvalido("PDF com conteúdo descomprimido grande demais.", 413)
            except zlib.error as exc:
                raise LaudoInvalido("PDF corrompido: conteúdo ilegível.") from exc
        return _texto_fluxo(raw)[:MAX_TEXTO_PAGINA]

    kids: list[int] = []
    for num in sorted(objs):
        body = objs[num]
        if re.search(rb"/Type\s*/Pages\b", body):
            m = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S)
            if m:
                kids = [int(k) for k in re.findall(rb"(\d+)\s+\d+\s+R", m.group(1))]
                break
    if not kids:
        kids = [n for n in sorted(objs) if re.search(rb"/Type\s*/Page\b(?!s)", objs[n])]
    if not kids:
        raise LaudoInvalido("PDF sem páginas legíveis.")
    if len(kids) > MAX_PAGES:
        raise LaudoInvalido(f"PDF com páginas demais (máximo {MAX_PAGES}).", 413)
    out = []
    for k in kids:
        body = objs.get(k, b"")
        refs = re.findall(rb"/Contents\s*\[?\s*((?:\d+\s+\d+\s+R\s*)+)", body)
        nums = [int(x) for r in refs for x in re.findall(rb"(\d+)\s+\d+\s+R", r)]
        out.append("\n".join(conteudo(n) for n in nums))
    return out


# ---------- texto da foto ----------
def texto_imagem(data: bytes, tipo: str, modo_mock: bool) -> str:
    w, h = dimensoes(data, tipo)
    if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
        raise LaudoInvalido("Imagem grande demais (máximo 40 megapixels).", 413)
    if modo_mock:
        txt = _ocr_sintetico(data, tipo)
        if not txt:
            raise LaudoInvalido("Foto sem texto reconhecível (modo de demonstração).")
        return txt
    try:
        import pytesseract  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:
        raise LaudoInvalido("Leitura de fotos indisponível neste servidor (OCR não instalado).", 503) from exc
    try:
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            return pytesseract.image_to_string(img, lang="por", timeout=30)[:MAX_TEXTO_PAGINA]
    except pytesseract.TesseractNotFoundError as exc:
        raise LaudoInvalido("Leitura de fotos indisponível neste servidor (tesseract não instalado).", 503) from exc
    except Exception as exc:  # noqa: BLE001
        raise LaudoInvalido("Imagem ilegível ou corrompida.") from exc


def _ocr_sintetico(data: bytes, tipo: str) -> str:
    """Modo de demonstração: o texto da foto sintética vem do metadado `housia-ocr` (PNG tEXt) ou do JPEG."""
    if tipo == "png":
        i = 8
        while i + 8 <= len(data):
            size, kind = struct.unpack(">I4s", data[i : i + 8])
            if kind == b"tEXt":
                chunk = data[i + 8 : i + 8 + size]
                key, _, val = chunk.partition(b"\x00")
                if key == b"housia-ocr":
                    return val.decode("utf-8", errors="replace")[:MAX_TEXTO_PAGINA]
            if kind == b"IEND" or size > len(data):
                break
            i += 12 + size
        return ""
    i = 2
    while i + 4 < len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        size = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker == 0xFE:
            com = data[i + 4 : i + 2 + size].decode("utf-8", errors="replace")
            if com.startswith("housia-ocr:"):
                return com[len("housia-ocr:") :][:MAX_TEXTO_PAGINA]
        i += 2 + size
    return ""


# ---------- valores ----------
_UNIT_ALIAS: dict[str, tuple[str, float]] = {
    "mg/dl": ("mg/dl", 1),
    "g/dl": ("g/dl", 1),
    "%": ("%", 1),
    "mmol/l": ("mmol/l", 1),
    "meq/l": ("mmol/l", 1),  # íon monovalente (sódio, potássio): 1 mEq = 1 mmol
    "mil/mm3": ("mil/mm3", 1),
    "x10^3/ul": ("mil/mm3", 1),
    "10^3/ul": ("mil/mm3", 1),
    "x10³/mm³": ("mil/mm3", 1),
    "/mm3": ("mil/mm3", 0.001),
    "/ul": ("mil/mm3", 0.001),
    "u/l": ("u/l", 1),
    "ui/l": ("u/l", 1),
    "ng/ml": ("ng/ml", 1),
    "pg/ml": ("pg/ml", 1),
    "mui/l": ("mui/l", 1),
    "uui/ml": ("mui/l", 1),  # µUI/mL = mUI/L
    "ng/dl": ("ng/dl", 1),
    "mg/l": ("mg/l", 1),
}
_PER_MM3 = {"/mm3", "/ul"}


def _unit_key(u: str) -> str:
    u = u.lower().replace(" ", "").replace("³", "3").replace("µ", "u").replace("μ", "u")
    return u.replace("mm^3", "mm3").replace("mcl", "ul")


@lru_cache
def _lexico() -> tuple[re.Pattern, dict[str, str]]:
    table = {
        syn: a.key for a in knowledge.analytes() if not a.qualitative and a.key in codes.LABS for syn in a.synonyms
    }
    names = "|".join(re.escape(t) for t in sorted(table, key=len, reverse=True))
    pat = re.compile(
        rf"^(?P<nome>{names})(?![a-z0-9])(?P<resto>[^0-9<>]{{0,40}}?)\s*"
        rf"(?P<cmp>[<>]=?)?\s*(?P<num>\d[\d.,]*)\s*(?P<un>[a-zA-Zµμ%/³^0-9.]*(?:/[a-zA-Zµμ0-9³^.]+)?)",
    )
    return pat, table


_DATA_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_NOME_RE = re.compile(r"^\s*(?:nome do paciente|paciente|nome)\s*[:\-]\s*(.+)$", re.I)


def _numero(raw: str, un_key: str) -> float | None:
    s = raw.strip(".,")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s) and un_key in _PER_MM3:
        return float(s.replace(".", ""))
    if s.count(",") + s.count(".") > 1:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _data_valida(m: re.Match, hoje: date) -> date | None:
    try:
        d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return d if date(1990, 1, 1) <= d <= hoje else None


def extrair(tipo: str, paginas: list[str], hoje: date) -> Extraido:
    """Texto por página → valores propostos (só os de linha que começa pelo nome do exame, com unidade compatível)."""
    out = Extraido(tipo=tipo, paginas=[])
    pat, table = _lexico()
    vistos: dict[str, dict] = {}
    conflitos: set[str] = set()
    datas: list[tuple[int, date]] = []
    for pno, raw_page in enumerate(paginas, start=1):
        page = limpar_pagina(raw_page)
        out.paginas.append("")  # o texto não é mantido
        for raw_line in page.split("\n"):
            line = limpar(raw_line)
            if not line:
                continue
            if out.nome_laudo is None and (mn := _NOME_RE.match(line)):
                out.nome_laudo = mn.group(1).strip()[:120]
                continue
            low = textutil.norm(line)
            if (md := _DATA_RE.search(line)) and any(w in low for w in ("coleta", "emissao", "realizad", "data")):
                if (d := _data_valida(md, hoje)) is not None:
                    datas.append((0 if "coleta" in low else 1, d))
                continue
            line_nopar = re.sub(r"\([^)]*\)", " ", line)
            m = pat.match(textutil.norm(line_nopar).replace("µ", "u"))
            if not m:
                continue
            key = table[m.group("nome")]
            measure = codes.LABS[key]
            nome = measure.label
            if len(vistos) + len(out.ignorados) >= MAX_VALORES:
                out.avisos.append(f"Limite de {MAX_VALORES} linhas de exame por documento: o restante foi ignorado.")
                break
            ukey = _unit_key(m.group("un") or "")
            if m.group("cmp"):
                out.ignorados.append({"pagina": pno, "exame": nome, "motivo": "valor com sinal de menor ou maior"})
                continue
            num = _numero(m.group("num"), ukey)
            if num is None or num <= 0:
                out.ignorados.append({"pagina": pno, "exame": nome, "motivo": "valor numérico ilegível"})
                continue
            want = _UNIT_ALIAS.get(_unit_key(measure.unit)) if measure.unit else None
            if measure.unit:
                got = _UNIT_ALIAS.get(ukey)
                if not ukey:
                    out.ignorados.append({"pagina": pno, "exame": nome, "motivo": "sem unidade no laudo"})
                    continue
                if got is None or want is None or got[0] != want[0]:
                    out.ignorados.append(
                        {"pagina": pno, "exame": nome, "motivo": f"unidade diferente da esperada ({measure.unit})"}
                    )
                    continue
                num = round(num * got[1], 4)
            elif ukey and ukey not in ("", "ratio"):
                out.ignorados.append({"pagina": pno, "exame": nome, "motivo": "unidade inesperada"})
                continue
            high = measure.high
            if num > (high or measure.crit_high or 1000) * 50:
                out.ignorados.append({"pagina": pno, "exame": nome, "motivo": "valor fora do possível"})
                continue
            if key in vistos:
                if abs(vistos[key]["valor"] - num) > 1e-9:
                    conflitos.add(key)
                continue
            vistos[key] = {
                "chave": key,
                "exame": nome,
                "loinc": measure.code,
                "valor": num,
                "unidade": measure.unit,
                "pagina": pno,
                "trecho": limpar(line, 140),
            }
    for key in conflitos:
        v = vistos.pop(key)
        out.ignorados.append(
            {"pagina": v["pagina"], "exame": v["exame"], "motivo": "valores conflitantes no documento"}
        )
    out.valores = list(vistos.values())
    if datas:
        out.data_laudo = min(datas, key=lambda x: x[0])[1].isoformat()  # a data da coleta vale mais que a de emissão
    else:
        out.avisos.append("O laudo não traz a data da coleta: confira antes de aceitar.")
    return out


def limpar_pagina(text: str) -> str:
    return "\n".join(limpar(line) for line in text[:MAX_TEXTO_PAGINA].splitlines())


def identidade(nome_laudo: str | None, nome_paciente: str) -> str:
    """ "confere" | "divergente" | "nao_encontrada": primeiro e último nome do paciente têm de aparecer no laudo."""
    if not nome_laudo:
        return "nao_encontrada"
    got = set(textutil.norm(nome_laudo).split())
    parts = [p for p in textutil.norm(nome_paciente).split() if len(p) > 2]
    if not parts:
        return "nao_encontrada"
    return "confere" if parts[0] in got and parts[-1] in got else "divergente"


def ref(n: int, pagina: int) -> str:
    return f"Documento/{n} p. {pagina}"


def refs_validas(extra: dict | None) -> set[str]:
    return {ref(x["n"], v["pagina"]) for x in (extra or {}).get("laudos") or [] for v in x.get("valores") or []}


def data_do(laudo: dict) -> datetime | None:
    d = laudo.get("data_laudo")
    return datetime.fromisoformat(d) if d else None


# ---------- no Smart App Exames, nos alertas e nas propostas ----------
def _status(key: str, value: float, diabetico: bool) -> str:
    m = codes.LABS[key]
    high = 7.0 if key == "hba1c" and diabetico else 100 if key == "ldl" and diabetico else m.high
    if m.low is not None and value < m.low:
        return "baixo"
    return "alto" if high is not None and value > high else "normal"


def _anterior(chart, key: str, quando: date | None):
    """Último resultado da série FHIR anterior ao laudo (sem data no laudo: o mais recente da série)."""
    series = chart.labs.get(key, [])
    if quando is not None:
        series = [e for e in series if e.timestamp.date() < quando]
    return series[-1] if series else None


def _mesmo_dia(chart, key: str, quando: date | None, valor: float):
    if quando is None:
        return None
    return next(
        (
            e
            for e in chart.labs.get(key, [])
            if e.timestamp.date() == quando and abs(float(e.value["valor"]) - valor) < 1e-6
        ),
        None,
    )


def comparar(chart, v: dict, quando: date | None) -> tuple[str, str | None]:
    """Texto da comparação com a série FHIR e a referência do resultado anterior."""
    from app.consult.chart import fmt_date, fmt_num  # noqa: PLC0415

    m = codes.LABS[v["chave"]]
    if (same := _mesmo_dia(chart, v["chave"], quando, v["valor"])) is not None:
        return f"já consta no prontuário na mesma data ({fmt_date(same.timestamp)})", same.source_ref
    prev = _anterior(chart, v["chave"], quando)
    if prev is None:
        return "sem resultado anterior desse exame no prontuário", None
    before = float(prev.value["valor"])
    txt = fmt_num(before, m.decimals)
    when = fmt_date(prev.timestamp)
    if abs(v["valor"] - before) < max(0.05 * before, 10**-m.decimals):
        return f"estável em relação a {when} ({txt})", prev.source_ref
    way = "subiu" if v["valor"] > before else "caiu"
    return f"{way} de {txt} ({when})", prev.source_ref


def exam_infos(laudos: list[dict], chart):
    """ExamInfo de cada valor de laudo, com a fonte `Documento/N p. X` e a comparação com a série FHIR."""
    from app.consult.chart import fmt_num  # noqa: PLC0415
    from app.consult.schemas import ExamInfo  # noqa: PLC0415

    diabetico = chart.has_condition("E11") is not None or chart.has_condition("E10") is not None
    lay = knowledge.lab_lay()
    out = []
    for laudo in laudos:
        quando = date.fromisoformat(laudo["data_laudo"]) if laudo.get("data_laudo") else None
        for v in laudo.get("valores") or []:
            key, m = v["chave"], codes.LABS[v["chave"]]
            info = lay.get(key) or {}
            status = _status(key, v["valor"], diabetico)
            value_txt = f"{fmt_num(v['valor'], m.decimals)} {m.unit}".strip()
            comp, prev_ref = comparar(chart, v, quando)
            fonte = ref(laudo["n"], v["pagina"])
            verdict = info.get(status) or info.get("normal") or ""
            explanation = f"{m.label}" + (f" {info['o_que_e']}" if info.get("o_que_e") else "")
            explanation += f". O resultado do laudo ({value_txt}) {verdict}." if verdict else f": {value_txt} no laudo."
            out.append(
                ExamInfo(
                    exame=m.label,
                    chave=key,
                    valor=value_txt,
                    data=quando.strftime("%d/%m/%Y") if quando else "data do laudo não informada",
                    status=status,
                    explicacao=explanation,
                    tendencia=comp,
                    refs=[fonte] + ([prev_ref] if prev_ref else []),
                    origem_dado=f"laudo ({fonte})",
                    comparacao=comp,
                    valor_num=v["valor"],
                )
            )
    return out


def alertas(laudos: list[dict], chart):
    """Valor crítico (faixa crítica de codes.LABS) vira alerta crítico com a fonte do laudo."""
    from app.consult.chart import fmt_num  # noqa: PLC0415
    from app.consult.schemas import Highlight  # noqa: PLC0415

    out = []
    for laudo in laudos:
        for v in laudo.get("valores") or []:
            m = codes.LABS[v["chave"]]
            if codes.classify(m, v["valor"]) == "critico":
                out.append(
                    Highlight(
                        texto=f"Crítico no laudo: {m.label} {fmt_num(v['valor'], m.decimals)} {m.unit}".rstrip()
                        + ". Confirme com o paciente e com o laboratório antes de agir.",
                        severidade="critico",
                        refs=[ref(laudo["n"], v["pagina"])],
                        categoria="laudo",
                    )
                )
    return out


def publico(laudo: dict) -> dict:
    return {
        "n": laudo["n"],
        "ref": f"Documento/{laudo['n']}",
        "tipo": laudo["tipo"],
        "paginas": laudo["paginas"],
        "data_laudo": laudo.get("data_laudo"),
        "identidade": laudo.get("identidade"),
        "avisos": laudo.get("avisos", []),
        "valores": [
            {k: v.get(k) for k in ("chave", "exame", "valor", "unidade", "pagina", "trecho", "ref", "comparacao")}
            for v in laudo.get("valores", [])
        ],
        "ignorados": laudo.get("ignorados", []),
        "guardado": laudo.get("guardado_ref"),
        "em": laudo.get("em"),
    }
