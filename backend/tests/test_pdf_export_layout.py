"""Offline export layout regressions using temporary SQLite and synthetic PNGs.

Reuse the ownership fixtures, not the app lifespan: sockets/HTTP are forbidden and
only conftest's throwaway database and this test's upload directory are touched.
"""
from __future__ import annotations

import io
import re
from unittest.mock import Mock

import pytest
import test_generated_image_ownership as ownership_fixtures
from PIL import Image
from pypdf import PdfReader
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table

from app import db as database
from app import export, models
from app.markdown_render import markdown_pdf_flowables
from app.tools import artifacts

# Register just the reusable offline fixtures, never conftest's lifespan-backed client.
chat = ownership_fixtures.chat
offline = ownership_fixtures.offline
owned = ownership_fixtures.owned
local_ref = ownership_fixtures.local_ref


def text_of(reader):
    return " ".join(" ".join(page.extract_text().split()) for page in reader.pages)


def assert_image(reader, data, caption):
    expected = Image.open(io.BytesIO(data)).convert("RGB")
    images = [(page, item.image.convert("RGB"))
              for page in reader.pages for item in page.images]
    assert images
    for page, image in images:
        assert image.size == expected.size
        assert image.tobytes() == expected.tobytes()
        assert caption in " ".join(page.extract_text().split())


def image_placements(reader):
    placements = []
    for page_index, page in enumerate(reader.pages):
        def collect(operator, args, cm, tm, page_index=page_index):
            if operator == b"Do":
                placements.append((page_index, list(cm)))
        page.extract_text(visitor_operand_before=collect)
    return placements


def replace_own_image(owned, size):
    output = io.BytesIO()
    image = Image.new("RGB", size, "#6366f1")
    image.putpixel((0, 0), (255, 0, 0))
    image.putpixel((size[0] - 1, size[1] - 1), (0, 255, 0))
    image.save(output, format="PNG")
    owned.images["own"] = output.getvalue()
    (owned.root / owned.names["own"]).write_bytes(owned.images["own"])


@pytest.mark.parametrize("absolute", [False, True])
def test_captioned_image_in_export_cell_preserves_ownership(owned, chat, monkeypatch, absolute):
    chat.run.minority_report = "\n\n".join(
        f"> ![{key} caption < & >]({local_ref(owned, key, absolute)})" for key in owned.images
    )
    owned.db.flush()
    resolver = Mock(wraps=artifacts.resolve_image_bytes)
    monkeypatch.setattr(artifacts, "resolve_image_bytes", resolver)
    monkeypatch.setattr(database, "SessionLocal", lambda: pytest.fail("Export opened a new DB"))
    stored, _, _ = export.export_deliberation_pdf(owned.db, chat.run)
    reader = PdfReader(owned.root / stored)
    assert_image(reader, owned.images["own"], "own caption < & >")
    assert len(image_placements(reader)) == 1
    assert "MINORITY REPORT" in text_of(reader)
    assert len(resolver.call_args_list) == 3
    for call in resolver.call_args_list:
        assert call.kwargs == {"user_id": owned.owner.id, "db": owned.db}


def test_multilane_session_keeps_captioned_images(owned, chat):
    second = models.Lane(session_id=chat.session.id, provider_id=chat.lane.provider_id,
                         model="second", position=1)
    owned.db.add(second)
    owned.db.flush()
    chat.message.content = f"![First lane caption]({local_ref(owned)})"
    owned.db.add(models.LaneMessage(lane_id=second.id, turn_id=chat.turn.id,
                                   content=f"![Second lane caption]({local_ref(owned)})"))
    owned.db.flush()
    stored, _, _ = export.export_session(owned.db, chat.session, "pdf")
    reader = PdfReader(owned.root / stored)
    for caption in ("First lane caption", "Second lane caption"):
        assert caption in text_of(reader)
    expected = Image.open(io.BytesIO(owned.images["own"])).convert("RGB").tobytes()
    assert len(image_placements(reader)) == 2
    assert all(item.image.convert("RGB").tobytes() == expected
               for page in reader.pages for item in page.images)


@pytest.mark.parametrize("lane_count", [1, 2, 3])
@pytest.mark.parametrize("size", [(320, 180), (120, 2400)])
def test_comparison_cells_keep_images_and_captions(owned, lane_count, size):
    replace_own_image(owned, size)
    styles = getSampleStyleSheet()
    width = 480 / lane_count
    cells = [markdown_pdf_flowables(
        f"> ![Lane {i} caption < & >]({local_ref(owned)})", styles["BodyText"],
        content_width=width - 12, user_id=owned.owner.id, db=owned.db, in_table=True,
    ) for i in range(lane_count)]
    table = Table([cells], colWidths=[width] * lane_count, splitByRow=1, splitInRow=1,
                  style=[("VALIGN", (0, 0), (-1, -1), "TOP")])
    path = owned.root / "comparison.pdf"
    SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=54, rightMargin=54).build([table])
    reader = PdfReader(path)
    placements = image_placements(reader)
    assert len(placements) == lane_count
    for i, (page_index, matrix) in enumerate(placements):
        assert_image(reader, owned.images["own"], f"Lane {i} caption < & >")
        w, skew_y, skew_x, h, x, y = matrix
        assert skew_x == skew_y == 0
        assert w > 0 and h > 0
        assert w / h == pytest.approx(size[0] / size[1], rel=1e-5)
        assert 66 + width * i <= x < x + w <= 66 + width * (i + 1)
        assert 78 <= y < y + h <= float(reader.pages[page_index].mediabox.height) - 78


@pytest.mark.parametrize("lane_count", [1, 2])
@pytest.mark.parametrize("nested_table", [False, True])
def test_long_comparison_cells_split_without_losing_content(owned, lane_count, nested_table):
    replace_own_image(owned, (120, 2400))
    style = getSampleStyleSheet()["BodyText"]
    width = 480 / lane_count
    cells = []
    for i in range(lane_count):
        tokens = " ".join(f"lane{i}word{n:04d}" for n in range(500))
        if nested_table:
            answer = f"| ID | Description |\n| --- | --- |\n| row | {tokens} |"
        else:
            answer = tokens
        md = (f"![Lane {i} image caption]({local_ref(owned)})\n\n{answer}\n\n"
              f"Lane {i} answer end")
        cells.append(markdown_pdf_flowables(md, style, content_width=width - 12,
                                           user_id=owned.owner.id, db=owned.db, in_table=True))
    table = Table([[Paragraph(f"Model {i}", style) for i in range(lane_count)], cells],
                  colWidths=[width] * lane_count, repeatRows=1, splitByRow=1, splitInRow=1,
                  style=[("VALIGN", (0, 0), (-1, -1), "TOP")])
    path = owned.root / "long-comparison.pdf"
    SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=54, rightMargin=54).build([table])
    reader = PdfReader(path)
    assert len(reader.pages) > 1
    assert len(image_placements(reader)) == lane_count
    text = text_of(reader)
    for i in range(lane_count):
        assert re.findall(fr"lane{i}word\d{{4}}", text) == [f"lane{i}word{n:04d}" for n in range(500)]
        assert f"Lane {i} answer end" in text
        for page in reader.pages:
            if list(page.images):
                assert f"Lane {i} image caption" in page.extract_text()
            if f"lane{i}word" in page.extract_text():
                assert f"Model {i}" in page.extract_text()
                if nested_table:
                    assert "Description" in page.extract_text()
    expected = Image.open(io.BytesIO(owned.images["own"])).convert("RGB").tobytes()
    assert all(item.image.convert("RGB").tobytes() == expected
               for page in reader.pages for item in page.images)


def test_long_minority_report_preserves_tall_image_caption_and_table(owned, chat):
    replace_own_image(owned, (800, 2400))
    tokens = [f"entry{n:04d}" for n in range(800)]
    chat.run.minority_report = (
        f"> ![Tall dissent caption]({local_ref(owned)})\n\n"
        f"| ID | Description |\n| --- | --- |\n| row | {' '.join(tokens)} |\n\n"
        "After the dissent table"
    )
    owned.db.flush()
    stored, _, _ = export.export_deliberation_pdf(owned.db, chat.run)
    reader = PdfReader(owned.root / stored)
    assert len(reader.pages) > 1
    assert len(image_placements(reader)) == 1
    assert_image(reader, owned.images["own"], "Tall dissent caption")
    assert re.findall(r"entry\d{4}", text_of(reader)) == tokens
    assert "After the dissent table" in text_of(reader)


@pytest.mark.parametrize("ordered", [False, True])
def test_cell_list_splits_without_dropping_items(owned, chat, ordered):
    # ListFlowable.split returns every item; Table._splitCell only consumes TWO
    # fragments. A cell adapter must preserve the entire continuation, not just item 2.
    items = [f"item{n:04d} " + "explanation " * 12 for n in range(80)]
    chat.run.minority_report = (
        f"![List figure caption]({local_ref(owned)})\n\n"
        + "\n".join(f"{n + 1}. {item}" if ordered else f"- {item}"
                    for n, item in enumerate(items))
        + "\n\nAfter all list items"
    )
    owned.db.flush()
    stored, _, _ = export.export_deliberation_pdf(owned.db, chat.run)
    reader = PdfReader(owned.root / stored)
    assert len(reader.pages) > 1
    assert re.findall(r"item\d{4}", text_of(reader)) == [f"item{n:04d}" for n in range(80)]
    assert_image(reader, owned.images["own"], "List figure caption")
    assert "After all list items" in text_of(reader)


@pytest.mark.parametrize("word_count", [80, 1000])
def test_long_cell_caption_is_not_truncated(owned, chat, word_count):
    tokens = [f"caption{n:04d}" for n in range(word_count)]
    chat.run.minority_report = (f"![{' '.join(tokens)}]({local_ref(owned)})\n\n"
                               "After the long caption")
    owned.db.flush()
    stored, _, _ = export.export_deliberation_pdf(owned.db, chat.run)
    reader = PdfReader(owned.root / stored)
    assert len(image_placements(reader)) == 1
    assert_image(reader, owned.images["own"], tokens[0])
    assert re.findall(r"caption\d{4}", text_of(reader)) == tokens
    assert "After the long caption" in text_of(reader)


def test_single_column_keeps_image_caption_together_at_page_boundary(owned):
    style = getSampleStyleSheet()["BodyText"]
    flow = markdown_pdf_flowables(f"![Boundary caption]({local_ref(owned)})", style,
                                 content_width=440, user_id=owned.owner.id, db=owned.db)
    assert isinstance(flow[0], KeepTogether)
    path = owned.root / "single-column.pdf"
    SimpleDocTemplate(str(path), pagesize=LETTER).build(
        [Spacer(1, 510), *flow, Paragraph("After the image", style)]
    )
    reader = PdfReader(path)
    assert len(reader.pages) == 2
    assert not list(reader.pages[0].images)
    assert_image(reader, owned.images["own"], "Boundary caption")
    assert "After the image" in text_of(reader)
