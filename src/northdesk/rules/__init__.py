"""Business rules. Pure functions: no FastAPI, Temporal or LLM imports."""

from northdesk.rules.validation import check_decision, enforce

__all__ = ["check_decision", "enforce"]
