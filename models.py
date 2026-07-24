"""
Model registry for the multi-provider setup.

Every model the bot can use is described here with the info LiteLLM needs to call
it (the litellm model string, which env var holds its API key, an optional custom
api_base) plus a `supports_vision` flag so the agent knows whether it may attach
images. Adding a new provider/model is just a new entry in MODELS.
"""
import os
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSpec:
    key: str                 # short stable id used in settings & callback data
    label: str               # human label shown in the /model picker
    litellm_model: str       # the string passed to litellm (e.g. "gemini/gemini-3.5-flash-lite")
    api_key_env: str         # env var holding this provider's key
    supports_vision: bool    # whether images may be attached
    api_base: Optional[str] = None  # custom endpoint (for OpenAI-compatible providers like Z.ai)

    @property
    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "")

    @property
    def is_available(self) -> bool:
        """True if this model's API key is configured."""
        return bool(self.api_key)

    def call_kwargs(self) -> dict:
        """Base kwargs for litellm.acompletion for this model."""
        kw = {"model": self.litellm_model, "api_key": self.api_key}
        if self.api_base:
            kw["api_base"] = self.api_base
        return kw


# The registry. Order here is the order shown in the /model picker.
MODELS: Dict[str, ModelSpec] = {
    "glm-4.7-flash": ModelSpec(
        key="glm-4.7-flash",
        label="GLM-4.7 Flash · text",
        # Z.ai exposes an OpenAI-compatible endpoint, so route it through litellm's
        # openai provider with a custom base rather than a second SDK.
        litellm_model="openai/glm-4.7-flash",
        api_key_env="ZAI_API_KEY",
        api_base="https://api.z.ai/api/paas/v4",
        supports_vision=False,
    ),
    "gemini-3.5-flash-lite": ModelSpec(
        key="gemini-3.5-flash-lite",
        label="Gemini 3.5 Flash-Lite · vision",
        litellm_model="gemini/gemini-3.5-flash-lite",
        api_key_env="GEMINI_API_KEY",
        supports_vision=True,
    ),
    "gemini-3.1-flash-lite": ModelSpec(
        key="gemini-3.1-flash-lite",
        label="Gemini 3.1 Flash-Lite · vision",
        litellm_model="gemini/gemini-3.1-flash-lite",
        api_key_env="GEMINI_API_KEY",
        supports_vision=True,
    ),
}


def all_models() -> List[ModelSpec]:
    """Every registered model, in registry order."""
    return list(MODELS.values())


def get_spec(key: Optional[str]) -> Optional[ModelSpec]:
    """Look up a model spec by key."""
    if not key:
        return None
    return MODELS.get(key)


def default_model_key() -> str:
    """
    The fallback model key. Uses config.MODEL_NAME if it names a registered model,
    otherwise the first registered model.
    """
    if config.MODEL_NAME in MODELS:
        return config.MODEL_NAME
    return next(iter(MODELS))


def resolve_spec(key: Optional[str]) -> ModelSpec:
    """
    Resolve a usable spec: the requested model if it exists and its key is set,
    otherwise fall back to any available model, otherwise the default (so callers
    always get *something* and errors surface at call time with a clear message).
    """
    spec = get_spec(key)
    if spec and spec.is_available:
        return spec
    if spec and not spec.is_available:
        logger.warning(f"Model '{key}' selected but {spec.api_key_env} is not set; looking for a fallback.")
    for candidate in MODELS.values():
        if candidate.is_available:
            return candidate
    # Nothing has a key configured — return the requested/default so the call
    # fails with a clear provider error instead of silently doing nothing.
    return spec or MODELS[default_model_key()]
