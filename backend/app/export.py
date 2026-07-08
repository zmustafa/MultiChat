from __future__ import annotations

import os
from xml.sax.saxutils import escape

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .models import Lane, LaneMessage, Provider, Session as ChatSession, Turn
from .tools.artifacts import generated_dir, new_stored_name, safe_download_name

_MIME = {
    "md": "text/markdown",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


def _gather(db: DbSession, session: ChatSession):
    lanes = [l for l in sorted(session.lanes, key=lambda x: x.position) if l.role == "responder"]
    turns = sorted(session.turns, key=lambda x: x.order_index)
    messages = db.scalars(
        select(LaneMessage)
        .join(Lane, Lane.id == LaneMessage.lane_id)
        .where(Lane.session_id == session.id, LaneMessage.role == "assistant")
    ).all()
    by_key: dict[tuple[str, str], LaneMessage] = {}
    for m in messages:
        by_key[(m.lane_id, m.turn_id)] = m
    providers = {p.id: p for p in db.scalars(select(Provider)).all()}

    def lane_label(lane: Lane) -> str:
        prov = providers.get(lane.provider_id)
        pname = prov.name if prov else "provider"
        return f"{lane.model} ({pname})"

    return lanes, turns, by_key, lane_label


def _export_markdown(db, session, path) -> None:
    lanes, turns, by_key, lane_label = _gather(db, session)
    out: list[str] = [f"# {session.title or 'Comparison'}", ""]
    for i, turn in enumerate(turns, 1):
        out.append(f"## Turn {i}")
        out.append("")
        out.append(f"**Prompt:** {turn.content}")
        out.append("")
        for lane in lanes:
            msg = by_key.get((lane.id, turn.id))
            if not msg or not msg.content:
                continue
            out.append(f"### {lane_label(lane)}")
            out.append("")
            out.append(msg.content)
            out.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))


def _export_docx(db, session, path) -> None:
    from docx import Document
    from docx.shared import RGBColor

    from .markdown_render import render_markdown_docx

    lanes, turns, by_key, lane_label = _gather(db, session)
    doc = Document()
    h = doc.add_heading(session.title or "Comparison", level=0)
    h.runs[0].font.color.rgb = RGBColor(0x1E, 0x1B, 0x4B)
    for i, turn in enumerate(turns, 1):
        doc.add_heading(f"Turn {i}", level=1)
        p = doc.add_paragraph()
        run = p.add_run("Prompt: ")
        run.bold = True
        p.add_run(turn.content or "")
        for lane in lanes:
            msg = by_key.get((lane.id, turn.id))
            if not msg or not msg.content:
                continue
            hh = doc.add_heading(lane_label(lane), level=2)
            hh.runs[0].font.color.rgb = RGBColor(0x4F, 0x46, 0xE5)
            render_markdown_docx(doc, msg.content, base_level=3)
    doc.save(path)


def _export_pdf(db, session, path) -> None:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    from .markdown_render import markdown_pdf_flowables

    lanes, turns, by_key, lane_label = _gather(db, session)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("T", parent=styles["Title"], textColor=HexColor("#1E1B4B"))
    turn_style = ParagraphStyle(
        "Turn", parent=styles["Heading1"], textColor=HexColor("#1E1B4B"), spaceBefore=12
    )
    lane_style = ParagraphStyle(
        "Lane", parent=styles["Heading3"], textColor=HexColor("#4F46E5"), spaceBefore=6
    )
    body = ParagraphStyle("Body", parent=styles["BodyText"], spaceAfter=5, leading=14)
    prompt_style = ParagraphStyle(
        "Prompt", parent=body, backColor=HexColor("#EEF2FF"), borderPadding=6, spaceAfter=8
    )

    doc = SimpleDocTemplate(
        path, pagesize=LETTER, topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch, title=session.title or "Comparison",
    )
    story: list = [Paragraph(escape(session.title or "Comparison"), title_style), Spacer(1, 8)]
    for i, turn in enumerate(turns, 1):
        story.append(Paragraph(f"Turn {i}", turn_style))
        story.append(Paragraph("<b>Prompt:</b> " + escape(turn.content or ""), prompt_style))
        for lane in lanes:
            msg = by_key.get((lane.id, turn.id))
            if not msg or not msg.content:
                continue
            story.append(Paragraph(escape(lane_label(lane)), lane_style))
            story.extend(markdown_pdf_flowables(msg.content, body))
    doc.build(story)


_BUILDERS = {"md": _export_markdown, "docx": _export_docx, "pdf": _export_pdf}


def export_session(db: DbSession, session: ChatSession, fmt: str):
    """Export a whole session (all lanes side-by-side) to md/docx/pdf.

    Returns (stored_name, download_name, mime_type).
    """
    fmt = (fmt or "md").lower()
    builder = _BUILDERS.get(fmt)
    if not builder:
        raise ValueError(f"Unsupported export format: {fmt}")
    stored_name = new_stored_name(fmt)
    path = os.path.join(generated_dir(), stored_name)
    builder(db, session, path)
    download_name = safe_download_name(session.title or "comparison", fmt, fallback="comparison")
    return stored_name, download_name, _MIME.get(fmt, "application/octet-stream")
