"""Fake Helpdesk API. Fails at random with temporary errors and is idempotent per request key."""

import random
import uuid

from northdesk.domain import Action, ActionResult, Close, Escalate, Refund, Reply


class TransientHelpdeskError(Exception):
    """Temporary failure: safe to retry."""


class HelpdeskError(Exception):
    """Permanent failure: retrying will not help."""


class FakeHelpdesk:
    def __init__(self, failure_rate: float = 0.0, rng: random.Random | None = None) -> None:
        self.failure_rate = failure_rate
        self._rng = rng or random.Random()
        self._done: dict[str, ActionResult] = {}  # idempotency key -> result
        self.refunds: list[Refund] = []  # every refund actually issued, to show none is doubled

    def perform(self, idempotency_key: str, ticket_id: str, action: Action) -> ActionResult:
        # Fail before doing anything (request never arrived)...
        self._maybe_fail("helpdesk unavailable")
        if idempotency_key in self._done:
            return self._done[idempotency_key]
        result = self._apply(ticket_id, action)
        self._done[idempotency_key] = result
        # ...or after doing it (the response got lost). A retry must then not repeat the action.
        self._maybe_fail("helpdesk response lost")
        return result

    def _maybe_fail(self, message: str) -> None:
        if self._rng.random() < self.failure_rate:
            raise TransientHelpdeskError(message)

    def _apply(self, ticket_id: str, action: Action) -> ActionResult:
        reference = f"HD-{uuid.uuid4().hex[:8]}"
        match action:
            case Reply(article_ids=articles):
                detail = f"reply sent to {ticket_id} (articles: {', '.join(articles) or 'none'})"
            case Refund(invoice_id=invoice, amount_eur=amount):
                self.refunds.append(action)
                detail = f"refunded {amount:.2f} EUR on {invoice}"
            case Escalate(reason=reason):
                detail = f"escalated to a person: {reason}"
            case Close(reason=reason):
                detail = f"closed: {reason}"
            case _:
                raise HelpdeskError(f"unsupported action {action!r}")
        return ActionResult(action=action.type, reference=reference, detail=detail)
