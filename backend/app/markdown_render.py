"""Render Markdown text into real Word (python-docx) and PDF (reportlab) elements.

LLM answers are Markdown. Exporting them by dumping the raw string leaves literal
``**bold**``, ``# heading``, ``- list`` and fenced code markers in the Word/PDF output.
This module parses the common Markdown constructs LLMs actually emit — headings, bold/
italic/inline-code, links, bullet/numbered lists, fenced code blocks, block quotes,
horizontal rules and pipe tables — into a small list of block tokens, then renders those
tokens with proper document formatting for each backend.
"""
from __future__ import annotations

import io
import itertools
import os
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from typing import Any
from xml.sax.saxutils import escape

from sqlalchemy.orm import Session as DbSession

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# A block token is a tuple whose first element names its kind:
#   ("h", level:int, text:str)
#   ("p", text:str)
#   ("ul", items:list[str], children:list[list[Block]])
#   ("ol", items:list[str], start:int, children:list[list[Block]])
#   (children[k] holds the nested blocks — sub-lists, code, paragraphs — of items[k])
#   ("code", text:str, lang:str)
#   ("quote", blocks:list[Block])
#   ("hr",)
#   ("table", cols:list[str], rows:list[list[str]])
#   ("image", alt:str, src:str)
Block = tuple

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^(\*\s*){3,}$|^(-\s*){3,}$|^(_\s*){3,}$")
_UL_RE = re.compile(r"^\s*[-*+]\s+")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+")
_LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")
_IMAGE_ONLY_RE = re.compile(r'^!\[([^\]]*)\]\(\s*(\S+?)(?:\s+"[^"]*")?\s*\)$')


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


def _indent_of(line: str) -> int:
    expanded = line.replace("\t", "    ")
    return len(expanded) - len(expanded.lstrip(" "))


def _dedent(lines: list[str]) -> list[str]:
    expanded = [ln.replace("\t", "    ") for ln in lines]
    depth = min((_indent_of(ln) for ln in expanded if ln.strip()), default=0)
    return [ln[depth:] if ln.strip() else "" for ln in expanded]


def _parse_list(lines: list[str], i: int, n: int) -> tuple[Block, int]:
    """Parse a bullet or numbered list starting at ``lines[i]``, including nested content.

    Anything indented past an item's marker belongs to that item — sub-lists, code fences,
    extra paragraphs — and is parsed recursively into the item's child blocks. Blank lines
    between items (a "loose" list) do not end the list.
    """
    first = _LIST_ITEM_RE.match(lines[i])
    base = _indent_of(first.group(1))
    ordered = first.group(2)[0].isdigit()
    # Keep the author's numbering: a list interrupted by a paragraph continues counting
    # instead of restarting at 1.
    start = int(re.match(r"\d+", first.group(2)).group()) if ordered else 1
    items: list[str] = []
    children: list[list[Block]] = []

    def same_level_item(line: str) -> bool:
        m = _LIST_ITEM_RE.match(line)
        return bool(
            m
            and not _HR_RE.match(line.strip())
            and abs(_indent_of(m.group(1)) - base) <= 1
            and m.group(2)[0].isdigit() == ordered
        )

    while i < n and same_level_item(lines[i]):
        m = _LIST_ITEM_RE.match(lines[i])
        marker_indent = _indent_of(m.group(1))
        text = m.group(3).strip()
        body: list[str] = []
        i += 1
        while i < n:
            line = lines[i]
            if not line.strip():
                j = i
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and _indent_of(lines[j]) >= marker_indent + 2:
                    body.extend(lines[i:j])
                    i = j
                    continue
                if j < n and same_level_item(lines[j]):
                    i = j
                break
            if _indent_of(line) >= marker_indent + 2:
                # An indented line that merely continues the item's first paragraph.
                if not body and not _is_block_start(line) and "|" not in line:
                    text = f"{text} {line.strip()}"
                else:
                    body.append(line)
                i += 1
                continue
            if _is_block_start(line) or "|" in line:
                break
            # Lazy continuation: an unindented soft-wrapped line of the item's text.
            if body:
                body.append(" " * (marker_indent + 2) + line.strip())
            else:
                text = f"{text} {line.strip()}"
            i += 1
        items.append(text.strip())
        children.append(parse_blocks("\n".join(_dedent(body))) if body else [])

    block: Block = ("ol", items, start, children) if ordered else ("ul", items, children)
    return block, i


def list_children(block: Block) -> list[list[Block]]:
    """Child blocks of each item of a ``ul``/``ol`` token (empty lists when none)."""
    kids = block[3] if block[0] == "ol" else block[2]
    return list(kids) if kids else [[] for _ in block[1]]


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
            lang = stripped[3:].strip().split()[0].lower() if stripped[3:].strip() else ""
            i += 1
            code_lines: list[str] = []
            while i < n and not lines[i].strip().startswith(fence):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            blocks.append(("code", "\n".join(code_lines), lang))
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

        # A line that is nothing but an image, e.g. a chart produced by a tool.
        m_img = _IMAGE_ONLY_RE.match(stripped)
        if m_img:
            blocks.append(("image", m_img.group(1), m_img.group(2)))
            i += 1
            continue

        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while i < n:
                cur = lines[i]
                if cur.strip().startswith(">"):
                    quote_lines.append(re.sub(r"^\s*>\s?", "", cur))
                    i += 1
                    continue
                # Lazy continuation: an unmarked line that is not itself a new block still
                # belongs to the paragraph the quote was in the middle of.
                if cur.strip() and not _is_block_start(cur):
                    quote_lines.append(cur.strip())
                    i += 1
                    continue
                break
            # A quote is a container, not a string: its headings, lists, tables and code
            # fences are real blocks and must render as such.
            blocks.append(("quote", parse_blocks("\n".join(quote_lines))))
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

        if _LIST_ITEM_RE.match(line):
            block, i = _parse_list(lines, i, n)
            blocks.append(block)
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
    r"`([^`]+)`"                                          # 1 code
    r"|(<[bB][rR]\s*/?>)"                                 # 2 line break
    r"|\*\*(?=\S)(.+?)(?<=\S)\*\*"                        # 3 bold
    r"|__(?=\S)(.+?)(?<=\S)__"                            # 4 bold
    r"|\*(?=[^\s*])([^*]+?)(?<=[^\s*])\*"                 # 5 italic
    r"|(?<![A-Za-z0-9])_(?=\S)([^_]+?)(?<=\S)_(?![A-Za-z0-9])"  # 6 italic
    r"|\[([^\]]+)\]\(([^)\s]+)\)"                         # 7 link text, 8 href
    r"|\$\$([^$]+?)\$\$"                                  # 9 display math
    # Inline math must contain a command, so "$5 and $10" stays plain text.
    r"|\$(?=[^$\n]*\\)(?=\S)([^$\n]+?)(?<=\S)\$"          # 10 inline math
    r"|\\\((.+?)\\\)"                                     # 11 inline math
)

# The TeX that models sprinkle into prose ("File $\rightarrow$ Save"). The chat UI renders
# it with KaTeX; documents approximate it with Unicode. Anything else stays as its source.
_TEX_SYMBOLS = {
    "rightarrow": "\u2192", "to": "\u2192", "longrightarrow": "\u2192",
    "leftarrow": "\u2190", "gets": "\u2190", "longleftarrow": "\u2190",
    "leftrightarrow": "\u2194", "Rightarrow": "\u21d2", "implies": "\u21d2",
    "Longrightarrow": "\u21d2", "Leftarrow": "\u21d0", "Leftrightarrow": "\u21d4",
    "iff": "\u21d4", "uparrow": "\u2191", "downarrow": "\u2193", "mapsto": "\u21a6",
    "times": "\u00d7", "cdot": "\u00b7", "div": "\u00f7", "pm": "\u00b1", "mp": "\u2213",
    "le": "\u2264", "leq": "\u2264", "ge": "\u2265", "geq": "\u2265", "ne": "\u2260",
    "neq": "\u2260", "approx": "\u2248", "sim": "~", "equiv": "\u2261", "infty": "\u221e",
    "ll": "\u226a", "gg": "\u226b", "propto": "\u221d", "partial": "\u2202",
    "checkmark": "\u2713", "degree": "\u00b0", "ldots": "\u2026", "dots": "\u2026",
    "cdots": "\u2026", "sum": "\u2211", "prod": "\u220f", "int": "\u222b",
    "alpha": "\u03b1", "beta": "\u03b2", "gamma": "\u03b3", "delta": "\u03b4",
    "Delta": "\u0394", "epsilon": "\u03b5", "theta": "\u03b8", "lambda": "\u03bb",
    "mu": "\u03bc", "pi": "\u03c0", "sigma": "\u03c3", "Sigma": "\u03a3", "tau": "\u03c4",
    "phi": "\u03c6", "omega": "\u03c9", "Omega": "\u03a9",
    "in": "\u2208", "notin": "\u2209", "subset": "\u2282", "subseteq": "\u2286",
    "cup": "\u222a", "cap": "\u2229", "forall": "\u2200", "exists": "\u2203",
    "neg": "\u00ac", "land": "\u2227", "lor": "\u2228", "emptyset": "\u2205",
    "quad": " ", "qquad": "  ", ",": " ", ";": " ", ":": " ", " ": " ", "!": "",
    "%": "%", "$": "$", "&": "&", "#": "#", "_": "_", "{": "{", "}": "}",
    "left": "", "right": "",
}
_SUPERSCRIPT = str.maketrans("0123456789+-=()n", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b\u207c\u207d\u207e\u207f")


def tex_to_text(expr: str) -> str | None:
    """Approximate a small TeX expression with Unicode, or None if it is beyond that."""
    s = expr.strip()
    s = re.sub(
        r"\\(?:text|mathrm|mathbf|textbf|mathit|textit|mathsf|texttt|operatorname|mbox)"
        r"\s*\{([^{}]*)\}",
        r"\1",
        s,
    )
    s = re.sub(r"\\d?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"\1/\2", s)
    s = re.sub(r"\\sqrt\s*\{([^{}]*)\}", "\u221a(\\1)", s)
    unknown = False

    def symbol(m: re.Match) -> str:
        nonlocal unknown
        name = m.group(1)
        if name in _TEX_SYMBOLS:
            return _TEX_SYMBOLS[name]
        unknown = True
        return m.group(0)

    s = re.sub(r"\\([A-Za-z]+|.)", symbol, s)
    if unknown:
        return None
    s = re.sub(
        r"\^\{([0-9+\-=()n]+)\}|\^([0-9n])",
        lambda m: (m.group(1) or m.group(2)).translate(_SUPERSCRIPT),
        s,
    )
    s = s.replace("{", "").replace("}", "").replace("~", " ")
    return re.sub(r" {2,}", " ", s)


def _inline_spans(
    text: str, styles: frozenset[str] = frozenset(), href: str | None = None
) -> list[tuple[str, set[str], str | None]]:
    """Split inline Markdown into (text, styles, href) spans.

    Emphasis and link text are parsed recursively, so ``**[a link](url)**`` is a bold link
    and ``**run `cmd`**`` is bold text with inline code — not literal brackets or backticks.
    A ``<br>`` becomes a ``("\\n", {"br"}, None)`` span.
    """
    spans: list[tuple[str, set[str], str | None]] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            spans.append((text[pos:m.start()], set(styles), href))
        g = m.group
        if g(1) is not None:
            spans.append((g(1), set(styles) | {"code"}, href))
        elif g(2) is not None:
            spans.append(("\n", set(styles) | {"br"}, href))
        elif g(3) is not None or g(4) is not None:
            spans.extend(_inline_spans(g(3) or g(4), styles | {"bold"}, href))
        elif g(5) is not None or g(6) is not None:
            spans.extend(_inline_spans(g(5) or g(6), styles | {"italic"}, href))
        elif g(7) is not None:
            spans.extend(_inline_spans(g(7), styles | {"link"}, g(8)))
        else:
            math = g(9) or g(10) or g(11) or ""
            spans.append((tex_to_text(math) or m.group(0), set(styles), href))
        pos = m.end()
    if pos < len(text):
        spans.append((text[pos:], set(styles), href))
    return spans or [(text, set(styles), href)]


def _strip_inline(text: str) -> str:
    return "".join(s[0] for s in _inline_spans(text))


# ---------------------------------------------------------------------------
# DOCX rendering
# ---------------------------------------------------------------------------

# Child order of <w:pPr> and <w:trPr> in the OOXML schema. Word rejects a document whose
# elements are out of sequence, so hand-built properties are inserted in schema order.
_PPR_ORDER = (
    "w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:framePr",
    "w:widowControl", "w:numPr", "w:suppressLineNumbers", "w:pBdr", "w:shd", "w:tabs",
    "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap", "w:overflowPunct",
    "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi", "w:adjustRightInd",
    "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents",
    "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
    "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr",
    "w:pPrChange",
)
_TRPR_ORDER = (
    "w:cnfStyle", "w:divId", "w:gridBefore", "w:gridAfter", "w:wBefore", "w:wAfter",
    "w:cantSplit", "w:trHeight", "w:tblHeader", "w:tblCellSpacing", "w:jc", "w:hidden",
)
_TBLPR_ORDER = (
    "w:tblStyle", "w:tblpPr", "w:tblOverlap", "w:bidiVisual", "w:tblStyleRowBandSize",
    "w:tblStyleColBandSize", "w:tblW", "w:jc", "w:tblCellSpacing", "w:tblInd",
    "w:tblBorders", "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
)


def _insert_ordered(parent: Any, child: Any, order: Sequence[str]) -> None:
    from docx.oxml.ns import qn

    tags = [qn(t) for t in order]
    tag = child.tag
    for existing in parent.findall(tag):
        parent.remove(existing)
    later = set(tags[tags.index(tag) + 1:]) if tag in tags else set()
    for idx, existing in enumerate(parent):
        if existing.tag in later:
            parent.insert(idx, child)
            return
    parent.append(child)


def _docx_el(tag: str, **attrs: str) -> Any:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    el = OxmlElement(tag)
    for key, value in attrs.items():
        el.set(qn(f"w:{key}"), value)
    return el


def _docx_pbdr(paragraph: Any, sides: Sequence[str], color: str, size: int = 6, space: int = 1):
    pbdr = _docx_el("w:pBdr")
    for side in sides:
        pbdr.append(_docx_el(f"w:{side}", val="single", sz=str(size), space=str(space), color=color))
    _insert_ordered(paragraph._p.get_or_add_pPr(), pbdr, _PPR_ORDER)


def _docx_hyperlink(paragraph: Any, text: str, url: str) -> Any:
    """Append a clickable external hyperlink run and return it for styling."""
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.text.run import Run

    r_id = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    r = OxmlElement("w:r")
    link.append(r)
    paragraph._p.append(link)
    run = Run(r, paragraph)
    run.text = text
    return run


_EXTERNAL_HREF_RE = re.compile(r"^(https?://|mailto:)", re.I)


def _add_inline_docx(
    paragraph: Any, text: str, *, size: float | None = None, bold: bool = False
) -> None:
    from docx.shared import Pt, RGBColor

    for content, styles, href in _inline_spans(text):
        if "br" in styles:
            paragraph.add_run().add_break()
            continue
        if not content:
            continue
        if href and _EXTERNAL_HREF_RE.match(href):
            run = _docx_hyperlink(paragraph, content, href)
        else:
            run = paragraph.add_run(content)
        if size:
            run.font.size = Pt(size)
        if bold or "bold" in styles:
            run.bold = True
        if "italic" in styles:
            run.italic = True
        if "code" in styles:
            run.font.name = "Consolas"
            run.font.size = Pt((size or 11) - 1.5)
            if not href:
                run.font.color.rgb = RGBColor(0xB9, 0x1C, 0x1C)
        if href or "link" in styles:
            run.font.color.rgb = RGBColor(0x4F, 0x46, 0xE5)
            run.underline = True


def _docx_content_width(doc: Any) -> int:
    section = doc.sections[-1]
    return int(section.page_width - section.left_margin - section.right_margin)


def _docx_new_list_num(doc: Any, style_name: str, start: int) -> int | None:
    """A fresh numbering instance for ``style_name`` that restarts at ``start``.

    Every paragraph styled "List Number" shares one counter, so without this each numbered
    list in a document carries on from where the previous one stopped.
    """
    try:
        style = doc.styles[style_name]
        num_pr = style.element.pPr.numPr
        numbering = doc.part.numbering_part.numbering_definitions._numbering
        abstract_id = numbering.num_having_numId(num_pr.numId.val).abstractNumId.val
        num = numbering.add_num(abstract_id)
        num.add_lvlOverride(ilvl=0).add_startOverride(start)
        return num.numId
    except Exception:  # noqa: BLE001 — a template without numbering keeps the shared counter
        return None


def _docx_apply_num(paragraph: Any, num_id: int) -> None:
    num_pr = paragraph._p.get_or_add_pPr().get_or_add_numPr()
    num_pr.get_or_add_ilvl().val = 0
    num_pr.get_or_add_numId().val = num_id


def render_markdown_docx(
    doc: Any,
    md: str,
    base_level: int = 2,
    diagrams: Sequence[dict] | None = None,
) -> None:
    """Append Markdown ``md`` to a python-docx ``doc`` as formatted elements.

    ``base_level`` is the docx heading level that a Markdown ``#`` maps to, so content
    headings nest below the surrounding section headings.

    ``diagrams`` optionally supplies pre-rendered PNGs for ```mermaid``` fences (the chat UI
    rasterizes what it is already showing). Without one, a fence falls back to its source
    text — readable, but not a diagram.
    """
    import io

    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Emu, Inches

    pending = [dict(d) for d in (diagrams or [])]

    def take_diagram(code: str) -> dict | None:
        key = (code or "").strip()
        for i, d in enumerate(pending):
            if (d.get("code") or "").strip() == key:
                return pending.pop(i)
        return None

    def place_diagram(data: bytes, natural_px: float = 0.0) -> bool:
        # Same sizing rule as the PDF: the diagram's own layout width, scaled up a little
        # for print, capped by the text column and a page's height.
        max_w = _docx_content_width(doc)
        max_h = Inches(8.0)
        width = min(max_w, int(Inches(natural_px * 1.2 / 72))) if natural_px else max_w
        try:
            shape = doc.add_picture(io.BytesIO(data), width=Emu(width))
        except Exception:  # noqa: BLE001 — fall back to the source text
            return False
        if shape.height > max_h:
            shape.width = Emu(int(shape.width * max_h / shape.height))
            shape.height = max_h
        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        return True

    _render_blocks_docx(
        doc, parse_blocks(md), base_level, take_diagram, place_diagram
    )


# The default template's bullet/number styles for list nesting levels 1-3.
_DOCX_LIST_STYLES = {
    "ul": ("List Bullet", "List Bullet 2", "List Bullet 3"),
    "ol": ("List Number", "List Number 2", "List Number 3"),
}
_DOCX_LIST_STEP = 0.25  # inches of indent per list level in those styles


def _render_blocks_docx(
    doc: Any,
    blocks: Sequence[Block],
    base_level: int,
    take_diagram: Callable[[str], dict | None],
    place_diagram: Callable[..., bool],
    indent: float = 0.0,
    depth: int = 0,
) -> None:
    """``indent`` is a block quote's extra left indent; ``depth`` the list nesting level."""
    from docx.shared import Inches, Pt, RGBColor

    # Content inside a list item lines up with the item's text.
    left = indent + _DOCX_LIST_STEP * depth

    def para(style: str | None = None):
        p = doc.add_paragraph(style=style) if style else doc.add_paragraph()
        if left:
            p.paragraph_format.left_indent = Inches(left)
        return p

    for block in blocks:
        kind = block[0]
        if kind == "h":
            level = min(base_level + block[1] - 1, 9)
            hp = doc.add_heading("", level=level)
            if left:
                hp.paragraph_format.left_indent = Inches(left)
            _add_inline_docx(hp, block[2])
        elif kind == "p":
            _add_inline_docx(para(), block[1])
        elif kind in ("ul", "ol"):
            style = _DOCX_LIST_STYLES[kind][min(depth, 2)]
            num_id = (
                _docx_new_list_num(doc, style, block[2] if len(block) > 2 else 1)
                if kind == "ol"
                else None
            )
            for text, kids in zip(block[1], list_children(block), strict=False):
                p = doc.add_paragraph(style=style)
                if num_id is not None:
                    _docx_apply_num(p, num_id)
                if indent or depth > 2:
                    # The list styles only know their own level; a quote (or a 4th level)
                    # needs the indent spelled out, keeping the hanging bullet.
                    fmt = p.paragraph_format
                    fmt.left_indent = Inches(left + _DOCX_LIST_STEP)
                    fmt.first_line_indent = Inches(-_DOCX_LIST_STEP)
                _add_inline_docx(p, text)
                if kids:
                    _render_blocks_docx(
                        doc, kids, base_level, take_diagram, place_diagram, indent, depth + 1
                    )
        elif kind == "code":
            lang = block[2] if len(block) > 2 else ""
            if lang == "mermaid":
                d = take_diagram(block[1])
                if d and d.get("data") and place_diagram(
                    d["data"], float(d.get("width") or 0)
                ):
                    continue
            # One shaded, bordered paragraph with line breaks — not a paragraph per line,
            # which spreads code out with body-text spacing.
            lines = (block[1] or " ").replace("\t", "    ").split("\n")
            p = para()
            fmt = p.paragraph_format
            fmt.space_before, fmt.space_after = Pt(4), Pt(10)
            fmt.line_spacing = 1.0
            fmt.left_indent = Inches(left + 0.08)
            fmt.right_indent = Inches(0.08)
            if len(lines) <= 40:
                fmt.keep_together = True
            _docx_pbdr(p, ("top", "left", "bottom", "right"), "D0D7DE", size=4, space=4)
            _insert_ordered(
                p._p.get_or_add_pPr(),
                _docx_el("w:shd", val="clear", color="auto", fill="F6F8FA"),
                _PPR_ORDER,
            )
            for li, line in enumerate(lines):
                run = p.add_run(line)
                run.font.name = "Consolas"
                run.font.size = Pt(9)
                run.font.color.rgb = RGBColor(0x1F, 0x23, 0x28)
                if li < len(lines) - 1:
                    run.add_break()
        elif kind == "quote":
            # Quoted content keeps its own block structure; the quote only adds an indent.
            _render_blocks_docx(
                doc, block[1], base_level, take_diagram, place_diagram, indent + 0.3, depth
            )
        elif kind == "hr":
            p = para()
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(8)
            _docx_pbdr(p, ("bottom",), "CBD5E1")
        elif kind == "table":
            _render_table_docx(doc, block[1], block[2], left)


def _render_table_docx(doc: Any, cols: list[str], rows: list[list[str]], left: float) -> None:
    from docx.shared import Inches, Pt

    if not cols:
        return
    t = doc.add_table(rows=1, cols=len(cols))
    try:
        t.style = "Light Grid Accent 1"
    except Exception:  # noqa: BLE001
        pass
    if left:
        _insert_ordered(
            t._tbl.tblPr,
            _docx_el("w:tblInd", w=str(int(left * 1440)), type="dxa"),
            _TBLPR_ORDER,
        )

    def fill(cell: Any, text: str, header: bool) -> None:
        # Cells keep their inline formatting — bold, code, links and <br> line breaks.
        p = cell.paragraphs[0]
        p.paragraph_format.space_after = Pt(0)
        _add_inline_docx(p, str(text), size=9.5, bold=header)

    header_row = t.rows[0]
    for c_i, c in enumerate(cols):
        fill(header_row.cells[c_i], c, True)
    # Repeat the header on every page the table spans.
    _insert_ordered(header_row._tr.get_or_add_trPr(), _docx_el("w:tblHeader"), _TRPR_ORDER)
    for r in rows:
        row = t.add_row()
        for c_i in range(len(cols)):
            fill(row.cells[c_i], r[c_i] if c_i < len(r) else "", False)
        # Keep ordinary rows whole; a row taller than a page must be allowed to break.
        if sum(len(str(c)) for c in r) < 1200:
            _insert_ordered(row._tr.get_or_add_trPr(), _docx_el("w:cantSplit"), _TRPR_ORDER)
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(4)
    spacer.paragraph_format.left_indent = Inches(left) if left else None


def docx_page_setup(doc: Any, footer_left: str = "") -> None:
    """US Letter, 0.75in margins and a "context · Page X of Y" footer — matching the PDFs."""
    from docx.shared import Inches

    for section in doc.sections:
        section.page_width, section.page_height = Inches(8.5), Inches(11)
        section.left_margin = section.right_margin = Inches(0.75)
        section.top_margin = Inches(0.7)
        section.bottom_margin = Inches(0.75)
        section.footer_distance = Inches(0.4)
        _docx_footer(section, footer_left)


def _docx_footer(section: Any, footer_left: str) -> None:
    from docx.enum.text import WD_TAB_ALIGNMENT
    from docx.oxml.ns import qn
    from docx.shared import Emu, Pt, RGBColor

    footer = section.footer
    p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    for child in list(p._p):
        if child.tag != qn("w:pPr"):
            p._p.remove(child)
    tabs = p.paragraph_format.tab_stops
    # The template's Footer style brings centre/right stops sized for 1.25in margins; clear
    # them so the page number lands on this section's right margin.
    for inherited in p.style.paragraph_format.tab_stops:
        tabs.add_tab_stop(inherited.position, WD_TAB_ALIGNMENT.CLEAR)
    tabs.add_tab_stop(
        Emu(int(section.page_width - section.left_margin - section.right_margin)),
        WD_TAB_ALIGNMENT.RIGHT,
    )
    _docx_pbdr(p, ("top",), "E2E8F0", size=4, space=4)

    def run(text: str = ""):
        r = p.add_run(text)
        r.font.size = Pt(7.5)
        r.font.color.rgb = RGBColor(0x94, 0xA3, 0xB8)
        return r

    run(footer_left)
    run("\tPage ")
    _docx_field("PAGE", run)
    run(" of ")
    _docx_field("NUMPAGES", run)


def _docx_field(instr: str, make_run: Callable[..., Any]) -> None:
    """Append a Word field (PAGE, NUMPAGES…) that Word computes when it lays out the page."""
    begin = make_run()
    begin._r.append(_docx_el("w:fldChar", fldCharType="begin"))
    code = make_run()
    instr_el = _docx_el("w:instrText")
    instr_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    instr_el.text = f" {instr} "
    code._r.append(instr_el)
    make_run()._r.append(_docx_el("w:fldChar", fldCharType="separate"))
    make_run("1")
    make_run()._r.append(_docx_el("w:fldChar", fldCharType="end"))


# ---------------------------------------------------------------------------
# PDF rendering (reportlab)
# ---------------------------------------------------------------------------

# GitHub-dark palette — the same theme the chat lanes use for fenced code blocks.
CODE_BG = "#0D1117"
CODE_FG = "#E6EDF3"

_FONTS: dict[str, str] | None = None
_GLYPH_OK: Callable[[str], bool] | None = None

# Characters LLM answers use constantly that no PDF base font can draw. Anything not
# listed and not drawable is dropped rather than rendered as a "missing glyph" box.
_CHAR_FALLBACKS = {
    "\u2192": "->", "\u2190": "<-", "\u2191": "^", "\u2193": "v", "\u2194": "<->",
    "\u21d2": "=>", "\u21d0": "<=", "\u21d4": "<=>",
    "\u2713": "[OK]", "\u2714": "[OK]", "\u2705": "[OK]", "\u2611": "[OK]",
    "\u2717": "[X]", "\u2718": "[X]", "\u274c": "[X]", "\u2716": "[X]", "\u274e": "[X]",
    "\u2b55": "( )", "\u2b1c": "[ ]", "\u2795": "+", "\u2796": "-",
    "\u26a0": "[!]", "\u2139": "i", "\u2757": "!", "\u2753": "?",
    "\u2605": "*", "\u2606": "*", "\u2b50": "*", "\u25cf": "\u2022", "\u25cb": "\u2022",
    "\u25aa": "\u2022", "\u25ab": "\u2022", "\u25e6": "\u2022", "\u2023": "\u2022",
    "\u25b6": ">", "\u25c0": "<", "\u25b2": "^", "\u25bc": "v",
    "\u2260": "!=", "\u2248": "~", "\u221e": "inf", "\u2261": "==",
    "\u2265": ">=", "\u2264": "<=", "\u2212": "-", "\u2044": "/",
    "\u2500": "-", "\u2501": "-", "\u2502": "|", "\u2503": "|", "\u2550": "=", "\u2551": "|",
    "\u250c": "+", "\u2510": "+", "\u2514": "+", "\u2518": "+", "\u251c": "+",
    "\u2524": "+", "\u252c": "+", "\u2534": "+", "\u253c": "+",
    "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u200b": "", "\ufe0f": "", "\u2060": "",
}


def _font_dirs() -> list[str]:
    import reportlab

    return [
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/dejavu",
        "/usr/share/fonts/truetype/noto",
        "/usr/share/fonts/noto",
        "/usr/share/fonts/truetype/ancient-scripts",
        "/usr/share/fonts/TTF",
        "/usr/local/share/fonts",
        "/Library/Fonts",
        "/System/Library/Fonts/Supplemental",
        "C:/Windows/Fonts",
        os.path.join(os.path.dirname(reportlab.__file__), "fonts"),
    ]


# Monochrome fonts that draw the symbols and emoji the body font lacks (check marks,
# crosses, warning signs, arrows). The first one found becomes the "symbol" face.
_SYMBOL_FONT_FILES = (
    "seguisym.ttf",                  # Windows: Segoe UI Symbol
    "NotoSansSymbols2-Regular.ttf",
    "Symbola.ttf",
    "DejaVuSans.ttf",
    "Arial Unicode.ttf",             # macOS
)


def pdf_fonts() -> dict[str, str]:
    """Font names to use for PDF body/bold/italic/mono text.

    Prefers DejaVu (broad Unicode coverage) when the host provides it, so arrows, box
    drawing and accented text survive; otherwise falls back to the built-in Helvetica and
    Courier, and :func:`pdf_safe` transliterates whatever the font cannot draw.
    """
    global _FONTS
    if _FONTS is not None:
        return _FONTS

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    fonts = {
        "body": "Helvetica",
        "bold": "Helvetica-Bold",
        "italic": "Helvetica-Oblique",
        "boldItalic": "Helvetica-BoldOblique",
        "mono": "Courier",
        "monoBold": "Courier-Bold",
    }
    dirs = _font_dirs()

    def find(filename: str) -> str | None:
        for d in dirs:
            path = os.path.join(d, filename)
            if os.path.exists(path):
                return path
        return None

    def register_family(prefix: str, files: dict[str, str], keys: Sequence[str]) -> bool:
        paths = {k: find(files[k]) for k in keys}
        if not all(paths.values()):
            return False
        names = {k: f"{prefix}-{k}" for k in keys}
        for k in keys:
            pdfmetrics.registerFont(TTFont(names[k], paths[k]))
        pdfmetrics.registerFontFamily(
            names["body"],
            normal=names["body"],
            bold=names.get("bold", names["body"]),
            italic=names.get("italic", names["body"]),
            boldItalic=names.get("boldItalic", names["body"]),
        )
        fonts.update(names)
        return True

    try:
        register_family(
            "MCSans",
            {
                "body": "DejaVuSans.ttf",
                "bold": "DejaVuSans-Bold.ttf",
                "italic": "DejaVuSans-Oblique.ttf",
                "boldItalic": "DejaVuSans-BoldOblique.ttf",
            },
            ("body", "bold", "italic", "boldItalic"),
        )
    except Exception:  # noqa: BLE001 — a broken font file must not break exporting
        pass
    try:
        mono, mono_bold = find("DejaVuSansMono.ttf"), find("DejaVuSansMono-Bold.ttf")
        if mono and mono_bold:
            pdfmetrics.registerFont(TTFont("MCMono", mono))
            pdfmetrics.registerFont(TTFont("MCMono-Bold", mono_bold))
            pdfmetrics.registerFontFamily(
                "MCMono", normal="MCMono", bold="MCMono-Bold",
                italic="MCMono", boldItalic="MCMono-Bold",
            )
            fonts["mono"], fonts["monoBold"] = "MCMono", "MCMono-Bold"
    except Exception:  # noqa: BLE001
        pass
    try:
        body_path = find("DejaVuSans.ttf") if fonts["body"].startswith("MCSans") else None
        for filename in _SYMBOL_FONT_FILES:
            path = find(filename)
            if path and path != body_path:
                pdfmetrics.registerFont(TTFont("MCSymbol", path))
                pdfmetrics.registerFontFamily(
                    "MCSymbol", normal="MCSymbol", bold="MCSymbol",
                    italic="MCSymbol", boldItalic="MCSymbol",
                )
                fonts["symbol"] = "MCSymbol"
                break
    except Exception:  # noqa: BLE001 — without it, symbols fall back to transliteration
        fonts.pop("symbol", None)

    _FONTS = fonts
    return fonts


_SYMBOL_OK: Callable[[str], bool] | None = None
# Emoji presentation selectors and joiners carry no glyph of their own.
_INVISIBLE = {"\ufe0f", "\ufe0e", "\u200d"}


def _symbol_ok() -> Callable[[str], bool]:
    global _SYMBOL_OK
    if _SYMBOL_OK is not None:
        return _SYMBOL_OK

    from reportlab.pdfbase import pdfmetrics

    table = None
    name = pdf_fonts().get("symbol")
    if name:
        try:
            table = pdfmetrics.getFont(name).face.charToGlyph
        except Exception:  # noqa: BLE001
            table = None
    _SYMBOL_OK = (lambda ch: ord(ch) in table) if table else (lambda ch: False)
    return _SYMBOL_OK


def pdf_markup(text: str) -> str:
    """Escape ``text`` for a reportlab Paragraph, drawing what the body font can't.

    Symbols the body font lacks (✅ ❌ ⚠ →) switch to the symbol font when one is installed;
    anything neither font has is transliterated exactly as :func:`pdf_safe` would.
    """
    if not text:
        return text
    ok, sym = _glyph_ok(), _symbol_ok()
    face = pdf_fonts().get("symbol")
    segments: list[tuple[bool, str]] = []  # (drawn with the symbol font?, text)
    for ch in text:
        if ch in _INVISIBLE:
            continue
        is_sym = not (ch in "\n\t" or ok(ch)) and sym(ch)
        if segments and segments[-1][0] == is_sym:
            segments[-1] = (is_sym, segments[-1][1] + ch)
        else:
            segments.append((is_sym, ch))
    return "".join(
        f'<font face="{face}">{escape(s)}</font>' if is_sym else escape(pdf_safe(s))
        for is_sym, s in segments
    )


def _glyph_ok() -> Callable[[str], bool]:
    global _GLYPH_OK
    if _GLYPH_OK is not None:
        return _GLYPH_OK

    from reportlab.pdfbase import pdfmetrics

    try:
        face = pdfmetrics.getFont(pdf_fonts()["body"]).face
    except Exception:  # noqa: BLE001
        face = None
    table = getattr(face, "charToGlyph", None)
    if table is not None:
        def ok(ch: str) -> bool:
            return ord(ch) in table
    else:
        def ok(ch: str) -> bool:
            try:
                ch.encode("cp1252")
                return True
            except UnicodeEncodeError:
                return False
    _GLYPH_OK = ok
    return ok


def pdf_safe(text: str) -> str:
    """Make ``text`` renderable by the active PDF font.

    Characters the font cannot draw are transliterated (``->`` for an arrow, ``[OK]`` for a
    check mark, accents stripped) or dropped, so the PDF never shows missing-glyph boxes.
    """
    if not text:
        return text
    ok = _glyph_ok()
    if all(ch in "\n\t" or ok(ch) for ch in text):
        return text
    out: list[str] = []
    for ch in text:
        if ch in "\n\t" or ok(ch):
            out.append(ch)
            continue
        repl = _CHAR_FALLBACKS.get(ch)
        if repl is None:
            # "ā" -> "a": keep the base letter of a decomposable character.
            repl = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
            if repl == ch:
                repl = ""
        out.append("".join(c for c in repl if ok(c)))
    return "".join(out)


def _fit_footer_text(text: str, font: str, size: float, max_width: float) -> str:
    """Shorten footer context to ``max_width`` with an ellipsis.

    Footers read "<title> · <model>"; the model is what tells one exported answer from
    another, so the title gives way first and the part after the last " · " is kept.
    """
    from reportlab.pdfbase import pdfmetrics

    def width(s: str) -> float:
        return pdfmetrics.stringWidth(s, font, size)

    if width(text) <= max_width:
        return text
    ellipsis = pdf_safe("\u2026") or "..."
    head, sep, tail = text.rpartition(" \u00b7 ")
    if sep and width(ellipsis + sep + tail) <= max_width * 0.75:
        while head and width(head.rstrip() + ellipsis + sep + tail) > max_width:
            head = head[:-1]
        return head.rstrip() + ellipsis + sep + tail
    while text and width(text.rstrip() + ellipsis) > max_width:
        text = text[:-1]
    return text.rstrip() + ellipsis


def footer_canvas(
    footer_left: str,
    font: str,
    pagesize: tuple[float, float] | None = None,
    margin: float = 54.0,
    attribution: bool = True,
):
    """Canvas subclass stamping a rule, context text, repo link and "Page X of Y".

    The page count is only known once the whole story is laid out, so pages are buffered
    and replayed on save. The centre slot carries a clickable link to the project repo
    (``settings.APP_REPO_URL``); pass ``attribution=False`` to leave it out.
    """
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas as pdf_canvas

    from .config import settings

    page_w, _page_h = pagesize or LETTER
    repo_url = (settings.APP_REPO_URL or "").strip() if attribution else ""
    # Show the bare host/path; the full URL stays behind the link.
    repo_label = pdf_safe(re.sub(r"^https?://", "", repo_url)) if repo_url else ""
    # Pages are replayed from buffered state on save, which rewinds reportlab's annotation
    # counter - every footer link would then be named "Annot.NUMBER1" and the second one
    # would raise "redefining named object". This counter keeps the names unique; the high
    # base keeps it clear of the names used by links inside the document body.
    link_seq = itertools.count(1_000_000)

    class FooterCanvas(pdf_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages: list[dict] = []

        def showPage(self):  # noqa: N802 — reportlab API
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._stamp(total)
                super().showPage()
            super().save()

        def _stamp(self, total: int) -> None:
            self.saveState()
            self.setStrokeColor(HexColor("#E2E8F0"))
            self.setLineWidth(0.5)
            self.line(margin, 44, page_w - margin, 44)
            self.setFont(font, 7.5)
            self.setFillColor(HexColor("#94A3B8"))
            # The left slot is capped to a third of the usable width so a long title
            # can never collide with the centred link.
            usable = page_w - 2 * margin
            self._draw_clipped(pdf_safe(footer_left), margin, 32, usable / 3.0)
            self.drawRightString(page_w - margin, 32, f"Page {self._pageNumber} of {total}")
            if repo_label:
                self._draw_link(page_w / 2.0, 32)
            self.restoreState()

        def _draw_clipped(self, text: str, x: float, y: float, max_width: float) -> None:
            self.drawString(x, y, _fit_footer_text(text, font, 7.5, max_width))

        def _draw_link(self, cx: float, y: float) -> None:
            self.setFillColor(HexColor("#6366F1"))
            self.drawCentredString(cx, y, repo_label)
            width = pdfmetrics.stringWidth(repo_label, font, 7.5)
            self._annotationCount = next(link_seq)
            self.linkURL(repo_url, (cx - width / 2, y - 2, cx + width / 2, y + 8), relative=0)
            self.setFillColor(HexColor("#94A3B8"))

    return FooterCanvas


def _inline_pdf(text: str) -> str:
    """Convert inline Markdown to reportlab's mini-HTML markup (fully escaped)."""
    fonts = pdf_fonts()
    out: list[str] = []
    for content, styles, href in _inline_spans(text):
        if "br" in styles:
            out.append("<br/>")
            continue
        seg = pdf_markup(content)
        if "code" in styles:
            seg = f'<font face="{fonts["mono"]}" color="#B91C1C">{seg}</font>'
        if "bold" in styles:
            seg = f"<b>{seg}</b>"
        if "italic" in styles:
            seg = f"<i>{seg}</i>"
        if href:
            safe_href = escape(href, {'"': "&quot;"})
            seg = f'<link href="{safe_href}"><font color="#4F46E5">{seg}</font></link>'
        out.append(seg)
    return "".join(out)


# ---------------------------------------------------------------------------
# Fenced code blocks: syntax highlighting
# ---------------------------------------------------------------------------

_PALETTE: dict | None = None


def _palette() -> dict:
    """GitHub-dark token colours keyed by Pygments token type."""
    global _PALETTE
    if _PALETTE is None:
        from pygments.token import Token

        _PALETTE = {
            Token.Comment: "#8B949E",
            Token.Keyword: "#FF7B72",
            Token.Keyword.Type: "#FFA657",
            Token.Operator: "#FF7B72",
            Token.Operator.Word: "#FF7B72",
            Token.Name: CODE_FG,
            Token.Name.Builtin: "#79C0FF",
            Token.Name.Builtin.Pseudo: "#79C0FF",
            Token.Name.Function: "#D2A8FF",
            Token.Name.Class: "#FFA657",
            Token.Name.Namespace: "#FFA657",
            Token.Name.Decorator: "#D2A8FF",
            Token.Name.Tag: "#7EE787",
            Token.Name.Attribute: "#79C0FF",
            Token.Name.Variable: "#FFA657",
            Token.Name.Constant: "#79C0FF",
            Token.Literal: "#A5D6FF",
            Token.String: "#A5D6FF",
            Token.String.Escape: "#79C0FF",
            Token.Number: "#79C0FF",
            Token.Generic.Deleted: "#FFA198",
            Token.Generic.Inserted: "#7EE787",
            Token.Generic.Heading: "#79C0FF",
            Token.Generic.Emph: "#E6EDF3",
            Token.Generic.Prompt: "#8B949E",
            Token.Error: "#FFA198",
        }
    return _PALETTE


def _code_spans(code: str, lang: str) -> list[list[tuple[str, str | None]]]:
    """Tokenize ``code`` into one list of ``(text, colour)`` spans per line."""
    try:
        from pygments import lex
        from pygments.lexers import get_lexer_by_name, guess_lexer

        palette = _palette()
        try:
            lexer = get_lexer_by_name(lang, stripnl=False, stripall=False)
        except Exception:  # noqa: BLE001 — unknown/absent language
            lexer = guess_lexer(code, stripnl=False) if code.strip() else None
        if lexer is None:
            raise ValueError("no lexer")

        def colour(ttype) -> str | None:
            t = ttype
            while t is not None:
                if t in palette:
                    return palette[t]
                t = t.parent
            return None

        lines: list[list[tuple[str, str | None]]] = [[]]
        for ttype, value in lex(code, lexer):
            col = colour(ttype)
            parts = value.split("\n")
            for idx, part in enumerate(parts):
                if idx:
                    lines.append([])
                if part:
                    lines[-1].append((part, col))
        while lines and not lines[-1]:
            lines.pop()
        return lines or [[]]
    except Exception:  # noqa: BLE001 — highlighting is best-effort
        return [[(line, None)] for line in code.split("\n")]


def _wrap_code_spans(
    lines: list[list[tuple[str, str | None]]], max_chars: int
) -> list[list[tuple[str, str | None]]]:
    """Hard-wrap highlighted lines so long code never runs off the page."""
    max_chars = max(20, max_chars)
    wrapped: list[list[tuple[str, str | None]]] = []
    for spans in lines:
        plain = "".join(t for t, _ in spans)
        if len(plain) <= max_chars:
            wrapped.append(spans)
            continue
        colours: list[str | None] = []
        for text, col in spans:
            colours.extend([col] * len(text))
        indent = len(plain) - len(plain.lstrip(" "))
        prefix = " " * min(indent + 2, max_chars // 2)
        pos, first = 0, True
        while pos < len(plain):
            avail = max(12, max_chars - (0 if first else len(prefix)))
            chunk = plain[pos : pos + avail]
            if pos + avail < len(plain):
                brk = max(chunk.rfind(" "), chunk.rfind(","), chunk.rfind(";"))
                if brk > avail * 0.55:
                    chunk = chunk[: brk + 1]
            row: list[tuple[str, str | None]] = []
            if not first:
                row.append((prefix, None))
            for offset, ch in enumerate(chunk):
                col = colours[pos + offset]
                if row and row[-1][1] == col and (first or offset):
                    row[-1] = (row[-1][0] + ch, col)
                else:
                    row.append((ch, col))
            wrapped.append(row)
            pos += len(chunk)
            first = False
    return wrapped


def _code_markup(lines: Iterable[list[tuple[str, str | None]]]) -> str:
    rows: list[str] = []
    for spans in lines:
        parts = []
        for text, colour in spans:
            seg = escape(pdf_safe(text))
            parts.append(f'<font color="{colour}">{seg}</font>' if colour else seg)
        rows.append("".join(parts) or " ")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Diagrams and images
# ---------------------------------------------------------------------------

# The height reportlab's _listWrapOn (used by KeepTogether) passes when it measures.
_MEASURE_HEIGHT = 0xFFFFFFF
_MIN_LABEL_PT = 5.0


def _diagram_card(data: bytes, max_width: float, max_height: float, natural_pt: float = 0.0):
    """A bordered image card that shrinks to fit the space left on the page.

    A plain reportlab ``Image`` that does not fit simply jumps to the next page, which for
    a large diagram can leave most of a page blank. This flowable scales down instead —
    but only while the result stays readable, otherwise it moves on as usual.
    """
    from reportlab.lib import colors
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Flowable

    reader = ImageReader(io.BytesIO(data))
    px_w, px_h = reader.getSize()
    if not px_w or not px_h:
        return None
    # Rasterized diagrams arrive at 2-4x, so pixel size alone would be enormous. Prefer the
    # diagram's own layout size (scaled up a little so it reads well in print) when known.
    target = natural_pt * 1.6 if natural_pt else max_width
    width = max(1.0, min(max_width, target))
    height = width * px_h / px_w
    if height > max_height:
        height = max_height
        width = height * px_w / px_h
    # Mermaid lays labels out at 16px. When the diagram's layout size is known, never shrink
    # it so far that they print below ~5pt (a tall diagram squeezed into the bottom of a
    # page); let it start the next page at a readable size instead.
    legible_h = (natural_pt / 0.75) * (_MIN_LABEL_PT / 16.0) * px_h / px_w if natural_pt else 0.0

    class DiagramCard(Flowable):
        pad = 8.0
        # Below this the shrunk diagram stops being legible - take the page break instead.
        min_fit = 200.0

        def __init__(self) -> None:
            super().__init__()
            self.hAlign = "CENTER"
            self.spaceBefore = 6
            self.spaceAfter = 12
            self._w, self._h = width + 2 * self.pad, height + 2 * self.pad
            self._iw, self._ih = width, height

        def wrap(self, avail_width: float, avail_height: float):  # noqa: D102
            w, h = width, height
            # A small shrink never hurts; a larger one only while labels stay legible.
            floor = min(h, max(self.min_fit, min(h * 0.85, legible_h) if legible_h else 0.0))
            if avail_height >= _MEASURE_HEIGHT:
                # A heading's keepWithNext groups it with this card in a KeepTogether, which
                # measures at unlimited height. Report the smallest size this card would
                # accept, or the group always jumps to a new page and strands the space
                # left on this one; the real wrap call below then shrinks to fit.
                w, h = w * floor / h, floor
            else:
                room_h = avail_height - 2 * self.pad
                if h > room_h >= floor:
                    w, h = w * room_h / h, room_h
            room_w = avail_width - 2 * self.pad
            if w > room_w:
                w, h = room_w, h * room_w / w
            self._iw, self._ih = w, h
            self._w, self._h = w + 2 * self.pad, h + 2 * self.pad
            return self._w, self._h

        def draw(self) -> None:  # noqa: D102
            c = self.canv
            c.saveState()
            c.setFillColor(colors.white)
            c.setStrokeColor(colors.HexColor("#E2E8F0"))
            c.setLineWidth(0.6)
            c.roundRect(0, 0, self._w, self._h, 4, stroke=1, fill=1)
            c.drawImage(
                ImageReader(io.BytesIO(data)), self.pad, self.pad,
                width=self._iw, height=self._ih, mask="auto",
            )
            c.restoreState()

    return DiagramCard()


def image_flowable(data: bytes, max_width: float, max_height: float):
    """Embed arbitrary image bytes (a prompt attachment, say) as a bordered card.

    Returns None when the bytes aren't a readable image, so callers can fall back to
    naming the file instead of failing the whole export.
    """
    try:
        return _diagram_card(data, max_width, max_height)
    except Exception:  # noqa: BLE001 — an unreadable image must not sink the document
        return None


def _fit_col_widths(
    cols: list[str], rows: list[list[str]], content_width: float, font_size: float = 8.5
) -> list[float]:
    """Distribute the frame width across table columns.

    Every column first reserves enough room for its widest unbreakable word (so short
    values like ``99.95%`` are never split across lines), then the slack is shared out in
    proportion to how much text each column holds.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    fonts = pdf_fonts()
    pad = 12.0
    ceiling = content_width / 2
    mins: list[float] = []
    weights: list[float] = []
    for i in range(len(cols)):
        cells = [str(cols[i])] + [str(r[i]) if i < len(r) else "" for r in rows]
        texts = [_strip_inline(c) for c in cells]
        widest_word = 0.0
        for text in texts:
            for word in text.split() or [""]:
                widest_word = max(
                    widest_word, stringWidth(pdf_safe(word), fonts["bold"], font_size)
                )
        mins.append(min(ceiling, widest_word + pad))
        weights.append(float(max(4, min(max((len(t) for t in texts), default=1), 60))))

    total_min = sum(mins)
    if total_min >= content_width:
        return [w * content_width / total_min for w in mins]
    slack = content_width - total_min
    total_weight = sum(weights) or 1.0
    return [m + slack * w / total_weight for m, w in zip(mins, weights, strict=False)]


def _pdf_table_cell(flowable):
    """Give nested content the geometry ReportLab's in-row splitter expects.

    Tables and custom cards return a size from wrap() without setting ``height``;
    the cell splitter reads that attribute directly and ignores external spacing.
    Include the spacing in the measured box and adapt split fragments as well.
    """
    from reportlab.platypus import Flowable, ListFlowable, Table

    if isinstance(flowable, ListFlowable):
        # ListFlowable.split expands every item, whereas Table._splitCell consumes
        # exactly two fragments. Rows preserve bullets/numbering and let ReportLab
        # split between items (or within a long item) into a proper continuation.
        flowable = Table(
            [[_pdf_table_cell(item)] for item in flowable.split(0, 0)],
            colWidths=["100%"], hAlign="LEFT", splitByRow=1, splitInRow=1,
            style=[(f"{side}PADDING", (0, 0), (-1, -1), 0)
                   for side in ("LEFT", "RIGHT", "TOP", "BOTTOM")],
        )

    class CellFlowable(Flowable):
        def __init__(self, child, before=None, after=None):
            super().__init__()
            self.child = child
            self.before = child.getSpaceBefore() if before is None else before
            self.after = child.getSpaceAfter() if after is None else after
            self.hAlign = getattr(child, "hAlign", "LEFT")

        def wrap(self, avail_width, avail_height):
            self._canvas = self.canv
            self._avail_width = avail_width
            self.width, height = self.child.wrapOn(
                self.canv, avail_width, max(0, avail_height - self.before - self.after)
            )
            self.height = height + self.before + self.after
            return self.width, self.height

        def split(self, avail_width, avail_height):
            # Table._splitCell passes the full column width, including cell padding.
            parts = self.child.splitOn(
                self._canvas, min(avail_width, self._avail_width),
                max(0, avail_height - self.before - self.after),
            )
            # The enclosing Table consumes exactly two fragments, never a story.
            if len(parts) != 2:
                return []
            return [CellFlowable(part, self.before if i == 0 else 0,
                                 self.after if i == len(parts) - 1 else 0)
                    for i, part in enumerate(parts)]

        def draw(self):
            self.child.drawOn(self.canv, 0, self.after)

    return CellFlowable(flowable)


def markdown_pdf_flowables(
    md: str,
    body_style: Any,
    *,
    diagrams: Sequence[dict] | None = None,
    content_width: float = 6.9 * 72,
    user_id: str | None = None,
    db: DbSession | None = None,
    in_table: bool = False,
) -> list:
    """Return a list of reportlab flowables rendering Markdown ``md``.

    ``diagrams`` optionally supplies pre-rendered images for ```mermaid``` fences, each
    ``{"code": str, "data": bytes, "width": float, "height": float}`` — the chat UI
    rasterizes the diagram it is already showing so the PDF matches the lane exactly.
    Local generated images require ``user_id``; ``db`` optionally reuses the export's
    session for ownership checks. Nested quotes retain this explicit context.
    ``in_table`` returns measured, splittable cell content instead of frame-control
    KeepTogether objects. Leave it false for normal single-column document stories.
    """
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        KeepTogether,
        ListFlowable,
        ListItem,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
        XPreformatted,
    )
    from reportlab.platypus.flowables import HRFlowable

    from .tools.artifacts import resolve_image_bytes

    fonts = pdf_fonts()
    heading_style = ParagraphStyle(
        "MdHeading", parent=body_style, fontName=fonts["bold"],
        textColor=colors.HexColor("#1E1B4B"), spaceBefore=10, spaceAfter=4,
        keepWithNext=1,
    )
    code_font_size = 8.0
    # reportlab draws a paragraph's background OUTSIDE its measured box, so the vertical
    # padding eats into the surrounding space; keep it small and let spaceBefore/After
    # (which must exceed twice the padding) create the visible gap between blocks.
    code_pad_x, code_pad_y = 10.0, 7.0
    code_style = ParagraphStyle(
        "MdCode", parent=body_style, fontName=fonts["mono"], fontSize=code_font_size,
        leading=code_font_size * 1.45, textColor=colors.HexColor(CODE_FG),
        backColor=colors.HexColor(CODE_BG),
        borderPadding=(code_pad_y, code_pad_x, code_pad_y, code_pad_x),
        borderRadius=4, spaceBefore=15, spaceAfter=22, leftIndent=0, rightIndent=0,
    )
    quote_style = ParagraphStyle(
        "MdQuote", parent=body_style, leftIndent=14, textColor=colors.HexColor("#475569"),
        borderPadding=4, spaceBefore=4, spaceAfter=6,
    )
    caption_style = ParagraphStyle(
        "MdCaption", parent=body_style, fontSize=8, leading=10, alignment=1,
        textColor=colors.HexColor("#64748B"), spaceBefore=2, spaceAfter=8,
    )
    list_style = ParagraphStyle("MdListItem", parent=body_style, spaceAfter=2)

    pending = [dict(d) for d in (diagrams or [])]

    def take_diagram(code: str) -> dict | None:
        key = (code or "").strip()
        for i, d in enumerate(pending):
            if (d.get("code") or "").strip() == key:
                return pending.pop(i)
        return None

    def code_flowable(text: str, lang: str, style, chars: int):
        """``chars`` is the exact column count: DejaVu Sans Mono and Courier are 0.6em."""
        spans = _wrap_code_spans(_code_spans(text.replace("\t", "    "), lang), chars)
        return XPreformatted(_code_markup(spans) or " ", style)

    # A quote shifts its children right; nothing else about them changes, so the whole
    # renderer is re-entered with a larger indent rather than flattened into one string.
    quote_indent = 16.0
    nested_bullet = "\u25e6" if _glyph_ok()("\u25e6") else "\u2013"

    def render(
        blocks: Sequence[Block],
        indent: float = 0.0,
        quoted: bool = False,
        inset: float = 0.0,
        depth: int = 0,
    ) -> list:
        """``inset`` is width taken by enclosing list items; ``depth`` their nesting level."""
        avail = content_width - indent - inset
        tag = f"{int(indent)}{'q' if quoted else ''}"
        b_style = ParagraphStyle(
            f"MdBody{tag}", parent=body_style, leftIndent=body_style.leftIndent + indent
        )
        h_style = ParagraphStyle(
            f"MdHead{tag}", parent=heading_style,
            leftIndent=heading_style.leftIndent + indent,
        )
        i_style = ParagraphStyle(f"MdItem{tag}", parent=list_style)
        c_style = ParagraphStyle(f"MdCodeI{tag}", parent=code_style, leftIndent=indent)
        cap_style = ParagraphStyle(
            f"MdCap{tag}", parent=caption_style, leftIndent=caption_style.leftIndent + indent
        )
        if quoted:
            b_style.textColor = quote_style.textColor
            i_style.textColor = quote_style.textColor
        chars = max(20, int((avail - 2 * code_pad_x) / (code_font_size * 0.602)))

        flow: list = []
        for block in blocks:
            kind = block[0]
            if kind == "h":
                size = max(15 - (block[1] - 1) * 1.6, 10.0)
                hs = ParagraphStyle(
                    f"MdH{block[1]}{tag}", parent=h_style, fontSize=size, leading=size + 4
                )
                flow.append(Paragraph(_inline_pdf(block[2]), hs))
            elif kind == "p":
                flow.append(Paragraph(_inline_pdf(block[1]), b_style))
            elif kind in ("ul", "ol"):
                ordered = kind == "ol"
                list_indent = 18 + indent
                items = []
                for text, kids in zip(block[1], list_children(block), strict=False):
                    parts = [Paragraph(_inline_pdf(text), i_style)]
                    if kids:
                        parts.extend(
                            render(kids, 0.0, quoted, inset + list_indent, depth + 1)
                        )
                    items.append(ListItem(parts))
                bullet = "\u2022" if depth == 0 else nested_bullet
                flow.append(
                    ListFlowable(
                        items,
                        bulletType="1" if ordered else "bullet",
                        start=(block[2] if len(block) > 2 else 1) if ordered else bullet,
                        bulletFormat="%s." if ordered else None,
                        leftIndent=list_indent,
                        bulletDedent=12,
                        bulletFontName=fonts["body"],
                        bulletFontSize=body_style.fontSize,
                        bulletColor=i_style.textColor,
                        spaceBefore=2,
                        spaceAfter=6,
                    )
                )
            elif kind == "code":
                text, lang = block[1], (block[2] if len(block) > 2 else "")
                if lang == "mermaid":
                    d = take_diagram(text)
                    card = (
                        _diagram_card(
                            d["data"], avail, 8.0 * 72,
                            natural_pt=float(d.get("width") or 0) * 0.75,
                        )
                        if d and d.get("data")
                        else None
                    )
                    if card is not None:
                        flow.append(card)
                        continue
                flow.append(code_flowable(text or " ", lang, c_style, chars))
            elif kind == "image":
                data = resolve_image_bytes(block[2], user_id=user_id, db=db)
                card = _diagram_card(data, avail, 7.0 * 72) if data else None
                if card is None:
                    flow.append(
                        Paragraph(_inline_pdf(f"[{block[1] or 'image'}]({block[2]})"), b_style)
                    )
                elif block[1]:
                    parts = [card, Paragraph(escape(pdf_safe(block[1])), cap_style)]
                    if in_table:
                        flow.extend(parts)
                    else:
                        flow.append(KeepTogether(parts))
                else:
                    flow.append(card)
            elif kind == "quote":
                flow.append(Spacer(1, 4))
                flow.extend(render(block[1], indent + quote_indent, True, inset, depth))
                flow.append(Spacer(1, 4))
            elif kind == "hr":
                flow.append(
                    HRFlowable(width=avail, thickness=0.6, color=colors.HexColor("#CBD5E1"),
                               spaceBefore=6, spaceAfter=6, hAlign="LEFT")
                )
            elif kind == "table":
                cols, rows = block[1], block[2]
                if not cols:
                    continue
                cell_style = ParagraphStyle(
                    "MdCell", parent=body_style, fontSize=8.5, leading=11, spaceAfter=0
                )
                head_style = ParagraphStyle(
                    "MdCellHead", parent=cell_style, fontName=fonts["bold"],
                    textColor=colors.HexColor("#1E1B4B"),
                )
                data = [[Paragraph(_inline_pdf(str(c)), head_style) for c in cols]]
                for r in rows:
                    data.append(
                        [
                            Paragraph(_inline_pdf(str(r[c_i])) if c_i < len(r) else "",
                                      cell_style)
                            for c_i in range(len(cols))
                        ]
                    )
                widths = _fit_col_widths(cols, rows, avail)
                # An indented table gets a blank spacer column so it lines up with the
                # quoted text around it; reportlab tables have no left indent of their own.
                x0 = 0
                if indent:
                    x0 = 1
                    widths = [indent] + widths
                    data = [[""] + row for row in data]
                # ``splitInRow`` lets a row taller than the page break across pages;
                # without it reportlab raises LayoutError and the export fails outright.
                tbl = Table(
                    data, colWidths=widths, hAlign="LEFT", repeatRows=1,
                    splitByRow=1, splitInRow=1,
                )

                tbl.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (x0, 0), (-1, 0), colors.HexColor("#EEF2FF")),
                            ("ROWBACKGROUNDS", (x0, 1), (-1, -1),
                             [colors.white, colors.HexColor("#F8FAFC")]),
                            ("GRID", (x0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 5),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                            ("TOPPADDING", (0, 0), (-1, -1), 4),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ]
                        + ([("LEFTPADDING", (0, 0), (0, -1), 0),
                            ("RIGHTPADDING", (0, 0), (0, -1), 0)] if x0 else [])
                    )
                )
                flow.append(tbl)
                flow.append(Spacer(1, 8))
        return flow

    flowables = render(parse_blocks(md))
    return [_pdf_table_cell(item) for item in flowables] if in_table else flowables
