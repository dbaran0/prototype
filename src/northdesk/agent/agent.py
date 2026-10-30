"""System 2: the tool-calling LLM agent.

With `TemporalDurability`, every model request and every tool call runs as its own Temporal
activity, with the timeouts and retry limits below. The model is picked once, from LLM_MODE.
"""

import logging
from datetime import timedelta

from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.models import Model
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig

from northdesk import data
from northdesk.agent.fake_model import fake_model
from northdesk.agent.prompts import INSTRUCTIONS
from northdesk.config import get_settings
from northdesk.domain import AgentDecision, Customer, Invoice, KbArticle

log = logging.getLogger(__name__)


class AgentDeps(BaseModel):
    """Run context for the tools. The customer comes from the ticket, never from the model."""

    ticket_id: str
    workflow_id: str
    customer_id: str


def build_model() -> Model:
    settings = get_settings()
    if settings.llm_mode == "fake":
        return fake_model
    if not settings.openrouter_api_key:
        raise RuntimeError("LLM_MODE=real needs OPENROUTER_API_KEY")
    # Retries are Temporal's job, so the HTTP client does not retry on its own.
    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=settings.openrouter_api_key, max_retries=0)
    return OpenRouterModel(settings.agent_model, provider=OpenRouterProvider(openai_client=client))


durability = TemporalDurability(
    # Tool calls: local data lookups.
    activity_config=ActivityConfig(
        start_to_close_timeout=timedelta(seconds=10),
        retry_policy=RetryPolicy(maximum_attempts=3),
    ),
    # Model requests: slow and flaky, so a longer timeout and backoff, but still a limit.
    model_activity_config=ActivityConfig(
        start_to_close_timeout=timedelta(seconds=90),
        retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2, maximum_attempts=4),
    ),
)

support_agent = Agent(
    build_model(),
    name="support_agent",
    deps_type=AgentDeps,
    output_type=AgentDecision,
    instructions=INSTRUCTIONS,
    retries={"output": 2},  # a model answer that can't be parsed is retried twice, then the run fails
    capabilities=[durability],
)


def _log(ctx: RunContext[AgentDeps], tool: str) -> None:
    log.info(
        "agent tool call", extra={"tool": tool, "ticket_id": ctx.deps.ticket_id, "workflow_id": ctx.deps.workflow_id}
    )


@support_agent.tool
async def get_customer(ctx: RunContext[AgentDeps]) -> Customer | None:
    """The customer's name, plan and account status."""
    _log(ctx, "get_customer")
    return data.get_customer(ctx.deps.customer_id)


@support_agent.tool
async def get_invoices(ctx: RunContext[AgentDeps]) -> list[Invoice]:
    """The customer's invoices with month, amount and payment status."""
    _log(ctx, "get_invoices")
    return data.get_invoices(ctx.deps.customer_id)


@support_agent.tool
async def search_kb(ctx: RunContext[AgentDeps], query: str) -> list[KbArticle]:
    """Knowledge base articles that match a question."""
    _log(ctx, "search_kb")
    return data.search_kb(query)
