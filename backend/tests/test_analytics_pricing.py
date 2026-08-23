from __future__ import annotations

import pytest

from app.routers.analytics import _rate


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5.6-sol", (4.00, 20.00)),
        ("gpt-5.6-cyber", (12.50, 75.00)),
        ("gpt-5.3-codex", (1.75, 14.00)),
        ("gpt-4o-2024-05-13", (5.00, 15.00)),
        ("gpt-4.1-2025-04-14", (2.00, 8.00)),
        ("gpt-4-0613", (30.00, 60.00)),
        ("o1-pro", (150.00, 600.00)),
        ("gpt-3.5-turbo-1106", (1.00, 2.00)),
        ("claude-sonnet-5", (2.00, 10.00)),
        ("claude-sonnet-4.6", (3.00, 15.00)),
        ("gemini-3.7-flash", (0.75, 3.75)),
        ("gemini-3.6-flash", (0.75, 3.75)),
        ("gemini-3.5-flash", (1.50, 9.00)),
    ],
)
def test_current_standard_model_rates(
    model: str,
    expected: tuple[float, float],
) -> None:
    assert _rate(model) == expected
