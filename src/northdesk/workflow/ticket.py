"""The ticket workflow: precheck -> Jev (System 1) -> agent (System 2) -> rules -> approval -> act.

Workflow code only orchestrates and keeps state; all I/O happens in activities.
"""

from datetime import timedelta
from typing import NoReturn

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from pydantic_ai import UsageLimits
    from pydantic_ai.durable_exec.temporal import PydanticAIWorkflow
    from pydantic_ai.exceptions import AgentRunError

    from northdesk.activities import ActionRequest, ClassifyInput, classify_ticket, execute_action, load_context
    from northdesk.agent.agent import AgentDeps, support_agent
    from northdesk.agent.prompts import build_prompt
    from northdesk.domain import (
        Approval,
        Close,
        Decision,
        Escalate,
        Step,
        Ticket,
        TicketContext,
        TicketState,
        TicketWorkflowInput,
        WorkflowOptions,
    )
    from northdesk.rules import enforce
    from northdesk.rules.triage import decision_from_triage

QUICK = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3)
HELPDESK_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=6,
)


@workflow.defn
class TicketWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = [support_agent]

    @workflow.init
    def __init__(self, input: TicketWorkflowInput) -> None:
        self._state = TicketState(ticket_id=input.ticket.ticket_id)

    @workflow.run
    async def run(self, input: TicketWorkflowInput) -> TicketState:
        ticket, options = input.ticket, input.options
        self._log("ticket received", decider=options.decider)

        self._step("loading_context")
        try:
            context = await workflow.execute_activity(
                load_context, ticket, start_to_close_timeout=timedelta(seconds=10), retry_policy=QUICK
            )
        except ActivityError as exc:
            self._fail(f"Could not load customer data: {exc.cause or exc}", exc)

        if context.customer is None:
            # Deterministic precheck: no model is asked about a customer we don't know.
            self._state.route = "precheck: unknown customer, no model called"
            decision = Decision(
                category="other",
                priority="normal",
                action=Escalate(reason=f"Unknown customer {ticket.customer_id}"),
                decided_by="precheck",
            )
        else:
            proposal = await self._decide(ticket, context, options)
            self._step("validating")
            self._state.proposal = proposal
            decision, self._state.violations = enforce(proposal, context)
            if self._state.violations:
                self._log("decision broke rules, escalating", violations=self._state.violations)
        self._state.decision = decision

        if decision.action.type == "refund":
            self._state.decision = await self._wait_for_approval(decision, options.approval_timeout)

        self._step("acting")
        try:
            self._state.result = await workflow.execute_activity(
                execute_action,
                ActionRequest(ticket_id=ticket.ticket_id, decision=self._state.decision),
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=HELPDESK_RETRY,
            )
        except ActivityError as exc:
            self._fail(f"Helpdesk action failed: {exc.cause or exc}", exc)

        self._step("done")
        self._log("ticket done", action=self._state.decision.action.type, decided_by=self._state.decision.decided_by)
        return self._state

    async def _decide(self, ticket: Ticket, context: TicketContext, options: WorkflowOptions) -> Decision:
        triage = None
        if options.decider == "jev_first":
            self._step("classifying")
            try:
                triage = await workflow.execute_activity(
                    classify_ticket,
                    ClassifyInput(ticket=ticket, context=context),
                    start_to_close_timeout=timedelta(seconds=15),
                    retry_policy=QUICK,
                )
                self._state.triage = triage
                decision, why = decision_from_triage(triage, context, options.jev_min_confidence)
            except ActivityError as exc:
                decision, why = None, f"Jev unavailable: {exc.cause or exc}"
            if decision:
                self._state.route = f"jev: {why}"
                return decision
            self._state.route = f"agent: {why}"
        else:
            self._state.route = "agent: DECIDER=agent, Jev skipped"
        self._log("routing", route=self._state.route)

        self._step("deciding")
        deps = AgentDeps(
            ticket_id=ticket.ticket_id, workflow_id=workflow.info().workflow_id, customer_id=ticket.customer_id
        )
        try:
            result = await support_agent.run(
                build_prompt(ticket, triage), deps=deps, usage_limits=UsageLimits(request_limit=6)
            )
        except (AgentRunError, ActivityError) as exc:
            # Unreadable answers after the retries, too many steps, or the model is down: a person decides.
            self._log("agent failed, escalating", error=str(exc))
            return Decision(
                category="other",
                priority="normal",
                action=Escalate(reason=f"Agent could not decide: {exc}"),
                decided_by="rules",
            )
        return Decision(**result.output.model_dump(), decided_by="agent")

    async def _wait_for_approval(self, decision: Decision, timeout: timedelta) -> Decision:
        self._step("awaiting_approval")
        decision = decision.model_copy(update={"decided_by": "approval"})
        try:
            await workflow.wait_condition(lambda: self._state.approval is not None, timeout=timeout)
        except TimeoutError:
            self._log("no approval in time, escalating")
            decision.action = Escalate(reason=f"Refund not approved within {timeout}")
            return decision
        approval = self._state.approval
        if not approval.approved:
            reason = f"Refund rejected by {approval.approver}" + (f": {approval.comment}" if approval.comment else "")
            decision.action = Close(reason=reason)
        return decision

    @workflow.query
    def state(self) -> TicketState:
        return self._state

    @workflow.update
    def approve(self, approval: Approval) -> TicketState:
        self._state.approval = approval
        self._log("approval received", approved=approval.approved, approver=approval.approver)
        return self._state

    @approve.validator
    def _check_approval(self, approval: Approval) -> None:
        if self._state.step != "awaiting_approval" or self._state.approval is not None:
            raise ValueError(f"Ticket is not waiting for approval (step: {self._state.step})")

    def _fail(self, error: str, cause: Exception) -> NoReturn:
        """End the workflow as failed, keeping the reason readable through the state query."""
        self._state.error = error
        self._step("failed")
        raise ApplicationError(error, non_retryable=True) from cause

    def _step(self, step: Step) -> None:
        self._state.step = step
        self._log("step", step=step)

    def _log(self, message: str, **fields: object) -> None:
        ids = {"ticket_id": self._state.ticket_id, "workflow_id": workflow.info().workflow_id}
        workflow.logger.info(message, extra={**ids, **fields})
