"""Agent configuration constants."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelDef:
    id: str
    label: str
    api_model: str
    supports_thinking: bool


MODELS: dict[str, ModelDef] = {
    "low": ModelDef(id="low", label="Low", api_model="claude-haiku-4-5", supports_thinking=False),
    "medium": ModelDef(id="medium", label="Medium", api_model="claude-sonnet-4-6", supports_thinking=True),
    "high": ModelDef(id="high", label="High", api_model="claude-opus-4-6", supports_thinking=True),
}

DEFAULT_MODEL = "medium"
MODEL = MODELS[DEFAULT_MODEL].api_model


def get_model(model_id: str) -> ModelDef:
    mid = (model_id or DEFAULT_MODEL).lower().strip()
    if mid not in MODELS:
        raise ValueError(f"Unknown model '{model_id}' — available: {', '.join(MODELS)}")
    return MODELS[mid]


MAX_TOKENS = 32768
MAX_TURNS = 25
TOKEN_BUDGET = 50000       # UI pie chart fills toward this limit
