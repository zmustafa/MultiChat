from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..models import Provider
from . import oauth
from .base import LLMProvider
from .chatgpt_responses import ChatGPTResponsesProvider
from .claude_provider import ClaudeProvider
from .copilot_provider import CopilotProvider
from .openai_provider import COPILOT_BASE, OpenAIProvider

# provider_types that speak the OpenAI Chat Completions API via the SDK.
_OPENAI_COMPATIBLE = {"openai", "openai_eu", "openai_compatible", "gemini", "ollama"}


def pick_default_provider(db: DbSession, user_id: str) -> Provider | None:
    """Pick the user's default provider for background AI tasks (titles, persona
    enhancement, …). Prefers the flagged default, then any provider that has a usable
    model, then whatever exists."""
    provs = db.scalars(select(Provider).where(Provider.user_id == user_id)).all()

    def _usable(p: Provider) -> bool:
        return bool(p.default_model or (p.models_json or []))

    return (
        next((p for p in provs if p.is_default and _usable(p)), None)
        or next((p for p in provs if _usable(p)), None)
        or (provs[0] if provs else None)
    )


def _resolve_model(provider: Provider, model: str | None) -> str:
    if model:
        return model
    if provider.default_model:
        return provider.default_model
    models = provider.models_json or []
    return models[0] if models else ""


async def build_provider(
    provider: Provider,
    db: DbSession,
    model: str | None = None,
) -> LLMProvider:
    """Resolve credentials and instantiate the correct LLMProvider."""
    token = await oauth.get_valid_token(provider, db)
    extra = provider.extra_json or {}
    models = list(provider.models_json or [])
    ptype = provider.provider_type
    flavor = extra.get("oauth_flavor")
    resolved = _resolve_model(provider, model)

    # ChatGPT via OAuth (Responses API).
    if ptype == "openai" and provider.auth_method == "oauth" and (flavor or "chatgpt") == "chatgpt":
        return ChatGPTResponsesProvider(
            model=resolved,
            oauth_token=token,
            account_id=extra.get("chatgpt_account_id", ""),
            base_url=provider.base_url or "",
            fallback_models=models,
        )

    # Anthropic (API key or OAuth Bearer).
    if ptype == "anthropic":
        if provider.auth_method == "oauth":
            return ClaudeProvider(
                model=resolved,
                base_url=provider.base_url or "",
                use_oauth=True,
                oauth_token=token,
            )
        return ClaudeProvider(
            model=resolved,
            api_key=token,
            base_url=provider.base_url or "",
        )

    # Azure OpenAI / Azure AI Foundry: model maps to a deployment.
    if ptype in ("azure_openai", "azure_foundry"):
        deployment = extra.get("deployment")
        dep_map = extra.get("deployment_map") or {}
        deployment = dep_map.get(resolved, deployment or resolved)
        default_api_version = (
            "2024-05-01-preview" if ptype == "azure_foundry" else "2024-10-21"
        )
        return OpenAIProvider(
            provider=ptype,
            api_key=token,
            model=deployment,
            base_url=provider.base_url or "",
            api_version=extra.get("api_version", default_api_version),
            default_headers=extra.get("headers"),
            fallback_models=models,
        )

    # GitHub Copilot: chat/completions with a fallback to the Responses API for
    # GPT-5 / o-series models that aren't served by /chat/completions.
    if ptype == "github_copilot":
        headers = {
            "Copilot-Integration-Id": extra.get("copilot_integration_id", "vscode-chat"),
            "Editor-Version": extra.get("editor_version", "vscode/1.95.0"),
            "Editor-Plugin-Version": extra.get("editor_plugin_version", "copilot-chat/0.22.0"),
            "User-Agent": extra.get("user_agent", "GitHubCopilotChat/0.22.0"),
        }
        headers.update(extra.get("headers") or {})
        # Prefer the per-account API base learned when the Copilot token was minted.
        base = provider.base_url or extra.get("copilot_api_base") or COPILOT_BASE
        return CopilotProvider(
            token=token,
            model=resolved,
            base_url=base,
            editor_headers=headers,
            fallback_models=models,
        )

    # openai, openai_compatible, gemini, ollama.
    if ptype in _OPENAI_COMPATIBLE:
        return OpenAIProvider(
            provider=ptype,
            api_key=token,
            model=resolved,
            base_url=provider.base_url or "",
            default_headers=extra.get("headers"),
            fallback_models=models,
        )

    raise ValueError(f"Unsupported provider type: {ptype}")
