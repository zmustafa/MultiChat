from __future__ import annotations

import hashlib

from app.providers.openai_provider import _credential_fingerprint


def test_credential_fingerprint_is_process_keyed() -> None:
    credential = "example-provider-secret"
    fingerprint = _credential_fingerprint(credential)

    assert fingerprint == _credential_fingerprint(credential)
    assert fingerprint != _credential_fingerprint("another-provider-secret")
    assert fingerprint != hashlib.sha256(credential.encode()).hexdigest()
    assert credential not in fingerprint
