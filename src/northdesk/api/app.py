"""HTTP API. It only starts, reads and signals workflows; it never decides anything itself."""

import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
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

    @app.exception_handler(RPCError)
    async def temporal_error(request: Request, exc: RPCError) -> JSONResponse:
        """Any Temporal error: 404 if the ticket doesn't exist, otherwise 502. Always logged with the IDs."""
        ids = getattr(request.state, "ids", {})
        if exc.status == RPCStatusCode.NOT_FOUND:
            log.info("ticket not found", extra=ids)
            return JSONResponse({"detail": f"Ticket {ids.get('ticket_id')} not found"}, status.HTTP_404_NOT_FOUND)
        log.error("temporal error", extra={**ids, "status": exc.status.name, "error": exc.message})
        return JSONResponse({"detail": "Temporal is unavailable, try again later"}, status.HTTP_502_BAD_GATEWAY)

    @app.post("/tickets", status_code=status.HTTP_202_ACCEPTED, response_model=TicketAccepted)
    async def create_ticket(ticket: Ticket, request: Request, response: Response) -> TicketAccepted:
        ids = request.state.ids = {"ticket_id": ticket.ticket_id, "workflow_id": ticket.ticket_id}
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
                id=ids["workflow_id"],
                task_queue=settings.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            log.info("duplicate ticket, returning existing workflow", extra=ids)
            response.status_code = status.HTTP_200_OK
            return TicketAccepted(**ids, duplicate=True)
        log.info("workflow started", extra={**ids, "decider": options.decider})
        return TicketAccepted(**ids, duplicate=False)

    @app.get("/tickets/{ticket_id}", response_model=TicketState)
    async def get_ticket(ticket_id: str, request: Request) -> TicketState:
        request.state.ids = {"ticket_id": ticket_id, "workflow_id": ticket_id}
        return await temporal(request).get_workflow_handle(ticket_id).query(TicketWorkflow.state)

    @app.post("/tickets/{ticket_id}/approval", response_model=TicketState)
    async def approve(ticket_id: str, approval: Approval, request: Request) -> TicketState:
        ids = request.state.ids = {"ticket_id": ticket_id, "workflow_id": ticket_id}
        handle = temporal(request).get_workflow_handle(ticket_id)
        try:
            state = await handle.execute_update(TicketWorkflow.approve, approval)
        except WorkflowUpdateFailedError as exc:
            log.info("approval refused", extra={**ids, "reason": str(exc.cause)})
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc.cause.message if exc.cause else exc)) from exc
        except RPCError as exc:
            # Temporal answers NOT_FOUND for an update to a finished workflow too; tell them apart.
            if exc.status == RPCStatusCode.NOT_FOUND and await _exists(handle):
                log.info("approval refused, ticket already finished", extra=ids)
                raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Ticket {ticket_id} is already finished") from exc
            raise
        log.info("approval accepted", extra={**ids, "approved": approval.approved, "approver": approval.approver})
        return state

    return app


async def _exists(handle: WorkflowHandle) -> bool:
    try:
        await handle.describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return False
        raise  # an outage is not "doesn't exist"
    return True


app = create_app()
