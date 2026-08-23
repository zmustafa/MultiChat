from __future__ import annotations

from collections import OrderedDict

import pytest

from app.providers import openai_provider


def test_client_cache_reuses_credentials_and_replaces_rotated_ones(monkeypatch) -> None:
    monkeypatch.setattr(openai_provider, "_client_cache", OrderedDict())
    closed = []
    monkeypatch.setattr(openai_provider, "_schedule_close", closed.append)

    original = object()
    assert openai_provider._cached_client(
        "provider-id", "credential-a", lambda: original
    ) is original
    assert openai_provider._cached_client(
        "provider-id",
        "credential-a",
        lambda: pytest.fail("same credential should reuse the client"),
    ) is original

    replacement = object()
    assert openai_provider._cached_client(
        "provider-id", "credential-b", lambda: replacement
    ) is replacement
    assert closed == [original]
