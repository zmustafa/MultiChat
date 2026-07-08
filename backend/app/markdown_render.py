"""Render Markdown text into real Word (python-docx) and PDF (reportlab) elements.

LLM answers are Markdown. Exporting them by dumping the raw string leaves literal
``**bold**``, ``# heading``, ``- list`` and fenced code markers in the Word/PDF output.
This module parses the common Markdown constructs LLMs actually emit — headings, bold/
italic/inline-code, links, bullet/numbered lists, fenced code blocks, block quotes,
horizontal rules and pipe tables — into a small list of block tokens, then renders those
tokens with proper document formatting for each backend.
"""
from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# A block token is a tuple whose first element names its kind:
#   ("h", level:int, text:str)
#   ("p", text:str)
#   ("ul", items:list[str])       ("ol", items:list[str])
#   ("code", text:str)
#   ("quote", text:str)
#   ("hr",)
#   ("table", cols:list[str], rows:list[list[str]])
Block = tuple

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^(\*\s*){3,}$|^(-\s*){3,}$|^(_\s*){3,}$")
_UL_RE = re.compile(r"^\s*[-*+]\s+")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")


def _is_block_start(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if s.startswith("```") or s.startswith("~~~"):
        return True
    if _HEADING_RE.match(s):
        return True
    if _UL_RE.match(line) or _OL_RE.match(line):
        return True
    if s.startswith(">"):
        return True
    if _HR_RE.match(s):
        return True
    return False


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_blocks(md: str) -> list[Block]:
    lines = (md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            i += 1
            code_lines: list[str] = []
            while i < n and not lines[i].strip().startswith(fence):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            blocks.append(("code", "\n".join(code_lines)))
            continue

        if not stripped:
            i += 1
            continue

        m = _HEADING_RE.match(stripped)
        if m:
            blocks.append(("h", len(m.group(1)), m.group(2).strip()))
            i += 1
            continue

        if _HR_RE.match(stripped):
            blocks.append(("hr",))
            i += 1
            continue

        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append(("quote", "\n".join(quote_lines).strip()))
            continue

        # Pipe table: a header row followed by a |---|---| separator row
        if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            header = _split_row(line)
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1
            blocks.append(("table", header, rows))
            continue

        if _UL_RE.match(line):
            items: list[str] = []
            while i < n and _UL_RE.match(lines[i]):
                items.append(re.sub(r"^\s*[-*+]\s+", "", lines[i]).strip())
                i += 1
            blocks.append(("ul", items))
            continue

        if _OL_RE.match(line):
            items = []
            while i < n and _OL_RE.match(lines[i]):
                items.append(re.sub(r"^\s*\d+[.)]\s+", "", lines[i]).strip())
                i += 1
            blocks.append(("ol", items))
            continue

        # Paragraph: gather soft-wrapped lines until a blank line or a new block.
        para_lines = [stripped]
        i += 1
        while i < n and lines[i].strip() and not _is_block_start(lines[i]):
            para_lines.append(lines[i].strip())
            i += 1
        blocks.append(("p", " ".join(para_lines)))

    return blocks


# ---------------------------------------------------------------------------
# Inline parsing (bold / italic / code / links)
# ---------------------------------------------------------------------------

_INLINE_RE = re.compile(
    r"`([^`]+)`"                      # 1 code
    r"|\*\*([^*]+)\*\*"              # 2 bold
    r"|__([^_]+)__"                  # 3 bold
    r"|\*([^*]+)\*"                  # 4 italic
    r"|(?<![A-Za-z0-9])_([^_]+)_(?![A-Za-z0-9])"  # 5 italic
    r"|\[([^\]]+)\]\(([^)\s]+)\)"    # 6 link text, 7 href
)


def _inline_spans(text: str) -> list[tuple[str, set[str], str | None]]:
    """Split inline Markdown into (text, styles, href) spans."""
    spans: list[tuple[str, set[str], str | None]] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            spans.append((text[pos:m.start()], set(), None))
        if m.group(1) is not None:
            spans.append((m.group(1), {"code"}, None))
        elif m.group(2) is not None:
            spans.append((m.group(2), {"bold"}, None))
        elif m.group(3) is not None:
            spans.append((m.group(3), {"bold"}, None))
        elif m.group(4) is not None:
            spans.append((m.group(4), {"italic"}, None))
        elif m.group(5) is not None:
            spans.append((m.group(5), {"italic"}, None))
        elif m.group(6) is not None:
            spans.append((m.group(6), {"link"}, m.group(7)))
        pos = m.end()
    if pos < len(text):
        spans.append((text[pos:], set(), None))
    return spans or [(text, set(), None)]


def _strip_inline(text: str) -> str:
    return "".join(s[0] for s in _inline_spans(text))


# ---------------------------------------------------------------------------
# DOCX rendering
# ---------------------------------------------------------------------------


def _add_inline_docx(paragraph: Any, text: str) -> None:
    from docx.shared import Pt, RGBColor

    for content, styles, href in _inline_spans(text):
        if not content:
            continue
        run = paragraph.add_run(content)
        if "bold" in styles:
            run.bold = True
        if "italic" in styles:
            run.italic = True
        if "code" in styles:
            run.font.name = "Consolas"
            run.font.size = Pt(9.5)
        if href or "link" in styles:
            run.font.color.rgb = RGBColor(0x4F, 0x46, 0xE5)
            run.underline = True


def render_markdown_docx(doc: Any, md: str, base_level: int = 2) -> None:
    """Append Markdown ``md`` to a python-docx ``doc`` as formatted elements.

    ``base_level`` is the docx heading level that a Markdown ``#`` maps to, so content
    headings nest below the surrounding section headings.
    """
    from docx.shared import Pt, RGBColor

    for block in parse_blocks(md):
        kind = block[0]
        if kind == "h":
            level = min(base_level + block[1] - 1, 9)
            hp = doc.add_heading("", level=level)
            _add_inline_docx(hp, block[2])
        elif kind == "p":
            _add_inline_docx(doc.add_paragraph(), block[1])
        elif kind == "ul":
            for it in block[1]:
                _add_inline_docx(doc.add_paragraph(style="List Bullet"), it)
        elif kind == "ol":
            for it in block[1]:
                _add_inline_docx(doc.add_paragraph(style="List Number"), it)
        elif kind == "code":
            for cl in block[1].split("\n"):
                p = doc.add_paragraph()
                run = p.add_run(cl or " ")
                run.font.name = "Consolas"
                run.font.size = Pt(9)
                run.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
        elif kind == "quote":
            try:
                p = doc.add_paragraph(style="Quote")
            except Exception:  # noqa: BLE001 — style not in template
                p = doc.add_paragraph()
            _add_inline_docx(p, block[1])
        elif kind == "hr":
            doc.add_paragraph("─" * 30)
        elif kind == "table":
            cols, rows = block[1], block[2]
            if not cols:
                continue
            t = doc.add_table(rows=1, cols=len(cols))
            try:
                t.style = "Light Grid Accent 1"
            except Exception:  # noqa: BLE001
                pass
            for c_i, c in enumerate(cols):
                t.rows[0].cells[c_i].text = _strip_inline(str(c))
            for r in rows:
                cells = t.add_row().cells
                for c_i in range(len(cols)):
                    cells[c_i].text = _strip_inline(str(r[c_i])) if c_i < len(r) else ""


# ---------------------------------------------------------------------------
# PDF rendering (reportlab)
# ---------------------------------------------------------------------------


def _inline_pdf(text: str) -> str:
    """Convert inline Markdown to reportlab's mini-HTML markup (fully escaped)."""
    out: list[str] = []
    for content, styles, href in _inline_spans(text):
        seg = escape(content)
        if "code" in styles:
            seg = f'<font face="Courier">{seg}</font>'
        if "bold" in styles:
            seg = f"<b>{seg}</b>"
        if "italic" in styles:
            seg = f"<i>{seg}</i>"
        if href:
            safe_href = escape(href, {'"': "&quot;"})
            seg = f'<link href="{safe_href}"><font color="#4F46E5">{seg}</font></link>'
        out.append(seg)
    return "".join(out)


def markdown_pdf_flowables(md: str, body_style: Any) -> list:
    """Return a list of reportlab flowables rendering Markdown ``md``."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        ListFlowable,
        ListItem,
        Paragraph,
        Preformatted,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.platypus.flowables import HRFlowable

    heading_style = ParagraphStyle(
        "MdHeading", parent=body_style, fontName="Helvetica-Bold",
        textColor=colors.HexColor("#1E1B4B"), spaceBefore=8, spaceAfter=4,
    )
    code_style = ParagraphStyle(
        "MdCode", parent=body_style, fontName="Courier", fontSize=8.5, leading=11,
        textColor=colors.HexColor("#111827"), backColor=colors.HexColor("#F3F4F6"),
        borderPadding=6, spaceBefore=4, spaceAfter=6,
    )
    quote_style = ParagraphStyle(
        "MdQuote", parent=body_style, leftIndent=12, textColor=colors.HexColor("#475569"),
        borderPadding=4, spaceBefore=4, spaceAfter=6,
    )

    flow: list = []
    for block in parse_blocks(md):
        kind = block[0]
        if kind == "h":
            size = max(14 - (block[1] - 1) * 1.5, 9.5)
            hs = ParagraphStyle(
                f"MdH{block[1]}", parent=heading_style, fontSize=size, leading=size + 3
            )
            flow.append(Paragraph(_inline_pdf(block[2]), hs))
        elif kind == "p":
            flow.append(Paragraph(_inline_pdf(block[1]), body_style))
        elif kind in ("ul", "ol"):
            items = [
                ListItem(Paragraph(_inline_pdf(it), body_style), leftIndent=18)
                for it in block[1]
            ]
            flow.append(
                ListFlowable(
                    items,
                    bulletType="bullet" if kind == "ul" else "1",
                    start="1" if kind == "ol" else None,
                    leftIndent=12,
                )
            )
        elif kind == "code":
            flow.append(Preformatted(block[1] or " ", code_style))
        elif kind == "quote":
            flow.append(Paragraph(_inline_pdf(block[1]), quote_style))
        elif kind == "hr":
            flow.append(
                HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#CBD5E1"),
                           spaceBefore=6, spaceAfter=6)
            )
        elif kind == "table":
            cols, rows = block[1], block[2]
            if not cols:
                continue
            data = [[Paragraph(_inline_pdf(str(c)), body_style) for c in cols]]
            for r in rows:
                data.append(
                    [
                        Paragraph(_inline_pdf(str(r[c_i])) if c_i < len(r) else "", body_style)
                        for c_i in range(len(cols))
                    ]
                )
            tbl = Table(data, hAlign="LEFT")
            tbl.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF2FF")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1E1B4B")),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 5),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]
                )
            )
            flow.append(tbl)
            flow.append(Spacer(1, 6))
    return flow
