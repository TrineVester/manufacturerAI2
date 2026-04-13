"""Agent package — LLM-driven design + circuit agents using Anthropic API."""

from .config import MODEL, MAX_TOKENS, THINKING_BUDGET, MAX_TURNS, TOKEN_BUDGET
from .tools import TOOLS, DESIGN_TOOLS, CIRCUIT_TOOLS
from .prompt import _build_system_prompt, _build_design_prompt, _build_circuit_prompt, _catalog_summary
from .messages import _serialize_content, _sanitize_messages, _prune_messages
from .core import DesignAgent, CircuitAgent, AgentEvent

__all__ = [
    # Config
    "MODEL", "MAX_TOKENS", "THINKING_BUDGET", "MAX_TURNS", "TOKEN_BUDGET",
    # Tools & prompt
    "TOOLS", "DESIGN_TOOLS", "CIRCUIT_TOOLS",
    "_build_system_prompt", "_build_design_prompt", "_build_circuit_prompt",
    "_catalog_summary",
    # Messages
    "_serialize_content", "_sanitize_messages", "_prune_messages",
    # Agents
    "DesignAgent", "CircuitAgent", "AgentEvent",
]
