"""Nota em PDF (#34), sem dependência: texto em Helvetica (WinAnsi) com quebra de linha e paginação em A4.

O PDF sai do mesmo texto revisado que vai ao prontuário, então não existe versão "só do PDF" que possa divergir.
Cada linha é gravada como string hexadecimal, o que dispensa escapar parênteses e barras.
"""

from __future__ import annotations

import textwrap
import zlib

PAGE_W, PAGE_H = 595, 842
MARGIN = 56
FONT = 10
LEAD = 14
CHARS = 92  # Helvetica 10 pt em A4 com margem de 56 pt
LINES_PER_PAGE = (PAGE_H - 2 * MARGIN) // LEAD


def _enc(text: str) -> bytes:
    return text.encode("cp1252", errors="replace")


def _wrap(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines() or [""]:
        if not raw.strip():
            lines.append("")
            continue
        indent = len(raw) - len(raw.lstrip())
        lines += textwrap.wrap(raw, CHARS, subsequent_indent=" " * min(indent + 2, 8), replace_whitespace=False) or [""]
    return lines


def _page_stream(lines: list[str], rodape: str, numero: int, total: int) -> bytes:
    ops = [f"BT /F1 {FONT} Tf {LEAD} TL {MARGIN} {PAGE_H - MARGIN} Td"]
    for line in lines:
        ops.append(f"<{_enc(line).hex()}> Tj T*")
    ops.append("ET")
    foot = f"{rodape}  ·  página {numero} de {total}"
    ops.append(f"BT /F1 8 Tf {MARGIN} {MARGIN - 24} Td <{_enc(foot).hex()}> Tj ET")
    return "\n".join(ops).encode("ascii")


def render(titulo: str, texto: str, rodape: str = "HousIA") -> bytes:
    """Monta o PDF (1.4) com o título nos metadados e o texto paginado."""
    lines = _wrap(texto)
    pages = [lines[i : i + LINES_PER_PAGE] for i in range(0, len(lines), LINES_PER_PAGE)] or [[]]
    objects: list[bytes] = []  # índice 0 = objeto 1

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # preenchido depois
    tree = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    info = add(b"<< /Title <" + (b"\xfe\xff" + titulo.encode("utf-16-be")).hex().encode() + b"> /Producer (HousIA) >>")
    kids = []
    for n, page_lines in enumerate(pages, start=1):
        data = zlib.compress(_page_stream(page_lines, rodape, n, len(pages)))
        content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream")
        kids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 %d 0 R >> >> "
                b"/Contents %d 0 R >>" % (tree, PAGE_W, PAGE_H, font, content)
            )
        )
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % tree
    objects[tree - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog,
        info,
        xref,
    )
    return bytes(out)
