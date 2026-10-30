import os

# Tests never touch the network or need a key: force the offline models before anything is imported.
os.environ["LLM_MODE"] = "fake"
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

import pytest  # noqa: E402
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402


@pytest.fixture(scope="session")
async def env():
    # Temporal's in-memory test server; time skipping makes the 24h approval wait instant.
    async with await WorkflowEnvironment.start_time_skipping(plugins=[PydanticAIPlugin()]) as env:
        yield env
