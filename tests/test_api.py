"""API tests: the real FastAPI app against Temporal's test server and a worker with offline models."""

import asyncio
import uuid

import httpx
import pytest

from northdesk.activities import load_context
from northdesk.api.app import create_app
from northdesk.config import Settings
from northdesk.worker import build_worker
from tests.fakes import FakeActivities


@pytest.fixture
async def api(env):
    queue = f"api-{uuid.uuid4()}"
    fakes = FakeActivities()
    worker = build_worker(env.client, queue, activities=[load_context, fakes.classify_ticket, fakes.execute_action])
    async with worker:
        app = create_app(client=env.client, settings=Settings(task_queue=queue, _env_file=None))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client


def ticket(ticket_id: str, customer_id: str, subject: str, body: str) -> dict:
    return {"ticket_id": ticket_id, "customer_id": customer_id, "subject": subject, "body": body}


async def wait_for(api, ticket_id: str, step: str) -> dict:
    for _ in range(200):
        body = (await api.get(f"/tickets/{ticket_id}")).json()
        if body["step"] == step:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"{ticket_id} never reached {step}: {body}")


async def test_same_ticket_twice_is_processed_once(api):
    t = ticket(f"T-{uuid.uuid4()}", "C-200", "Can't log in", "I forgot my password.")

    first = await api.post("/tickets", json=t)
    second = await api.post("/tickets", json=t)

    assert first.status_code == 202 and first.json()["duplicate"] is False
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert first.json()["workflow_id"] == second.json()["workflow_id"]
    done = await wait_for(api, t["ticket_id"], "done")
    assert done["decision"]["action"]["type"] == "reply"


async def test_refund_approval_flow(api):
    ticket_id = f"T-{uuid.uuid4()}"
    await api.post("/tickets", json=ticket(ticket_id, "C-100", "Charged twice", "Please refund the extra charge."))
    waiting = await wait_for(api, ticket_id, "awaiting_approval")
    assert waiting["decision"]["action"] == {"type": "refund", "invoice_id": "INV-1002", "amount_eur": 49.0}

    approved = await api.post(f"/tickets/{ticket_id}/approval", json={"approved": True, "approver": "alice"})

    assert approved.status_code == 200
    done = await wait_for(api, ticket_id, "done")
    assert done["result"]["action"] == "refund"


async def test_approval_errors(api):
    unknown = await api.post("/tickets/T-nope/approval", json={"approved": True, "approver": "alice"})
    assert unknown.status_code == 404
    assert (await api.get("/tickets/T-nope")).status_code == 404

    ticket_id = f"T-{uuid.uuid4()}"
    await api.post("/tickets", json=ticket(ticket_id, "C-999", "Hello", "Is my subscription active?"))
    await wait_for(api, ticket_id, "done")
    not_waiting = await api.post(f"/tickets/{ticket_id}/approval", json={"approved": True, "approver": "alice"})
    assert not_waiting.status_code == 409
    assert "already finished" in not_waiting.json()["detail"]


async def test_invalid_ticket_is_rejected(api):
    response = await api.post("/tickets", json={"ticket_id": "T-x"})
    assert response.status_code == 422
