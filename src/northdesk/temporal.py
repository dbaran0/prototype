"""Temporal client shared by the API and the worker."""

import asyncio
import logging

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client

from northdesk.config import Settings

log = logging.getLogger(__name__)


async def connect(settings: Settings, attempts: int = 30) -> Client:
    """Connect to Temporal, waiting for it to come up (it starts slower than our containers)."""
    for attempt in range(1, attempts + 1):
        try:
            return await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace, plugins=[PydanticAIPlugin()]
            )
        except RuntimeError as exc:
            if attempt == attempts:
                raise
            log.warning("temporal not reachable yet", extra={"attempt": attempt, "error": str(exc)})
            await asyncio.sleep(2)
    raise AssertionError("unreachable")
