"""HTTP API. It only starts, reads and signals workflows; it never decides anything itself."""

import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel
from temporalio.client import Client, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from northdesk.config import Settings, get_settings
from northdesk.domain import Approval, Ticket, TicketState, TicketWorkflowInput, WorkflowOptions
from northdesk.log import setup_logging
from northdesk.temporal import connect
from northdesk.workflow.ticket import TicketWorkflow

log = logging.getLogger(__name__)


class TicketAccepted(BaseModel):
    ticket_id: str
    workflow_id: str
    duplicate: bool


def create_app(client: Client | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if client is None:
            setup_logging(settings.log_level)
        app.state.client = client or await connect(settings)
        yield

    app = FastAPI(title="Northdesk tickets", lifespan=lifespan)
    app.state.client = client

    def temporal(request: Request) -> Client:
        return request.app.state.client

    @app.post("/tickets", status_code=status.HTTP_202_ACCEPTED, response_model=TicketAccepted)
    async def create_ticket(ticket: Ticket, request: Request, response: Response) -> TicketAccepted:
        workflow_id = ticket.ticket_id
        ids = {"ticket_id": ticket.ticket_id, "workflow_id": workflow_id}
        options = WorkflowOptions(
            decider=settings.decider,
            jev_min_confidence=settings.jev_min_confidence,
            approval_timeout=timedelta(seconds=settings.approval_timeout_seconds),
        )
        try:
            # The ticket ID is the workflow ID, and a used ID is never reused: one ticket, one run.
            await temporal(request).start_workflow(
                TicketWorkflow.run,
                TicketWorkflowInput(ticket=ticket, options=options),
                id=workflow_id,
                task_queue=settings.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            log.info("duplicate ticket, returning existing workflow", extra=ids)
            response.status_code = status.HTTP_200_OK
            return TicketAccepted(**ids, duplicate=True)
        log.info("workflow started", extra=ids)
        return TicketAccepted(**ids, duplicate=False)

    @app.get("/tickets/{ticket_id}", response_model=TicketState)
    async def get_ticket(ticket_id: str, request: Request) -> TicketState:
        handle = temporal(request).get_workflow_handle(ticket_id)
        try:
            return await handle.query(TicketWorkflow.state)
        except RPCError as exc:
            raise _http_error(exc, ticket_id) from exc

    @app.post("/tickets/{ticket_id}/approval", response_model=TicketState)
    async def approve(ticket_id: str, approval: Approval, request: Request) -> TicketState:
        ids = {"ticket_id": ticket_id, "workflow_id": ticket_id}
        handle = temporal(request).get_workflow_handle(ticket_id)
        try:
            state = await handle.execute_update(TicketWorkflow.approve, approval)
        except WorkflowUpdateFailedError as exc:
            log.info("approval refused", extra={**ids, "reason": str(exc.cause)})
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc.cause.message if exc.cause else exc)) from exc
        except RPCError as exc:
            # Temporal answers NOT_FOUND for an update to a finished workflow too; tell them apart.
            if exc.status == RPCStatusCode.NOT_FOUND and await _exists(handle):
                raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Ticket {ticket_id} is already finished") from exc
            raise _http_error(exc, ticket_id) from exc
        log.info("approval accepted", extra={**ids, "approved": approval.approved, "approver": approval.approver})
        return state

    return app


async def _exists(handle: WorkflowHandle) -> bool:
    try:
        await handle.describe()
    except RPCError:
        return False
    return True


def _http_error(exc: RPCError, ticket_id: str) -> HTTPException:
    if exc.status == RPCStatusCode.NOT_FOUND:
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Ticket {ticket_id} not found")
    return HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Temporal error")


app = create_app()
