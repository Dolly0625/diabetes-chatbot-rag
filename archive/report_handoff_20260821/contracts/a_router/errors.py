from __future__ import annotations


class RouterDependencyError(RuntimeError):
    """LLM timeout, malformed structured output, or another dependency error."""
