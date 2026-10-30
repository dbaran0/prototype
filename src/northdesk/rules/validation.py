"""Step 2: check a proposed decision against the business rules.

Whatever the ticket text says, only decisions that pass these checks are carried out as proposed.
Anything else becomes an escalation to a person.
"""

import math

from northdesk.domain import (
    ACTIONS,
    CATEGORIES,
    PRIORITIES,
    Close,
    Decision,
    Escalate,
    ProposedDecision,
    Refund,
    Reply,
    TicketContext,
)


def check_decision(decision: ProposedDecision, context: TicketContext) -> list[str]:
    """Return the rules the decision breaks (empty list = valid)."""
    violations: list[str] = []
    if decision.category not in CATEGORIES:
        violations.append(f"category '{decision.category}' is not allowed")
    if decision.priority not in PRIORITIES:
        violations.append(f"priority '{decision.priority}' is not allowed")

    action = decision.action
    if action.type not in ACTIONS:
        violations.append(f"action '{action.type}' is not allowed")
    elif isinstance(action, Reply):
        violations += _check_reply(action, context)
    elif isinstance(action, Refund):
        violations += _check_refund(action, context)
    elif isinstance(action, Escalate | Close) and not action.reason.strip():
        violations.append(f"{action.type} needs a reason")
    return violations


def _check_reply(reply: Reply, context: TicketContext) -> list[str]:
    violations = []
    if not reply.message.strip():
        violations.append("reply message is empty")
    known = {a.article_id for a in context.kb_articles}
    unknown = [a for a in reply.article_ids if a not in known]
    if unknown:
        violations.append(f"reply cites unknown KB articles: {', '.join(unknown)}")
    return violations


def _check_refund(refund: Refund, context: TicketContext) -> list[str]:
    customer_id = context.customer.customer_id if context.customer else None
    invoice = next((i for i in context.invoices if i.invoice_id == refund.invoice_id), None)
    if invoice is None or invoice.customer_id != customer_id:
        return [f"invoice {refund.invoice_id} does not belong to customer {customer_id}"]
    if not math.isfinite(refund.amount_eur):
        return ["refund amount must be a finite number"]
    violations = []
    if invoice.status != "paid":
        violations.append(f"invoice {invoice.invoice_id} is {invoice.status}, only paid invoices can be refunded")
    if refund.amount_eur <= 0:
        violations.append("refund amount must be positive")
    if refund.amount_eur > invoice.amount_eur:
        violations.append(
            f"refund {refund.amount_eur:.2f} EUR exceeds invoice {invoice.invoice_id} ({invoice.amount_eur:.2f} EUR)"
        )
    return violations


def enforce(proposal: Decision, context: TicketContext) -> tuple[Decision, list[str]]:
    """Return the decision to act on: the proposal if it is valid, otherwise an escalation."""
    violations = check_decision(proposal, context)
    if not violations:
        return proposal, []
    category = proposal.category if proposal.category in CATEGORIES else "other"
    priority = proposal.priority if proposal.priority in PRIORITIES else "normal"
    escalation = Decision(
        category=category,
        priority=priority,
        action=Escalate(reason="Decision broke business rules: " + "; ".join(violations)),
        decided_by="rules",
    )
    return escalation, violations
