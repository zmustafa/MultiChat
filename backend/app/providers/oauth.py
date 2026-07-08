from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx
from sqlalchemy.orm import Session as DbSession

from ..crypto import decrypt, encrypt
from ..models import Provider

# ----------------------------------------------------------------------------
# Public client constants (per brief §6.9 / §6.10 / §6.11)
# ----------------------------------------------------------------------------

CHATGPT = {
    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    "authorize": "https://auth.openai.com/oauth/authorize",
    "token": "https://auth.openai.com/oauth/token",
    "redirect_uri": "http://localhost:1455/auth/callback",
    "scope": "openid profile email offline_access",
    "port": 1455,
}

CLAUDE = {
    "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    "authorize": "https://claude.ai/oauth/authorize",
    "token": "https://console.anthropic.com/v1/oauth/token",
    "redirect_uri": "https://console.anthropic.com/oauth/code/callback",
    "scope": "org:create_api_key user:profile user:inference",
}

COPILOT = {
    "client_id": "Iv1.b507a08c87ecfe98",  # GitHub Copilot public device-flow client id
    "device_code": "https://github.com/login/device/code",
    "access_token": "https://github.com/login/oauth/access_token",
    "copilot_token": "https://api.github.com/copilot_internal/v2/token",
    "scope": "read:user",
    "default_api_base": "https://api.githubcopilot.com",
}

# Headers GitHub expects when minting a Copilot bearer from a gho_ token.
COPILOT_EDITOR_HEADERS = {
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.0",
    "User-Agent": "GitHubCopilotChat/0.22.0",
    "Accept": "application/json",
}

REFRESH_SKEW = 120  # seconds — refresh a little before expiry to avoid races


# ----------------------------------------------------------------------------
# In-memory state (never persisted)
# ----------------------------------------------------------------------------


@dataclass
class PendingFlow:
    flavor: str
    verifier: str = ""
    state: str = ""
    device_code: str = ""
    captured_code: str | None = None
    captured_state: str | None = None
    error: str | None = None
    server: Any = None


_pending: dict[str, PendingFlow] = {}
# provider_id -> {"token": str, "expires_at": int, "last_used": int}  (Copilot short-lived)
_short_lived: dict[str, dict[str, Any]] = {}
# provider_id -> asyncio.Lock, so N concurrent lanes sharing one Copilot provider don't all
# re-mint a bearer at the same moment (a thundering herd GitHub intermittently 403-throttles).
_mint_locks: dict[str, asyncio.Lock] = {}
# provider_id -> scheduled TimerHandle that proactively re-mints the Copilot bearer shortly
# before it expires, so the first request after an idle period never pays the mint latency.
_prerefresh_handles: dict[str, Any] = {}
COPILOT_PREREFRESH_LEAD = 150  # seconds before expiry to proactively re-mint (~2.5 min)
COPILOT_IDLE_STOP = 3600  # stop the background refresh loop if unused for this long


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _make_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _authorize_url(base: str, params: dict[str, str]) -> str:
    """Build an authorize URL with RFC 3986 encoding.

    Anthropic's /oauth/authorize reads the query literally and does NOT treat ``+`` as a
    space, so a ``+``-encoded scope (the default from urlencode) is rejected as an
    "Invalid request format". Encode spaces as ``%20`` instead.
    """
    return f"{base}?{urlencode(params, quote_via=quote, safe='')}"


def _jwt_claims(token: str | None) -> dict[str, Any]:
    """Best-effort decode of a JWT payload (no signature verification)."""
    if not token or token.count(".") < 2:
        return {}
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:  # noqa: BLE001
        return {}


def _extract_chatgpt_account_id(access_token: str | None, id_token: str | None) -> str:
    """Pull the ChatGPT account id from the access or id token claims."""
    for tok in (access_token, id_token):
        claims = _jwt_claims(tok)
        if not claims:
            continue
        auth = claims.get("https://api.openai.com/auth") or {}
        account_id = (
            (auth.get("chatgpt_account_id") if isinstance(auth, dict) else None)
            or (auth.get("organization_id") if isinstance(auth, dict) else None)
            or claims.get("account_id")
            or ""
        )
        if account_id:
            return account_id
    return ""


async def _mint_copilot_token(client_id: str, gh_token: str) -> tuple[str, str, int]:
    """Exchange a long-lived gho_ token for a short-lived Copilot bearer.

    Returns (bearer, api_base_url, expires_at_epoch).

    GitHub's copilot_internal/v2/token endpoint intermittently returns a transient 403
    ("Resource not accessible by integration") or 429/5xx under load, so retry a few times
    with a short backoff before surfacing the error to the user.
    """
    last_status = 0
    last_text = ""
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(3):
            try:
                resp = await client.get(
                    COPILOT["copilot_token"],
                    headers={
                        "Authorization": f"token {gh_token}",
                        **COPILOT_EDITOR_HEADERS,
                    },
                )
            except httpx.HTTPError as exc:  # network blip / timeout
                last_status, last_text = 0, str(exc)[:200]
                await asyncio.sleep(0.4 * (attempt + 1))
                continue
            if resp.status_code == 200:
                body = resp.json()
                bearer = body.get("token", "")
                api_base = (body.get("endpoints") or {}).get("api") or COPILOT[
                    "default_api_base"
                ]
                expires_at = int(body.get("expires_at") or (time.time() + 20 * 60))
                return bearer, api_base, expires_at
            last_status, last_text = resp.status_code, resp.text[:200]
            # Only transient statuses are worth retrying; a hard 401/404 won't improve.
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                await asyncio.sleep(0.4 * (attempt + 1))
                continue
            break
    raise RuntimeError(
        "Could not get a Copilot token — does this GitHub account have an active "
        f"Copilot subscription? ({last_status}: {last_text})"
    )


# ----------------------------------------------------------------------------
# Loopback capture server (ChatGPT flavor)
# ----------------------------------------------------------------------------


async def _start_loopback(provider_id: str, port: int) -> None:
    flow = _pending[provider_id]

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            request = line.decode(errors="ignore")
            # GET /auth/callback?code=...&state=... HTTP/1.1
            path = request.split(" ")[1] if len(request.split(" ")) > 1 else ""
            if "?" in path:
                query = path.split("?", 1)[1]
                params = dict(
                    p.split("=", 1) for p in query.split("&") if "=" in p
                )
                flow.captured_code = params.get("code")
                flow.captured_state = params.get("state")
            body = (
                "<html><body><h2>Authentication complete.</h2>"
                "<p>You can close this window and return to MultiChat.</p>"
                "</body></html>"
            )
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
                + body.encode()
            )
            await writer.drain()
        finally:
            writer.close()

    try:
        server = await asyncio.start_server(handle, "127.0.0.1", port)
        flow.server = server
        asyncio.create_task(server.serve_forever())
    except OSError as exc:  # port busy
        flow.error = f"Could not bind loopback port {port}: {exc}"


def _close_loopback(provider_id: str) -> None:
    flow = _pending.get(provider_id)
    if flow and flow.server is not None:
        try:
            flow.server.close()
        except Exception:
            pass
        flow.server = None


# ----------------------------------------------------------------------------
# Start / poll / complete
# ----------------------------------------------------------------------------


async def start_flow(provider: Provider) -> dict[str, Any]:
    flavor = (provider.extra_json or {}).get("oauth_flavor")
    if provider.provider_type == "github_copilot":
        flavor = "copilot"
    elif provider.provider_type == "openai":
        flavor = flavor or "chatgpt"
    elif provider.provider_type == "anthropic":
        flavor = flavor or "claude"

    if flavor == "chatgpt":
        return await _start_chatgpt(provider)
    if flavor == "claude":
        return _start_claude(provider)
    if flavor == "copilot":
        return await _start_copilot(provider)
    raise ValueError(f"Unsupported oauth flavor: {flavor}")


async def _start_chatgpt(provider: Provider) -> dict[str, Any]:
    verifier, challenge = _make_pkce()
    state = _b64url(secrets.token_bytes(16))
    _pending[provider.id] = PendingFlow(flavor="chatgpt", verifier=verifier, state=state)
    await _start_loopback(provider.id, CHATGPT["port"])
    params = {
        "response_type": "code",
        "client_id": _client_id(provider, CHATGPT["client_id"]),
        "redirect_uri": CHATGPT["redirect_uri"],
        "scope": CHATGPT["scope"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return {
        "authorize_url": _authorize_url(CHATGPT["authorize"], params),
        "flavor": "chatgpt",
        "mode": "loopback",
    }


def _start_claude(provider: Provider) -> dict[str, Any]:
    verifier, challenge = _make_pkce()
    # Anthropic echoes `state` back as the callback fragment (code#state) and validates
    # it as a PKCE-format value (43-128 unreserved chars). A short random state is
    # rejected as "Invalid request format", so state must equal the code_verifier —
    # matching the canonical Claude Code OAuth flow.
    state = verifier
    _pending[provider.id] = PendingFlow(flavor="claude", verifier=verifier, state=state)
    params = {
        "code": "true",
        "client_id": _client_id(provider, CLAUDE["client_id"]),
        "response_type": "code",
        "redirect_uri": CLAUDE["redirect_uri"],
        "scope": CLAUDE["scope"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return {
        "authorize_url": _authorize_url(CLAUDE["authorize"], params),
        "flavor": "claude",
        "mode": "paste",
    }


async def _start_copilot(provider: Provider) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            COPILOT["device_code"],
            headers={"Accept": "application/json"},
            data={"client_id": _client_id(provider, COPILOT["client_id"]),
                  "scope": COPILOT["scope"]},
        )
        resp.raise_for_status()
        data = resp.json()
    flow = PendingFlow(flavor="copilot")
    flow.device_code = data["device_code"]
    _pending[provider.id] = flow
    return {
        "flavor": "copilot",
        "mode": "device",
        "user_code": data["user_code"],
        "verification_uri": data["verification_uri"],
        "interval": data.get("interval", 5),
        "expires_in": data.get("expires_in", 900),
    }


def _client_id(provider: Provider, default: str) -> str:
    return (provider.extra_json or {}).get("client_id") or default


async def poll_flow(provider: Provider, db: DbSession) -> dict[str, Any]:
    flow = _pending.get(provider.id)
    if not flow:
        return {"status": "error", "detail": "No pending flow"}
    if flow.error:
        return {"status": "error", "detail": flow.error}

    if flow.flavor == "chatgpt":
        if not flow.captured_code:
            return {"status": "pending", "detail": "Waiting for browser callback"}
        if flow.captured_state != flow.state:
            return {"status": "error", "detail": "State mismatch"}
        try:
            await _exchange_chatgpt(provider, db, flow.captured_code)
            _close_loopback(provider.id)
            _pending.pop(provider.id, None)
            return {"status": "authorized", "detail": "Connected"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "detail": str(exc)}

    if flow.flavor == "copilot":
        try:
            done = await _poll_copilot(provider, db, flow)
            if done:
                _pending.pop(provider.id, None)
                return {"status": "authorized", "detail": "Connected"}
            return {"status": "pending", "detail": "Authorization pending"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "detail": str(exc)}

    return {"status": "pending", "detail": "Use complete endpoint for paste flow"}


async def complete_flow(
    provider: Provider, db: DbSession, code: str, state: str | None
) -> dict[str, Any]:
    flow = _pending.get(provider.id)
    if not flow:
        return {"status": "error", "detail": "No pending flow — start sign-in again."}

    code = (code or "").strip()
    # Accept a full pasted callback URL (ChatGPT loopback fallback): extract code+state.
    if "://" in code or code.startswith("localhost") or code.startswith("/"):
        try:
            qs = parse_qs(urlparse(code if "://" in code else f"http://{code}").query)
            code = (qs.get("code") or [code])[0]
            if state is None and qs.get("state"):
                state = qs["state"][0]
        except Exception:  # noqa: BLE001
            pass
    # Claude console returns the code formatted as <code>#<state>.
    if "#" in code and state is None:
        code, state = code.split("#", 1)
    if flow.state and state and flow.state != state:
        return {"status": "error", "detail": "State mismatch — start sign-in again."}
    try:
        if flow.flavor == "claude":
            await _exchange_claude(provider, db, code, state or flow.state)
        elif flow.flavor == "chatgpt":
            await _exchange_chatgpt(provider, db, code)
            _close_loopback(provider.id)
        else:
            return {"status": "error", "detail": "Unsupported flavor for complete"}
        _pending.pop(provider.id, None)
        return {"status": "authorized", "detail": "Connected"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": str(exc)}


# ----------------------------------------------------------------------------
# Token exchanges
# ----------------------------------------------------------------------------


def _store_tokens(
    provider: Provider,
    db: DbSession,
    access: str,
    refresh: str | None,
    expires_in: int | None,
) -> None:
    provider.oauth_access_token_encrypted = encrypt(access)
    if refresh:
        provider.oauth_refresh_token_encrypted = encrypt(refresh)
    if expires_in:
        provider.oauth_expires_at = int(time.time()) + int(expires_in)
    db.add(provider)
    db.commit()


async def _exchange_chatgpt(provider: Provider, db: DbSession, code: str) -> None:
    flow = _pending[provider.id]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            CHATGPT["token"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CHATGPT["redirect_uri"],
                "client_id": _client_id(provider, CHATGPT["client_id"]),
                "code_verifier": flow.verifier,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    _store_tokens(
        provider, db, data["access_token"], data.get("refresh_token"),
        data.get("expires_in"),
    )
    # Decode the access/id token to read the ChatGPT account id (needed as a header
    # on every Responses-API call).
    account_id = _extract_chatgpt_account_id(
        data.get("access_token"), data.get("id_token")
    )
    if account_id:
        extra = dict(provider.extra_json or {})
        extra["chatgpt_account_id"] = account_id
        provider.extra_json = extra
        db.add(provider)
        db.commit()


async def _exchange_claude(
    provider: Provider, db: DbSession, code: str, state: str
) -> None:
    flow = _pending[provider.id]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            CLAUDE["token"],
            headers={"Content-Type": "application/json"},
            json={
                "grant_type": "authorization_code",
                "code": code,
                "state": state,
                "client_id": _client_id(provider, CLAUDE["client_id"]),
                "redirect_uri": CLAUDE["redirect_uri"],
                "code_verifier": flow.verifier,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    _store_tokens(
        provider, db, data["access_token"], data.get("refresh_token"),
        data.get("expires_in"),
    )


async def _poll_copilot(
    provider: Provider, db: DbSession, flow: PendingFlow
) -> bool:
    client_id = _client_id(provider, COPILOT["client_id"])
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            COPILOT["access_token"],
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id,
                "device_code": flow.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        data = resp.json()
    if data.get("error") in ("authorization_pending", "slow_down"):
        return False
    if "access_token" not in data:
        raise RuntimeError(data.get("error_description") or data.get("error") or "failed")

    gh_token = data["access_token"]
    # Mint a Copilot bearer now to validate the subscription and learn the per-account
    # API base URL; the long-lived gho_ token is what we persist (bearer is re-minted).
    bearer, api_base, expires_at = await _mint_copilot_token(client_id, gh_token)
    _store_tokens(provider, db, gh_token, None, None)
    extra = dict(provider.extra_json or {})
    extra["copilot_api_base"] = api_base
    provider.extra_json = extra
    db.add(provider)
    db.commit()
    _short_lived[provider.id] = {
        "token": bearer,
        "expires_at": expires_at,
        "last_used": int(time.time()),
    }
    _schedule_copilot_prerefresh(provider.id, expires_at)
    return True


# ----------------------------------------------------------------------------
# get_valid_token — used by adapters
# ----------------------------------------------------------------------------


async def _refresh(provider: Provider, db: DbSession) -> None:
    flavor = (provider.extra_json or {}).get("oauth_flavor")
    if provider.provider_type == "openai":
        flavor = flavor or "chatgpt"
    elif provider.provider_type == "anthropic":
        flavor = flavor or "claude"
    refresh_token = decrypt(provider.oauth_refresh_token_encrypted)
    if not refresh_token:
        raise RuntimeError("No refresh token; reconnect required")

    if flavor == "chatgpt":
        url, payload, headers = (
            CHATGPT["token"],
            {"grant_type": "refresh_token", "refresh_token": refresh_token,
             "client_id": _client_id(provider, CHATGPT["client_id"])},
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=headers, data=payload)
    else:  # claude
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CLAUDE["token"],
                headers={"Content-Type": "application/json"},
                json={"grant_type": "refresh_token", "refresh_token": refresh_token,
                      "client_id": _client_id(provider, CLAUDE["client_id"])},
            )
    resp.raise_for_status()
    data = resp.json()
    _store_tokens(
        provider, db, data["access_token"],
        data.get("refresh_token", refresh_token), data.get("expires_in"),
    )


async def get_valid_token(provider: Provider, db: DbSession) -> str:
    """Return a usable bearer credential for a provider."""
    if provider.auth_method != "oauth":
        key = decrypt(provider.api_key_encrypted)
        return key or ""

    # Copilot: exchange the stored GitHub token for a short-lived Copilot token.
    if provider.provider_type == "github_copilot":
        return await _get_copilot_token(provider, db)

    now = int(time.time())
    if provider.oauth_expires_at and provider.oauth_expires_at - now <= REFRESH_SKEW:
        await _refresh(provider, db)
    token = decrypt(provider.oauth_access_token_encrypted)
    if not token:
        raise RuntimeError("Provider not connected; reconnect required")
    return token


async def _get_copilot_token(provider: Provider, db: DbSession) -> str:
    now = int(time.time())
    cached = _short_lived.get(provider.id)
    if cached and cached["expires_at"] - now > REFRESH_SKEW:
        cached["last_used"] = now
        return cached["token"]
    # Serialize minting per provider: when a bearer expires, all lanes sharing this Copilot
    # provider hit this at once — without the lock they'd each fire a mint request and GitHub
    # 403-throttles the burst. The first waiter mints; the rest reuse the fresh cached token.
    lock = _mint_locks.get(provider.id)
    if lock is None:
        lock = _mint_locks[provider.id] = asyncio.Lock()
    async with lock:
        now = int(time.time())
        cached = _short_lived.get(provider.id)
        if cached and cached["expires_at"] - now > REFRESH_SKEW:
            cached["last_used"] = now
            return cached["token"]
        gh_token = decrypt(provider.oauth_access_token_encrypted) or decrypt(
            provider.api_key_encrypted
        )
        if not gh_token:
            raise RuntimeError("GitHub token missing; reconnect Copilot")
        client_id = _client_id(provider, COPILOT["client_id"])
        bearer, api_base, expires_at = await _mint_copilot_token(client_id, gh_token)
        _short_lived[provider.id] = {
            "token": bearer,
            "expires_at": expires_at,
            "last_used": now,
        }
        # Keep the per-account API base fresh.
        if api_base and (provider.extra_json or {}).get("copilot_api_base") != api_base:
            extra = dict(provider.extra_json or {})
            extra["copilot_api_base"] = api_base
            provider.extra_json = extra
            db.add(provider)
            db.commit()
        _schedule_copilot_prerefresh(provider.id, expires_at)
        return bearer


def _schedule_copilot_prerefresh(provider_id: str, expires_at: int) -> None:
    """(Re)arm a background re-mint ~2.5 min before the bearer expires, so the cache stays
    warm and the next request never blocks on minting. No-op if there's no running loop."""
    old = _prerefresh_handles.pop(provider_id, None)
    if old is not None:
        old.cancel()
    delay = max(5, expires_at - int(time.time()) - COPILOT_PREREFRESH_LEAD)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    _prerefresh_handles[provider_id] = loop.call_later(
        delay,
        lambda: asyncio.create_task(_prerefresh_copilot(provider_id)),
    )


async def _prerefresh_copilot(provider_id: str) -> None:
    """Force a fresh Copilot bearer for a provider that's still being used, then reschedule."""
    _prerefresh_handles.pop(provider_id, None)
    cached = _short_lived.get(provider_id)
    # Stop the loop if this provider hasn't been used recently (avoid perpetual minting).
    if cached and int(time.time()) - cached.get("last_used", 0) > COPILOT_IDLE_STOP:
        return
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        provider = db.get(Provider, provider_id)
        if not provider or provider.provider_type != "github_copilot":
            return
        gh_token = decrypt(provider.oauth_access_token_encrypted) or decrypt(
            provider.api_key_encrypted
        )
        if not gh_token:
            return
        lock = _mint_locks.get(provider_id)
        if lock is None:
            lock = _mint_locks[provider_id] = asyncio.Lock()
        async with lock:
            client_id = _client_id(provider, COPILOT["client_id"])
            bearer, api_base, expires_at = await _mint_copilot_token(client_id, gh_token)
            _short_lived[provider_id] = {
                "token": bearer,
                "expires_at": expires_at,
                "last_used": (cached or {}).get("last_used", int(time.time())),
            }
            if api_base and (provider.extra_json or {}).get("copilot_api_base") != api_base:
                extra = dict(provider.extra_json or {})
                extra["copilot_api_base"] = api_base
                provider.extra_json = extra
                db.add(provider)
                db.commit()
        _schedule_copilot_prerefresh(provider_id, expires_at)
    except Exception:  # noqa: BLE001 — background best-effort; lazy mint remains the fallback
        pass
    finally:
        db.close()


def oauth_connected(provider: Provider) -> bool:
    return bool(
        provider.oauth_access_token_encrypted or provider.api_key_encrypted
    ) if provider.auth_method == "oauth" else False


def disconnect(provider: Provider, db: DbSession) -> None:
    provider.oauth_access_token_encrypted = None
    provider.oauth_refresh_token_encrypted = None
    provider.oauth_expires_at = None
    _short_lived.pop(provider.id, None)
    _pending.pop(provider.id, None)
    db.add(provider)
    db.commit()
