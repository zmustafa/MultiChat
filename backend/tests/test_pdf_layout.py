"""Offline PDF layout regressions: temporary output, mocked images, no database.

Inspect the actual PDF text and image payloads, not just the returned download link.
"""
from __future__ import annotations

import asyncio
import io
import re
import socket
from unittest.mock import Mock

import httpx
import pytest
from PIL import Image
from pypdf import PdfReader

from app import db as database
from app.tools import artifacts, pdf_generate
from app.tools.base import ToolContext


def png(size=(320, 180)):
    output = io.BytesIO()
    image = Image.new("RGB", size, "#6366f1")
    # Distinct corners make pixel comparisons catch cropping as well as missing images.
    image.putpixel((0, 0), (255, 0, 0))
    image.putpixel((size[0] - 1, size[1] - 1), (0, 255, 0))
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def render_pdf(monkeypatch, tmp_path):
    # Windows creates a local wakeup socketpair when constructing the loop.
    loop = asyncio.new_event_loop()

    def forbidden(*args, **kwargs):
        pytest.fail("PDF layout tests must not use the network or database")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(database, "SessionLocal", forbidden)
    monkeypatch.setattr(database.engine, "connect", forbidden)
    monkeypatch.setattr(artifacts.settings, "UPLOAD_DIR", str(tmp_path))

    def render(sections, image_data=None, expected_error=None):
        resolver = Mock(return_value=image_data)
        monkeypatch.setattr(pdf_generate, "resolve_image_bytes", resolver)
        result = loop.run_until_complete(pdf_generate.PdfGenerateTool().run(
            {"title": "Layout regression", "sections": sections, "user_id": "untrusted"},
            ToolContext(user_id="layout-owner"),
        ))
        if expected_error:
            assert result.content.startswith("Failed to build PDF:"), result.content
            assert expected_error in result.content, result.content
            assert "/api/files/" not in result.content
            return None
        match = re.search(r"/api/files/([^?\s)]+)", result.content)
        assert match, result.content
        for call in resolver.call_args_list:
            assert call.kwargs == {"user_id": "layout-owner"}
        path = tmp_path / "generated" / match.group(1)
        return PdfReader(path)

    try:
        yield render
    finally:
        loop.close()


def text_of(reader):
    return " ".join(" ".join(page.extract_text().split()) for page in reader.pages)


def image_pages(reader):
    return [page for page in reader.pages if list(page.images)]


def assert_image(reader, data):
    images = [item.image for page in reader.pages for item in page.images]
    assert len(images) == 1
    expected = Image.open(io.BytesIO(data)).convert("RGB")
    assert images[0].size == expected.size
    assert images[0].convert("RGB").tobytes() == expected.tobytes()
    page = image_pages(reader)[0]
    placements = []

    def collect_placements(operator, args, cm, tm):
        if operator == b"Do":
            placements.append(list(cm))

    page.extract_text(visitor_operand_before=collect_placements)
    assert len(placements) == 1
    width, skew_y, skew_x, height, x, y = placements[0]
    assert skew_x == skew_y == 0
    assert width > 0 and height > 0
    assert width / height == pytest.approx(expected.width / expected.height, rel=1e-5)
    inset = 0.9 * 72 + 6
    assert x >= inset - 0.01 and y >= inset - 0.01
    assert x + width <= float(page.mediabox.width) - inset + 0.01
    assert y + height <= float(page.mediabox.height) - inset + 0.01


def wide_section():
    return {"table": {"columns": ["One", "Two", "Three", "Four", "Five"],
                      "rows": [["a", "b", "c", "d", "e"]]}}


@pytest.mark.parametrize("landscape", [False, True])
def test_image_with_plain_caption_preserves_text_and_pixels(render_pdf, landscape):
    data = png()
    caption = "Figure 1: Revenue < forecast & margin > target"
    sections = [{"heading": "Image section", "image": "/api/files/mocked.png",
                 "caption": caption}]
    if landscape:
        sections.insert(0, wide_section())
    reader = render_pdf(sections, data)
    assert_image(reader, data)
    assert caption in text_of(reader)
    assert caption in image_pages(reader)[0].extract_text()
    box = reader.pages[0].mediabox
    assert (box.width > box.height) == landscape


@pytest.mark.parametrize("landscape", [False, True])
@pytest.mark.parametrize("size", [(120, 2400), (800, 2400)])
def test_tall_image_fits_page_with_caption(render_pdf, landscape, size):
    data = png(size)
    caption = "Tall figure caption stays with the image"
    sections = [{"image": "/api/files/tall.png", "caption": caption},
                {"paragraphs": ["Text after the tall image"]}]
    if landscape:
        sections.insert(0, wide_section())
    reader = render_pdf(sections, data)
    assert_image(reader, data)
    assert caption in image_pages(reader)[0].extract_text()
    assert "Text after the tall image" in text_of(reader)


def test_caption_does_not_move_to_separate_page(render_pdf):
    data = png((396, 600))
    caption = "Boundary figure caption"
    reader = render_pdf([{"image": "/api/files/boundary.png", "caption": caption}], data)
    assert_image(reader, data)
    assert caption in image_pages(reader)[0].extract_text()


@pytest.mark.parametrize("landscape", [False, True])
def test_tall_image_without_caption_fits_page(render_pdf, landscape):
    data = png((120, 2400))
    sections = [{"image": "/api/files/tall.png"}, {"paragraphs": ["Following content"]}]
    if landscape:
        sections.insert(0, wide_section())
    reader = render_pdf(sections, data)
    assert_image(reader, data)
    assert "Following content" in text_of(reader)


@pytest.mark.parametrize("word_count", [80, 1200])
def test_multiline_and_multipage_captions_preserve_all_text(render_pdf, word_count):
    data = png((800, 2400))
    tokens = [f"caption{i:04d}" for i in range(word_count)]
    reader = render_pdf([{"image": "/api/files/tall.png", "caption": " ".join(tokens)},
                         {"paragraphs": ["After caption"]}], data)
    assert_image(reader, data)
    text = text_of(reader)
    assert re.findall(r"caption\d{4}", text) == tokens
    assert "After caption" in text
    assert tokens[0] in image_pages(reader)[0].extract_text()
    if word_count == 80:
        assert tokens[-1] in image_pages(reader)[0].extract_text()


@pytest.mark.parametrize("columns", [["ID", "Description"],
                                    ["ID", "Description", "Status", "Owner", "Date"]])
def test_long_table_row_splits_without_losing_words(render_pdf, columns):
    tokens = [f"entry{i:04d}" for i in range(1200)]
    row = ["row-one", " ".join(tokens)] + ["value"] * (len(columns) - 2)
    reader = render_pdf([{"table": {"columns": columns, "rows": [row]}},
                         {"paragraphs": ["After the long table"]}])
    text = text_of(reader)
    assert len(reader.pages) > 1
    assert re.findall(r"entry\d{4}", text) == tokens
    assert "After the long table" in text
    for page in reader.pages:
        if "entry" in page.extract_text():
            assert "Description" in page.extract_text()


def test_long_cells_in_multiple_rows_and_columns_preserve_text(render_pdf):
    rows = [[" ".join(f"r{row}c{col}word{i:04d}" for i in range(400))
             for col in range(2)] for row in range(2)]
    reader = render_pdf([{"table": {"columns": ["Left", "Right"], "rows": rows}}])
    text = text_of(reader)
    for row in range(2):
        for col in range(2):
            assert re.findall(fr"r{row}c{col}word\d{{4}}", text) == rows[row][col].split()


def test_oversized_repeated_header_reports_failure_not_empty_pdf(render_pdf):
    # Repeated headers themselves are not splittable. Keep a visible failure for
    # this extreme input rather than silently deleting the table to claim success.
    render_pdf([{"table": {"columns": ["header " * 3000], "rows": [["Body"]]}}],
               expected_error="too large")


@pytest.mark.parametrize("image_data", [None, b"not an image"])
def test_unavailable_or_invalid_image_preserves_other_content(render_pdf, image_data):
    reader = render_pdf([{"image": "/api/files/unavailable.png"},
                         {"paragraphs": ["Other content remains"]}], image_data)
    assert not image_pages(reader)
    assert "Other content remains" in text_of(reader)


def test_table_literal_cells_short_rows_and_empty_rows(render_pdf):
    reader = render_pdf([{"table": {
        "columns": ["Key < & >", "Value"],
        "rows": [["<b>literal</b>", "A & B"], ["Short row"], []],
    }}])
    text = text_of(reader)
    for value in ("Key < & >", "Value", "<b>literal</b>", "A & B", "Short row"):
        assert value in text


@pytest.mark.parametrize("table", [{}, {"columns": [], "rows": []},
                                   {"columns": ["Header only"], "rows": []}])
def test_empty_tables_do_not_break_following_content(render_pdf, table):
    reader = render_pdf([{"table": table}, {"paragraphs": ["After empty table"]}])
    assert "After empty table" in text_of(reader)
    if table.get("columns"):
        assert "Header only" in text_of(reader)


def test_long_unbroken_cell_preserves_every_character(render_pdf):
    value = "AbCd0123" * 160
    reader = render_pdf([{"table": {"columns": ["Token", "Label"],
                                    "rows": [[value, "Unbroken value"]]}}])
    assert value in "".join(text_of(reader).split())
    # Line wrapping may split words in a narrow column; it must not lose characters.
    assert "Unbrokenvalue" in "".join(text_of(reader).split())


@pytest.mark.parametrize("table,error", [
    ({"columns": "Heading", "rows": [["value"]]}, "Table columns must be a list"),
    ({"columns": ["Heading"], "rows": "value"}, "Table rows must be a list"),
    ({"columns": ["Heading"], "rows": ["value"]}, "Table row 1 must be a list"),
    ({"columns": ["Heading"], "rows": [None]}, "Table row 1 must be a list"),
    ({"columns": ["Heading"], "rows": [42]}, "Table row 1 must be a list"),
    ({"columns": ["Heading"], "rows": [{"key": "value"}]}, "Table row 1 must be a list"),
    ({"columns": ["Heading"], "rows": [["kept", "lost"]]}, "Table row 1 has 2 cells"),
    ({"columns": [], "rows": [["lost"]]}, "Table rows require column headers"),
])
def test_malformed_table_is_reported_instead_of_silently_truncated(render_pdf, table, error):
    render_pdf([{"table": table}], expected_error=error)


def test_non_string_cells_are_still_rendered_as_literal_text(render_pdf):
    reader = render_pdf([{"table": {"columns": ["Value"],
                                    "rows": [[None], [42], [False], [{"key": "<value>"}]]}}])
    text = text_of(reader)
    for value in ("None", "42", "False", "{'key': '<value>'}"):
        assert value in text


@pytest.mark.parametrize("column_count", [2, 5, 10])
def test_table_backgrounds_stay_inside_drawable_frame(render_pdf, column_count):
    reader = render_pdf([{"table": {"columns": [f"H{i}" for i in range(column_count)],
                                    "rows": [["Value"] * column_count]}}])
    page = reader.pages[0]
    rectangles = []

    def collect_rectangles(operator, args, cm, tm):
        if operator == b"re":
            x, y, width, height = map(float, args)
            # Transform each rectangle corner into page coordinates.
            rectangles.extend((cm[0] * px + cm[2] * py + cm[4],
                               cm[1] * px + cm[3] * py + cm[5])
                              for px in (x, x + width) for py in (y, y + height))

    page.extract_text(visitor_operand_before=collect_rectangles)
    assert rectangles
    # SimpleDocTemplate's frame adds 6pt padding inside each 0.9in margin.
    inset = 0.9 * 72 + 6
    for x, y in rectangles:
        assert inset - 0.01 <= x <= float(page.mediabox.width) - inset + 0.01
        assert inset - 0.01 <= y <= float(page.mediabox.height) - inset + 0.01

