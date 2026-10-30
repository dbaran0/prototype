"""Temporal worker: runs the ticket workflow and its activities."""

import asyncio
import logging

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

from northdesk.activities import classify_ticket, execute_action, load_context
from northdesk.config import get_settings
from northdesk.log import setup_logging
from northdesk.temporal import connect
from northdesk.workflow.ticket import TicketWorkflow

ACTIVITIES = [load_context, classify_ticket, execute_action]


def build_worker(client: Client, task_queue: str, activities: list | None = None) -> Worker:
    # Agent model/tool activities are added by PydanticAIPlugin from TicketWorkflow.__pydantic_ai_agents__.
    return Worker(client, task_queue=task_queue, workflows=[TicketWorkflow], activities=activities or ACTIVITIES)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    workflow.logger.workflow_info_on_message = False  # IDs are logged as fields instead
    client = await connect(settings)
    logging.getLogger(__name__).info(
        "worker started",
        extra={"task_queue": settings.task_queue, "llm_mode": settings.llm_mode, "decider": settings.decider},
    )
    await build_worker(client, settings.task_queue).run()


if __name__ == "__main__":
    asyncio.run(main())
