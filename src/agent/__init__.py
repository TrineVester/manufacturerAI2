"""LLM agent package — design and circuit agents."""

from .core import AgentEvent, DesignAgent, CircuitAgent
from .tools import DESIGN_TOOLS, CIRCUIT_TOOLS
from .prompt import (
    catalog_summary,
    build_design_prompt,
    build_circuit_prompt,
    build_circuit_user_prompt,
)
from .messages import serialize_content, sanitize_messages, prune_messages, strip_thinking_blocks
from .config import MODEL, MAX_TOKENS, MAX_TURNS, TOKEN_BUDGET, MODELS, get_model

__all__ = [
    # Core
    "AgentEvent",
    "DesignAgent",
    "CircuitAgent",
    # Tools
    "DESIGN_TOOLS",
    "CIRCUIT_TOOLS",
    # Prompts
    "catalog_summary",
    "build_design_prompt",
    "build_circuit_prompt",
    "build_circuit_user_prompt",
    # Messages
    "serialize_content",
    "sanitize_messages",
    "prune_messages",
    # Config
    "MODEL",
    "MAX_TOKENS",
    "MAX_TURNS",
    "TOKEN_BUDGET",
]
