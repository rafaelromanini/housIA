"""OPME no centro cirúrgico: guia autorizada, consumo por etiqueta GS1 e conciliação em três vias.

- Guia autorizada: entrada manual (com autocomplete do catálogo), JSON FHIR ClaimResponse ou XML de guia TISS. O
  catálogo (data/reference/opme_demo.csv) é de DEMONSTRAÇÃO (códigos DEMO-OPME-*, GTIN e registro ANVISA
  fictícios); itens fora dele entram pela guia, com a descrição e as palavras que o cirurgião usa.
- Consumo: etiqueta GS1 lida pela câmera (app/consult/gs1.py) → GTIN, lote, validade e série.
- Conciliação: o narrado (Fala/N, com a quantidade e o tamanho dito), o autorizado e o consumido, cada um com o seu
  status; validade vencida é crítica; divergência de quantidade, item sem guia, sem etiqueta ou sem fala é pendência.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from zoneinfo import ZoneInfo

from app import clock
from app.config import get_settings
from app.consult import gs1, knowledge
from app.consult.schemas import Highlight, OpmeLine, OpmeLote, Segment, ViaStatus, fala_ref
from app.consult.surgical_checks import affirmative, clauses, negated
from app.consult.text import norm

NUMBERS = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6}
GTIN_CODE_PREFIX = "GTIN-"  # item lido na etiqueta e fora do catálogo e da guia
MAX_XML = 1_000_000
MAX_QTY = 99

# tamanho dito junto do material: "parafuso 3,5 por 40", "componente femoral tamanho 5", "placa de 6 mm"
_N = r"\d+(?:[,.]\d+)?"
_MM = r"(?:mm|milimetros?)"
SIZE_DIM = re.compile(rf"({_N})\s*{_MM}?\s*(?:por|x)\s*({_N})\s*{_MM}?(?!\d|[,.]\d)")
SIZE_LABEL = re.compile(r"(?<![a-z])(tamanho|numero)\s+(\d+[a-z]?|[a-z])(?![a-z0-9])")
SIZE_MM = re.compile(rf"(?<![\d,.])({_N})\s*{_MM}(?![a-z])")
SIZE_SPLIT = re.compile(r"[.;,](?!\d)|\s+e\s+")


@dataclass(frozen=True)
class Entry:
    """Item conhecido: do catálogo ou da guia."""

    code: str
    description: str
    words: tuple[str, ...]
    implant: bool = False
    sized: bool = False
    anvisa: str = ""
    gtin: str = ""


@dataclass
class Cited:
    qty: int = 0
    refs: list[str] = field(default_factory=list)
    sizes: list[str] = field(default_factory=list)


def catalog() -> dict[str, Entry]:
    return {
        d.code: Entry(d.code, d.description, d.words, d.implant, d.sized, d.anvisa, d.gtin)
        for d in knowledge.opme_catalog()
    }


def search(query: str, limit: int = 15) -> list[dict]:
    """Autocomplete do catálogo por código, descrição ou palavra dita (sem acento nem maiúsculas)."""
    q = norm(query)
    out = []
    for e in catalog().values():
        hay = norm(f"{e.code} {e.description} {' '.join(e.words)}")
        if not q or q in hay:
            out.append(
                {
                    "codigo": e.code,
                    "descricao": e.description,
                    "implantavel": e.implant,
                    "registro_anvisa": e.anvisa or None,
                    "gtin": e.gtin or None,
                }
            )
    return out[:limit]


def by_gtin(gtin: str | None) -> Entry | None:
    if not gtin:
        return None
    padded = gtin.zfill(14)
    return next((e for e in catalog().values() if e.gtin and e.gtin.zfill(14) == padded), None)


def entries_for(authorized: list[dict], consumed: list[dict]) -> dict[str, Entry]:
    """Catálogo + itens da guia que não estão nele + itens lidos na etiqueta sem catálogo nem guia."""
    out = catalog()
    for a in authorized:
        code = a["codigo"]
        if code in out:
            continue
        description = a.get("descricao") or code
        words = tuple(dict.fromkeys(norm(w) for w in [*(a.get("palavras") or []), description] if norm(w)))
        out[code] = Entry(code, description, words, bool(a.get("implantavel", True)), False, "", a.get("gtin") or "")
    for c in consumed:
        code = c["codigo"]
        if code not in out:
            out[code] = Entry(code, c.get("descricao") or code, (), True, False, c.get("registro_anvisa") or "", "")
    return out


# ---------- guia autorizada ----------
class GuiaError(ValueError):
    pass


@dataclass
class Guia:
    itens: list[dict]
    numero: str | None = None
    senha: str | None = None
    origem: str = "manual"


def normalize_items(items: list[dict]) -> list[dict]:
    """Valida e junta itens repetidos (soma as quantidades). Descrição vem do catálogo quando o código é conhecido."""
    known = catalog()
    merged: dict[str, dict] = {}
    for raw in items:
        code = str(raw.get("codigo") or "").strip()
        if not code:
            raise GuiaError("Item da guia sem código")
        try:
            qty = int(raw.get("quantidade", 1))
        except (TypeError, ValueError) as exc:
            raise GuiaError(f"Quantidade inválida para {code}") from exc
        if not 1 <= qty <= MAX_QTY:
            raise GuiaError(f"Quantidade de {code} deve estar entre 1 e {MAX_QTY}")
        item = merged.setdefault(code, {"codigo": code, "quantidade": 0})
        item["quantidade"] += qty
        if code in known:
            item["descricao"] = known[code].description
        else:
            description = str(raw.get("descricao") or "").strip()
            if not description:
                raise GuiaError(f"Item {code} fora do catálogo precisa de descrição")
            item["descricao"] = description
            words = [str(w).strip() for w in raw.get("palavras") or [] if str(w).strip()]
            if words:
                item["palavras"] = words
            if raw.get("gtin"):
                item["gtin"] = str(raw["gtin"])
    if not merged:
        raise GuiaError("A guia não tem nenhum item de OPME")
    return list(merged.values())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def from_claim_response(data: dict) -> Guia:
    """FHIR ClaimResponse (ou Bundle com um): materiais autorizados em `addItem` (código, descrição, quantidade)."""
    if data.get("resourceType") == "Bundle":
        found = [
            e["resource"]
            for e in data.get("entry", [])
            if (e.get("resource") or {}).get("resourceType") == "ClaimResponse"
        ]
        if not found:
            raise GuiaError("O Bundle não tem ClaimResponse")
        data = found[0]
    if data.get("resourceType") != "ClaimResponse":
        raise GuiaError("Esperado um recurso FHIR ClaimResponse")
    if data.get("outcome") == "error":
        raise GuiaError("ClaimResponse com outcome=error: a guia não foi autorizada")
    items = []
    for add in data.get("addItem", []):
        denied = any(
            c.get("code") in ("denied", "denial")
            for adj in add.get("adjudication", [])
            for c in (adj.get("category") or {}).get("coding", [])
        )
        if denied:
            continue
        pos = add.get("productOrService") or {}
        coding = (pos.get("coding") or [{}])[0]
        code = coding.get("code")
        if not code:
            continue
        qty = (add.get("quantity") or {}).get("value", 1)
        items.append(
            {"codigo": code, "quantidade": int(qty), "descricao": coding.get("display") or pos.get("text") or code}
        )
    if not items:
        raise GuiaError("O ClaimResponse não tem itens autorizados (addItem)")
    ident = (data.get("identifier") or [{}])[0].get("value")
    return Guia(normalize_items(items), numero=data.get("preAuthRef") or ident, origem="claimresponse")


TISS_CODE = ("codigoproduto", "codigoprocedimento", "codigodespesa", "codigo")
TISS_DESC = ("descricaoproduto", "descricaoprocedimento", "descricao", "descricaodespesa")
TISS_QTY = ("quantidadeautorizada", "quantidadesolicitada", "quantidadeexecutada", "quantidade")
TISS_GUIDE = ("numeroguiaoperadora", "numeroguiaprestador", "numeroguia", "numeroguiasolicitacaointernacao")
TISS_PASSWORD = ("senha", "senhaautorizacao")


def from_tiss_xml(xml: str) -> Guia:
    """XML de guia TISS (solicitação/autorização de OPME).

    Leitura tolerante e independente do namespace/versão: cada elemento com um filho de código (`codigoProduto`,
    `codigoProcedimento`, `codigoDespesa`) e um de quantidade (`quantidadeAutorizada` > `quantidadeSolicitada` >
    `quantidadeExecutada` > `quantidade`) é um item. NÃO valida contra o XSD da ANS: a operadora e a versão do
    padrão definem o leiaute exato, e este leitor deve ser ajustado a ele antes de uso real.
    """
    if len(xml) > MAX_XML:
        raise GuiaError("XML grande demais")
    if re.search(r"<!(?:DOCTYPE|ENTITY)", xml, re.IGNORECASE):
        raise GuiaError("XML com DOCTYPE/ENTITY não é aceito")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise GuiaError(f"XML inválido: {exc}") from exc

    def child(el: ET.Element, names: tuple[str, ...]) -> str | None:
        found = {_local(c.tag).lower(): (c.text or "").strip() for c in el if (c.text or "").strip()}
        return next((found[n] for n in names if n in found), None)

    items, number, password = [], None, None
    for el in root.iter():
        number = number or child(el, TISS_GUIDE)
        password = password or child(el, TISS_PASSWORD)
        code, qty = child(el, TISS_CODE), child(el, TISS_QTY)
        if code and qty:
            try:
                n = round(float(qty.replace(",", ".")))
            except ValueError as exc:
                raise GuiaError(f"Quantidade inválida no XML: {qty!r}") from exc
            items.append({"codigo": code, "quantidade": n, "descricao": child(el, TISS_DESC) or code})
    if not items:
        raise GuiaError("Nenhum item de OPME encontrado no XML da guia")
    return Guia(normalize_items(items), numero=number, senha=password, origem="tiss")


# ---------- narrado ----------
def _quantity_before(text: str, idx: int) -> int:
    for tok in reversed(text[:idx].split()[-2:]):
        if tok.isdigit():
            return int(tok)
        if tok in NUMBERS:
            return NUMBERS[tok]
    return 1


def extract_size(tail: str) -> str | None:
    """Tamanho dito logo depois do material, na mesma oração ("parafuso 3,5 por 40" → "3,5 × 40 mm")."""
    tail = SIZE_SPLIT.split(tail, maxsplit=1)[0][:50]
    if m := SIZE_DIM.search(tail):
        return f"{m.group(1)} × {m.group(2)} mm".replace(".", ",")
    if m := SIZE_LABEL.search(tail):
        return f"tamanho {m.group(2).upper()}"
    if m := SIZE_MM.search(tail):
        return f"{m.group(1)} mm".replace(".", ",")
    return None


def narrated(segments: list[Segment], entries: dict[str, Entry]) -> dict[str, Cited]:
    """O que o ditado cita de cada material: quantidade, falas e tamanhos.

    Uma citação por fala; citações repetidas na mesma fala só somam quando trazem tamanhos diferentes
    ("parafuso 3,5 por 40 e parafuso 3,5 por 36"). Citação negada ("sem dreno tubular") não conta.
    """
    patterns = {
        code: re.compile("|".join(re.escape(w) for w in sorted(e.words, key=len, reverse=True)))
        for code, e in entries.items()
        if e.words
    }
    out: dict[str, Cited] = {}
    for s in segments:
        seen: dict[str, list[str]] = {}
        for _raw, clause in clauses(affirmative(s.texto)):
            for code, pattern in patterns.items():
                entry = entries[code]
                if negated(clause, entry.words):
                    continue
                for m in pattern.finditer(clause):
                    size = extract_size(clause[m.end() :]) if entry.sized else None
                    taken = seen.setdefault(code, [])
                    if taken and (size is None or size in taken):
                        continue
                    taken.append(size or "")
                    cited = out.setdefault(code, Cited())
                    cited.qty += _quantity_before(clause, m.start())
                    if fala_ref(s.n) not in cited.refs:
                        cited.refs.append(fala_ref(s.n))
                    if size and size not in cited.sizes:
                        cited.sizes.append(size)
    return out


# ---------- consumo (etiquetas) ----------
def scan(label: str, today: date, *, quantity: int = 1, code: str | None = None, authorized: list[dict] | None = None):
    """Etiqueta GS1 → item de consumo (ainda sem id/quem/quando). Código: o informado, o do catálogo ou o da guia."""
    et = gs1.parse(label, today)
    known = catalog()
    entry = known.get(code) if code else by_gtin(et.gtin)
    if code and entry is None:
        entry = next((e for e in entries_for(authorized or [], []).values() if e.code == code), None)
    if entry is None and et.gtin:
        for a in authorized or []:
            if a.get("gtin") and str(a["gtin"]).zfill(14) == et.gtin.zfill(14):
                entry = entries_for([a], []).get(a["codigo"])
                break
    if entry is None:
        description = f"Produto fora do catálogo (GTIN {et.gtin or 'não lido'})"
        entry = Entry(f"{GTIN_CODE_PREFIX}{et.gtin or 'sem-gtin'}", description, (), True)
    item = {
        "codigo": entry.code,
        "descricao": entry.description,
        "gtin": et.gtin,
        "lote": et.lote,
        "validade": et.validade,
        "serie": et.serie,
        "quantidade": quantity,
        "registro_anvisa": entry.anvisa or None,
        "etiqueta": et.bruto,
        "origem": "etiqueta",
    }
    return item, et


def local_today() -> date:
    return clock.now().astimezone(ZoneInfo(get_settings().timezone)).date()


def expired(validade: str | None, today: date) -> bool:
    return bool(validade) and date.fromisoformat(validade) < today


def br_date(iso: str | None) -> str:
    return date.fromisoformat(iso).strftime("%d/%m/%Y") if iso else "sem validade"


# ---------- conciliação ----------
def line_status(a: int, n: int, c: int, implant: bool, tracking: bool, vencido: bool) -> str:
    """Status geral da linha (o pior): vencido > sem guia > quantidade > falta de uma das vias."""
    if vencido:
        return "validade_vencida"
    if a == 0:
        return "nao_autorizado"
    if n == 0 and c == 0:
        return "nao_citado"
    if (n and n != a) or (c and c != a):
        return "quantidade_divergente"
    if c and not n:
        return "sem_fala"
    if tracking and implant and n and not c:
        return "sem_etiqueta"
    return "conferido"


def vias(a: int, n: int, c: int, implant: bool, tracking: bool) -> tuple[ViaStatus, ViaStatus, ViaStatus]:
    """(fala, guia, etiqueta) de uma linha: ok, divergente, ausente, não registrado ou não se aplica."""
    present = n or c
    fala: ViaStatus = "ausente" if n == 0 else ("ok" if n == a else "divergente")
    guia: ViaStatus = "ausente" if a == 0 else ("ok" if (not present or {n, c} - {0} <= {a}) else "divergente")
    if not tracking:
        label: ViaStatus = "nao_registrado"
    elif c == 0:
        label = "ausente" if implant and (n or a) else "nao_aplica"
    else:
        label = "ok" if c == a else "divergente"
    return fala, guia, label


def issues(line: OpmeLine, tracking: bool) -> list[tuple[str, str, str, list[str]]]:
    """Problemas da linha: (tipo = status, texto, severidade, refs). Base das pendências e das regras anti-glosa."""
    d, refs = line.descricao, line.refs
    out: list[tuple[str, str, str, list[str]]] = []
    for lote in line.lotes:
        if lote.vencido:
            out.append(
                (
                    "validade_vencida",
                    f"Validade vencida: {d}, lote {lote.lote or '—'}, venceu em {br_date(lote.validade)}. Não "
                    "implantar nem faturar; trocar o item e registrar a ocorrência (tecnovigilância).",
                    "critico",
                    refs,
                )
            )
    if line.justificativa:
        return out  # o cirurgião justificou na revisão: a divergência segue como informação
    a, n, c = line.autorizado, line.citado, line.consumido
    where = "citado no ditado" if n else "lido na etiqueta"
    if line.status == "nao_autorizado":
        out.append(
            (
                "nao_autorizado",
                f"{d} {where} sem autorização na guia: risco de glosa — anexar justificativa ou solicitar "
                "autorização complementar.",
                "critico",
                refs,
            )
        )
    elif line.status == "nao_citado":
        out.append(
            ("nao_citado", f"{d} autorizado mas não citado no relatório: confirmar se foi usado.", "atencao", [])
        )
    elif line.status == "quantidade_divergente":
        if n and n != a:
            out.append(
                (
                    "quantidade_divergente",
                    f"{d}: {n} citado(s) × {a} autorizado(s). Conferir a quantidade.",
                    "atencao",
                    refs,
                )
            )
        if c and c != a:
            out.append(
                (
                    "quantidade_divergente",
                    f"{d}: {c} lido(s) na etiqueta × {a} autorizado(s). Conferir a quantidade.",
                    "atencao",
                    [],
                )
            )
    elif line.status == "sem_etiqueta":
        out.append(
            (
                "sem_etiqueta",
                f"{d} citado no ditado sem etiqueta lida: ler a etiqueta (lote e série) do implante.",
                "atencao",
                refs,
            )
        )
    elif line.status == "sem_fala":
        out.append(
            ("sem_fala", f"{d} lido na etiqueta mas não citado no ditado: citar o uso no relatório.", "atencao", [])
        )
    return out


def line_highlights(line: OpmeLine, tracking: bool) -> list[Highlight]:
    """Pendências da linha de OPME (recalculadas também na revisão, quando o cirurgião edita)."""
    return [
        Highlight(texto=text, severidade=severity, refs=refs, categoria="opme")  # type: ignore[arg-type]
        for _kind, text, severity, refs in issues(line, tracking)
    ]


def reconcile(
    segments: list[Segment], authorized: list[dict], consumed: list[dict], today: date
) -> tuple[list[OpmeLine], list[Highlight]]:
    entries = entries_for(authorized, consumed)
    cited = narrated(segments, entries)
    auth = {a["codigo"]: int(a.get("quantidade", 1)) for a in authorized}
    used: dict[str, list[dict]] = {}
    for c in consumed:
        used.setdefault(c["codigo"], []).append(c)
    tracking = bool(consumed)
    lines: list[OpmeLine] = []
    pending: list[Highlight] = []
    for code in list(dict.fromkeys([*auth, *cited, *used])):
        entry = entries[code]
        n, c_ = cited.get(code, Cited()), used.get(code, [])
        qty_used = sum(int(x.get("quantidade", 1)) for x in c_)
        lotes = [
            OpmeLote(
                id=x["id"],
                gtin=x.get("gtin"),
                lote=x.get("lote"),
                validade=x.get("validade"),
                serie=x.get("serie"),
                quantidade=int(x.get("quantidade", 1)),
                vencido=expired(x.get("validade"), today),
                registro_anvisa=x.get("registro_anvisa"),
                lido_em=x.get("lido_em"),
                por=x.get("por"),
            )
            for x in c_
        ]
        a = auth.get(code, 0)
        status = line_status(a, n.qty, qty_used, entry.implant, tracking, any(x.vencido for x in lotes))
        fala, guia, etiqueta = vias(a, n.qty, qty_used, entry.implant, tracking)
        line = OpmeLine(
            codigo=code,
            descricao=entry.description,
            autorizado=a,
            citado=n.qty,
            consumido=qty_used,
            status=status,  # type: ignore[arg-type]
            refs=n.refs,
            status_fala=fala,
            status_guia=guia,
            status_etiqueta=etiqueta,
            implantavel=entry.implant,
            tamanhos=n.sizes,
            lotes=lotes,
            registro_anvisa=entry.anvisa or None,
        )
        lines.append(line)
        pending += line_highlights(line, tracking)
    return lines, pending


# ---------- revisão ----------
def recompute(lines: list[OpmeLine]) -> list[OpmeLine]:
    """Após a edição na revisão, status e vias saem dos números (nunca do que o cliente mandou)."""
    tracking = any(line.consumido or line.lotes for line in lines)
    out = []
    for line in lines:
        status = line_status(
            line.autorizado,
            line.citado,
            line.consumido,
            line.implantavel,
            tracking,
            any(lote.vencido for lote in line.lotes),
        )
        fala, guia, etiqueta = vias(line.autorizado, line.citado, line.consumido, line.implantavel, tracking)
        out.append(
            line.model_copy(
                update={"status": status, "status_fala": fala, "status_guia": guia, "status_etiqueta": etiqueta}
            )
        )
    return out


def review_lines(previous: list[OpmeLine], edited: list[dict]) -> tuple[list[OpmeLine], list[dict]]:
    """Aplica a edição do cirurgião: só `citado`, `justificativa` e a retirada/inclusão de linhas.

    Autorizado, consumido e lotes vêm do servidor (guia e etiquetas), nunca do cliente. Linha nova só com código do
    catálogo ou da guia. Devolve as linhas e a lista de mudanças (para o audit).
    """
    known = {line.codigo: line for line in previous}
    cat = catalog()
    changes: list[dict] = []
    out: list[OpmeLine] = []
    seen: set[str] = set()
    for raw in edited:
        code = raw.get("codigo")
        if not code or code in seen:
            continue
        seen.add(code)
        base = known.get(code)
        try:
            citado = max(0, min(MAX_QTY, int(raw.get("citado", base.citado if base else 0))))
        except (TypeError, ValueError):
            citado = base.citado if base else 0
        just = (raw.get("justificativa") or "").strip()[:500] or None
        if base is None:
            if code not in cat:
                continue
            e = cat[code]
            base = OpmeLine(
                codigo=code,
                descricao=e.description,
                autorizado=0,
                citado=0,
                status="nao_autorizado",
                refs=[],
                implantavel=e.implant,
                registro_anvisa=e.anvisa or None,
            )
            changes.append({"codigo": code, "acao": "incluido", "citado": citado})
        elif citado != base.citado or just != base.justificativa:
            changes.append(
                {"codigo": code, "acao": "editado", "citado": [base.citado, citado], "justificativa": bool(just)}
            )
        out.append(
            base.model_copy(
                update={
                    "citado": citado,
                    "justificativa": just,
                    "editado": base.editado or citado != base.citado or just != base.justificativa,
                }
            )
        )
    for code in known.keys() - seen:
        changes.append({"codigo": code, "acao": "retirado"})
    return recompute(out), changes


def review_highlights(lines: list[OpmeLine]) -> list[Highlight]:
    tracking = any(line.consumido or line.lotes for line in lines)
    out: list[Highlight] = []
    for line in lines:
        out += line_highlights(line, tracking)
        if line.justificativa and line.status not in ("conferido",):
            out.append(
                Highlight(
                    texto=f"{line.descricao}: divergência justificada pelo cirurgião — {line.justificativa}",
                    severidade="info",
                    refs=line.refs,
                    categoria="opme",
                )
            )
    return out
