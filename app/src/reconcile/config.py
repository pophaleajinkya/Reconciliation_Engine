"""Runtime configuration loaded from environment variables.

The LLM client is provider-agnostic (anything that speaks the OpenAI
chat-completions API works). Two providers are supported out of the box:

* **OpenRouter** (default) — a marketplace that proxies Llama 4 Scout and
  other models with no daily token cap.
* **Groq** — fast, but rate-limited on the free tier.

Set `LLM_PROVIDER=openrouter` or `LLM_PROVIDER=groq` in `.env`. If
`LLM_PROVIDER` is unset, we auto-detect from whichever API key is present
(`OPENROUTER_API_KEY` preferred, then legacy `GROQ_API_KEY`).

Model IDs differ by provider:
* Groq vision: `meta-llama/llama-4-scout-17b-16e-instruct`
* OpenRouter vision: `meta-llama/llama-4-scout` (or `:free`)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from app/ root if present.
_APP_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_APP_ROOT / ".env")


# Provider → sensible defaults so new users don't have to discover model IDs.
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "text_model": "meta-llama/llama-3.3-70b-instruct",
        "vision_model": "meta-llama/llama-4-scout",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "text_model": "llama-3.3-70b-versatile",
        "vision_model": "meta-llama/llama-4-scout-17b-16e-instruct",
    },
}


@dataclass(frozen=True)
class Settings:
    provider: str  # 'openrouter' | 'groq'
    api_key: str | None
    base_url: str
    text_model: str
    vision_model: str
    output_dir: Path
    app_root: Path

    # Backward-compatible alias — older code still reads `groq_api_key`.
    @property
    def groq_api_key(self) -> str | None:
        return self.api_key if self.provider == "groq" else None

    @property
    def has_llm(self) -> bool:
        return bool(self.api_key)

    @property
    def provider_display(self) -> str:
        return {"openrouter": "OpenRouter", "groq": "Groq"}.get(self.provider, self.provider)


def _resolve_provider() -> str:
    """Pick the provider from env, with sensible auto-detection."""
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit in _PROVIDER_DEFAULTS:
        return explicit
    # Auto-detect: prefer OpenRouter if its key is set.
    if os.getenv("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.getenv("GROQ_API_KEY"):
        return "groq"
    # Default to openrouter (no key yet → still boots, LLM calls degrade gracefully).
    return "openrouter"


def _resolve_api_key(provider: str) -> str | None:
    # Allow a generic LLM_API_KEY override for convenience.
    generic = os.getenv("LLM_API_KEY")
    if generic:
        return generic
    if provider == "openrouter":
        return os.getenv("OPENROUTER_API_KEY")
    if provider == "groq":
        return os.getenv("GROQ_API_KEY")
    return None


def load_settings() -> Settings:
    provider = _resolve_provider()
    defaults = _PROVIDER_DEFAULTS[provider]
    return Settings(
        provider=provider,
        api_key=_resolve_api_key(provider),
        base_url=os.getenv("LLM_BASE_URL", defaults["base_url"]),
        text_model=os.getenv(
            "LLM_TEXT_MODEL",
            # Honour the legacy GROQ_TEXT_MODEL if the user had it set.
            os.getenv("GROQ_TEXT_MODEL", defaults["text_model"]),
        ),
        vision_model=os.getenv(
            "LLM_VISION_MODEL",
            os.getenv("GROQ_VISION_MODEL", defaults["vision_model"]),
        ),
        output_dir=_APP_ROOT / "output",
        app_root=_APP_ROOT,
    )


SETTINGS = load_settings()
