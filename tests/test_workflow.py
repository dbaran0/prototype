"""Workflow tests on Temporal's time-skipping test server.

Our activities are fakes (tests/fakes.py); the agent runs through its real Temporal activities
with the offline rule-based model, so the whole loop (tool calls included) is exercised.
"""

import asyncio
import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.client import WorkflowFailureError, WorkflowHandle, WorkflowUpdateFailedError

from northdesk.activities import load_context
from northdesk.domain import Approval, Ticket, TicketState, TicketWorkflowInput, WorkflowOptions
from northdesk.services.helpdesk import FakeHelpdesk
from northdesk.worker import build_worker
from northdesk.workflow.ticket import TicketWorkflow
from tests.fakes import FakeActivities, ScriptedRandom

TICKETS = {t["ticket_id"]: Ticket(**t) for t in json.loads(Path("examples/tickets.json").read_text())}
_workers: list[asyncio.Task] = []


@pytest.fixture(autouse=True)
async def stop_workers():
    yield
    while _workers:
        task = _workers.pop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def start(env, fakes: FakeActivities, ticket_id: str, **options) -> WorkflowHandle:
    queue = f"q-{uuid.uuid4()}"
    worker = build_worker(env.client, queue, activities=[load_context, fakes.classify_ticket, fakes.execute_action])
    _workers.append(asyncio.create_task(worker.run()))
    return await env.client.start_workflow(
        TicketWorkflow.run,
        TicketWorkflowInput(ticket=TICKETS[ticket_id], options=WorkflowOptions(**options)),
        id=f"{ticket_id}-{uuid.uuid4()}",
        task_queue=queue,
    )


async def wait_for_step(handle: WorkflowHandle, step: str) -> TicketState:
    for _ in range(200):
        state = await handle.query(TicketWorkflow.state)
        if state.step == step:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(f"never reached {step}, last: {state.step}")


async def test_password_ticket_is_answered_by_jev(env):
    fakes = FakeActivities()
    state = await (await start(env, fakes, "T-1")).result()

    assert state.step == "done"
    assert state.decision.decided_by == "jev"
    assert state.decision.action.type == "reply"
    assert state.decision.action.article_ids == ["KB-1"]
    assert state.result.action == "reply"


async def test_refund_approved_is_issued_once_even_if_the_response_is_lost(env):
    # Second random draw fails *after* the refund was applied, so the activity is retried.
    fakes = FakeActivities(helpdesk=FakeHelpdesk(failure_rate=0.5, rng=ScriptedRandom(0.9, 0.0)))
    handle = await start(env, fakes, "T-2")

    waiting = await wait_for_step(handle, "awaiting_approval")
    assert waiting.decision.action.type == "refund"
    assert waiting.decision.decided_by == "agent"  # Jev never decides refunds
    assert waiting.decision.action.amount_eur == 49.0

    await handle.execute_update(TicketWorkflow.approve, Approval(approved=True, approver="alice"))
    state = await handle.result()

    assert state.step == "done"
    assert state.result.action == "refund"
    assert fakes.helpdesk_attempts == 2
    assert len(fakes.helpdesk.refunds) == 1


async def test_refund_without_approval_for_24h_is_escalated(env):
    fakes = FakeActivities()
    handle = await start(env, fakes, "T-2", approval_timeout=timedelta(hours=24))
    await wait_for_step(handle, "awaiting_approval")

    state = await handle.result()  # time skipping jumps over the 24 hours

    assert state.decision.action.type == "escalate"
    assert "not approved" in state.decision.action.reason
    assert fakes.helpdesk.refunds == []


async def test_rejected_refund_is_closed(env):
    fakes = FakeActivities()
    handle = await start(env, fakes, "T-2")
    await wait_for_step(handle, "awaiting_approval")

    await handle.execute_update(TicketWorkflow.approve, Approval(approved=False, approver="bob", comment="duplicate"))
    state = await handle.result()

    assert state.decision.action.type == "close"
    assert state.decision.action.reason == "Refund rejected by bob: duplicate"


async def test_helpdesk_fails_then_succeeds_after_retry(env):
    fakes = FakeActivities(helpdesk=FakeHelpdesk(failure_rate=0.5, rng=ScriptedRandom(0.0)))
    state = await (await start(env, fakes, "T-5")).result()

    assert fakes.helpdesk_attempts == 2
    assert state.step == "done"
    assert state.decision.action.type == "close"


async def test_helpdesk_down_fails_with_a_clear_reason(env):
    fakes = FakeActivities(helpdesk=FakeHelpdesk(failure_rate=1.0))
    handle = await start(env, fakes, "T-5")

    with pytest.raises(WorkflowFailureError):
        await handle.result()
    state = await handle.query(TicketWorkflow.state)
    assert state.step == "failed"
    assert "Helpdesk action failed" in state.error
    assert fakes.helpdesk_attempts == 6  # retry limit, not forever


async def test_prompt_injection_refund_is_escalated_by_the_rules(env):
    state = await (await start(env, FakeActivities(), "T-6")).result()

    assert state.proposal.action.type == "refund"
    assert state.proposal.action.amount_eur == 5000
    assert state.decision.decided_by == "rules"
    assert state.decision.action.type == "escalate"
    assert any("exceeds invoice" in v for v in state.violations)


async def test_unknown_customer_is_escalated_without_any_model(env):
    state = await (await start(env, FakeActivities(), "T-7")).result()

    assert state.decision.decided_by == "precheck"
    assert state.decision.action.type == "escalate"
    assert state.triage is None and state.proposal is None


async def test_low_jev_confidence_hands_over_to_the_agent(env):
    state = await (await start(env, FakeActivities(confidence=0.3), "T-1", jev_min_confidence=0.8)).result()

    assert state.route.startswith("agent: Jev confidence below 0.8")
    assert state.decision.decided_by == "agent"
    assert state.decision.action.type == "reply"


async def test_agent_only_mode_skips_jev(env):
    state = await (await start(env, FakeActivities(), "T-3", decider="agent")).result()

    assert state.triage is None
    assert state.decision.decided_by == "agent"
    assert (state.decision.category, state.decision.priority) == ("technical", "urgent")
    assert state.decision.action.type == "escalate"


async def test_approval_is_refused_when_not_waiting(env):
    # A closed ticket stuck retrying the Helpdesk is in step "acting", not waiting for approval.
    handle = await start(env, FakeActivities(helpdesk=FakeHelpdesk(failure_rate=1.0)), "T-5")
    await wait_for_step(handle, "acting")

    with pytest.raises(WorkflowUpdateFailedError) as error:
        await handle.execute_update(TicketWorkflow.approve, Approval(approved=True, approver="alice"))
    assert "not waiting for approval" in str(error.value.cause)
    await handle.terminate()
