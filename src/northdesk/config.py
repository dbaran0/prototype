"""All settings come from environment variables (see `.env.example`)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "fake" runs both models as offline rule-based stand-ins; "real" calls OpenRouter.
    llm_mode: Literal["fake", "real"] = "fake"
    openrouter_api_key: str = ""
    agent_model: str = "openai/gpt-6-luna"
    classifier_model: str = "~typesafe/jev-latest"

    # "agent" (default): the tool-calling agent decides every ticket.
    # "jev_first" (opt-in): Jev decides when confident, the agent handles the rest.
    decider: Literal["agent", "jev_first"] = "agent"
    jev_min_confidence: float = 0.8
    approval_timeout_seconds: int = 24 * 60 * 60

    helpdesk_failure_rate: float = 0.2
    # SQLite file with completed Helpdesk actions; shared by all workers so retries are never doubled.
    helpdesk_store: str = "helpdesk.sqlite3"

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "tickets"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
