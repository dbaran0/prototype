import os

# Tests never touch the network or need a key: force the offline models before anything is imported.
os.environ["LLM_MODE"] = "fake"
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

import pytest  # noqa: E402
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402

from northdesk.config import Settings  # noqa: E402

# Collection imports the API's default app; keep it from reading a developer's .env too.
Settings.model_config["env_file"] = None


@pytest.fixture(scope="session")
async def env():
    # Temporal's in-memory test server; time skipping makes the 24h approval wait instant.
    # The SDK downloads the test server once and caches it. Set TEMPORAL_TEST_SERVER_PATH to use a
    # binary you already have, so tests run with no network at all.
    existing = os.environ.get("TEMPORAL_TEST_SERVER_PATH")
    async with await WorkflowEnvironment.start_time_skipping(
        plugins=[PydanticAIPlugin()], test_server_existing_path=existing
    ) as env:
        yield env
