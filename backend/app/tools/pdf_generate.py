from __future__ import annotations

import io
import os
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .artifacts import (
    download_result,
    generated_dir,
    new_stored_name,
    resolve_image_bytes,
    safe_download_name,
)
from .base import ToolContext, ToolDef, ToolResult

_CHART_PALETTE = [
    "#6366F1", "#10B981", "#F59E0B", "#EF4444",
    "#3B82F6", "#8B5CF6", "#EC4899", "#14B8A6",
]


def _chart_drawing(spec: dict):
    """Build a native reportlab chart (bar/pie) Drawing from a chart spec, or None."""
    try:
        from reportlab.graphics.charts.barcharts import VerticalBarChart
        from reportlab.graphics.charts.piecharts import Pie
        from reportlab.graphics.shapes import Drawing, String

        ctype = (spec.get("type") or "bar").strip().lower()
        cats = [str(c) for c in (spec.get("categories") or [])]
        norm: list[list[float]] = []
        for s in spec.get("series") or []:
            if not isinstance(s, dict):
                continue
            vals: list[float] = []
            for v in s.get("values") or []:
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    vals.append(0.0)
            norm.append(vals)
        if not cats or not norm:
            return None

        d = Drawing(420, 210)
        title = (spec.get("title") or "").strip()
        if title:
            d.add(String(10, 196, title, fontName="Helvetica-Bold", fontSize=11))
        if ctype in ("pie", "doughnut"):
            pie = Pie()
            pie.x, pie.y, pie.width, pie.height = 150, 15, 160, 160
            pie.data = norm[0]
            pie.labels = cats
            pie.sideLabels = True
            for i in range(len(norm[0])):
                pie.slices[i].fillColor = HexColor(_CHART_PALETTE[i % len(_CHART_PALETTE)])
            d.add(pie)
        else:
            bc = VerticalBarChart()
            bc.x, bc.y, bc.width, bc.height = 45, 25, 350, 150
            bc.data = norm
            bc.categoryAxis.categoryNames = cats
            bc.valueAxis.valueMin = 0
            bc.barSpacing = 2
            for i in range(len(norm)):
                bc.bars[i].fillColor = HexColor(_CHART_PALETTE[i % len(_CHART_PALETTE)])
            d.add(bc)
        return d
    except Exception:  # noqa: BLE001
        return None


class PdfGenerateTool:
    definition = ToolDef(
        name="generate_pdf",
        description=(
            "Create a downloadable PDF document from a structured outline that you "
            "author. Use this whenever the user asks for a PDF, report, or handout. "
            "Provide a title and a list of sections (each with an optional heading, "
            "paragraphs, bullet points, and/or a table). Returns a Markdown download "
            "link — include it verbatim in your reply."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Document title."},
                "subtitle": {"type": "string", "description": "Optional subtitle."},
                "sections": {
                    "type": "array",
                    "description": "The document sections, in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {
                                "type": "string",
                                "description": "Optional section heading.",
                            },
                            "paragraphs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Body paragraphs.",
                            },
                            "bullets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Bullet points.",
                            },
                            "table": {
                                "type": "object",
                                "description": "Optional table.",
                                "properties": {
                                    "columns": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "rows": {
                                        "type": "array",
                                        "items": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                    },
                                },
                            },
                            "image": {
                                "type": "string",
                                "description": (
                                    "Optional image to embed: a data: URI, an http(s) "
                                    "image URL, or a generated /api/files/<name> link."
                                ),
                            },
                            "caption": {
                                "type": "string",
                                "description": "Optional caption shown under the image.",
                            },
                            "chart": {
                                "type": "object",
                                "description": "Optional native chart drawn in the PDF.",
                                "properties": {
                                    "type": {
                                        "type": "string",
                                        "enum": ["bar", "pie"],
                                    },
                                    "title": {"type": "string"},
                                    "categories": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "series": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "name": {"type": "string"},
                                                "values": {
                                                    "type": "array",
                                                    "items": {"type": "number"},
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "required": ["title", "sections"],
        },
    )

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        title = (args.get("title") or "").strip() or "Document"
        subtitle = (args.get("subtitle") or "").strip()
        sections = args.get("sections") or []
        if not isinstance(sections, list) or not sections:
            return ToolResult(content="No content was provided.", citations=None)

        try:
            styles = getSampleStyleSheet()
            title_style = ParagraphStyle(
                "DeckTitle", parent=styles["Title"], textColor=HexColor("#1E1B4B")
            )
            sub_style = ParagraphStyle(
                "Sub", parent=styles["Italic"], textColor=HexColor("#64748B"), spaceAfter=14
            )
            head_style = ParagraphStyle(
                "Head",
                parent=styles["Heading2"],
                textColor=HexColor("#4F46E5"),
                spaceBefore=10,
                spaceAfter=6,
            )
            body_style = ParagraphStyle(
                "Body", parent=styles["BodyText"], alignment=TA_LEFT, spaceAfter=6, leading=15
            )

            stored_name = new_stored_name("pdf")
            path = os.path.join(generated_dir(), stored_name)
            doc = SimpleDocTemplate(
                path,
                pagesize=LETTER,
                topMargin=0.9 * inch,
                bottomMargin=0.9 * inch,
                leftMargin=0.9 * inch,
                rightMargin=0.9 * inch,
                title=title,
            )
            story: list = [Paragraph(escape(title), title_style)]
            if subtitle:
                story.append(Paragraph(escape(subtitle), sub_style))
            story.append(Spacer(1, 8))

            for sec in sections:
                if not isinstance(sec, dict):
                    continue
                heading = (sec.get("heading") or "").strip()
                if heading:
                    story.append(Paragraph(escape(heading), head_style))
                paragraphs = sec.get("paragraphs") or []
                if isinstance(paragraphs, str):
                    paragraphs = [paragraphs]
                for para in paragraphs:
                    text = str(para).strip()
                    if text:
                        story.append(Paragraph(escape(text), body_style))
                bullets = sec.get("bullets") or []
                if isinstance(bullets, str):
                    bullets = [bullets]
                items = [
                    ListItem(Paragraph(escape(str(b).strip()), body_style))
                    for b in bullets
                    if str(b).strip()
                ]
                if items:
                    story.append(ListFlowable(items, bulletType="bullet", leftIndent=18))

                table = sec.get("table")
                if isinstance(table, dict):
                    cols = table.get("columns") or []
                    rows = table.get("rows") or []
                    if cols:
                        data = [[str(c) for c in cols]]
                        for r in rows:
                            data.append(
                                [str(r[i]) if i < len(r) else "" for i in range(len(cols))]
                            )
                        tbl = Table(data, hAlign="LEFT")
                        tbl.setStyle(
                            TableStyle(
                                [
                                    ("BACKGROUND", (0, 0), (-1, 0), HexColor("#4F46E5")),
                                    ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#FFFFFF")),
                                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                                    ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#CBD5E1")),
                                    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                                     [HexColor("#FFFFFF"), HexColor("#F1F5F9")]),
                                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                                ]
                            )
                        )
                        story.append(Spacer(1, 6))
                        story.append(tbl)

                image_ref = (sec.get("image") or "").strip()
                if image_ref:
                    data = resolve_image_bytes(image_ref)
                    if data:
                        try:
                            reader = ImageReader(io.BytesIO(data))
                            iw, ih = reader.getSize()
                            max_w = 5.5 * inch
                            w = min(max_w, iw)
                            h = w * (ih / iw) if iw else 3 * inch
                            story.append(Spacer(1, 6))
                            story.append(Image(io.BytesIO(data), width=w, height=h))
                            caption = (sec.get("caption") or "").strip()
                            if caption:
                                story.append(Paragraph(escape(caption), sub_style))
                        except Exception:  # noqa: BLE001
                            pass

                chart_spec = sec.get("chart")
                if isinstance(chart_spec, dict):
                    drawing = _chart_drawing(chart_spec)
                    if drawing is not None:
                        story.append(Spacer(1, 6))
                        story.append(drawing)

                story.append(Spacer(1, 6))

            doc.build(story)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"Failed to build PDF: {exc}", citations=None)

        download_name = safe_download_name(title, "pdf")
        return download_result(
            stored_name, download_name, f"Created a PDF with {len(sections)} section(s)."
        )
