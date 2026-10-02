"""The one place that talks to an LLM.

Everything else calls :func:`chat` or :func:`chat_json`, so the provider can be
swapped with two env vars and tests can replace the whole thing with
:func:`set_fake` - no network, no key.
"""

from __future__ import annotations

import functools
import json
import os
import re
from typing import Any, Callable, Optional

from dotenv import load_dotenv

load_dotenv()

FakeLLM = Callable[[str, str], str]  # (system, user) -> raw text
_fake: Optional[FakeLLM] = None


class LLMError(Exception):
    """The model could not be reached, or returned something unusable."""


def set_fake(fn: Optional[FakeLLM]) -> None:
    """Install (or clear) a deterministic stand-in. Used by the eval suite."""
    global _fake
    _fake = fn


def is_faked() -> bool:
    return _fake is not None


# Providers that speak the OpenAI chat-completions API. `nvidia` is NVIDIA NIM,
# which is OpenAI-compatible and only needs a different base URL and key name.
OPENAI_COMPATIBLE = {
    "openai": {"key": "OPENAI_API_KEY", "base_url": None, "model": "gpt-4o-mini"},
    "nvidia": {
        "key": "NVIDIA_API_KEY",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "meta/llama-3.3-70b-instruct",
    },
}


def _timeout() -> float:
    """Per-call timeout. Free-tier endpoints queue, so the default is generous."""
    try:
        return float(os.getenv("LLM_TIMEOUT", "120"))
    except ValueError:
        return 120.0


@functools.lru_cache(maxsize=4)
def _model(provider: str, name: str, temperature: float, base_url: Optional[str], api_key: str):
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=name, temperature=temperature, timeout=_timeout(), max_retries=1, api_key=api_key
        )
    if provider in OPENAI_COMPATIBLE:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=name, temperature=temperature, timeout=_timeout(), max_retries=1,
            base_url=base_url, api_key=api_key,
        )
    raise LLMError(
        f"unsupported LLM_PROVIDER={provider!r} "
        f"(use 'anthropic', 'fake', or one of {sorted(OPENAI_COMPATIBLE)})"
    )


def _defaults() -> tuple[str, str, Optional[str], str]:
    """(provider, model, base_url, key env var) from the environment."""
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    spec = OPENAI_COMPATIBLE.get(provider)
    fallback = spec["model"] if spec else "claude-sonnet-5"
    key = spec["key"] if spec else "ANTHROPIC_API_KEY"
    base_url = os.getenv("LLM_BASE_URL", "").strip() or (spec["base_url"] if spec else None)
    return provider, os.getenv("LLM_MODEL", fallback).strip(), base_url, key


def chat(system: str, user: str, temperature: float = 0.0) -> str:
    """One turn, text in, text out."""
    if _fake is not None:
        return _fake(system, user)
    provider, name, base_url, key = _defaults()
    api_key = os.getenv(key, "")
    if not api_key:
        raise LLMError(f"{key} is not set (copy .env.example to .env)")
    try:
        response = _model(provider, name, temperature, base_url, api_key).invoke(
            [("system", system), ("human", user)]
        )
    except Exception as exc:  # provider SDKs raise their own hierarchies
        raise LLMError(f"{provider}/{name} call failed: {exc}") from exc
    content = response.content
    if isinstance(content, list):  # Anthropic returns content blocks
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def chat_json(system: str, user: str, temperature: float = 0.0) -> dict[str, Any]:
    """One turn that must return a JSON object. Raises :class:`LLMError` otherwise."""
    raw = chat(system, user, temperature)
    text = _FENCE.sub("", raw).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise LLMError(f"model did not return JSON: {raw[:200]!r}")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned malformed JSON: {raw[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise LLMError(f"model returned {type(parsed).__name__}, expected a JSON object")
    return parsed


def untrusted_block(text: str, tag: str = "user_message") -> str:
    """Wrap user text as clearly-marked data. Closing tags inside it are defanged."""
    safe = str(text).replace(f"</{tag}>", f"<_/{tag}>")
    return f"<{tag}>\n{safe}\n</{tag}>"


if __name__ == "__main__":
    provider, name, base_url, key = _defaults()
    print(f"provider={provider} model={name} base_url={base_url or 'default'} key_env={key}")
    print(chat("Reply with exactly one word.", "Say: ready"))
