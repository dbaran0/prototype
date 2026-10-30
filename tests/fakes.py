"""Fake activities for workflow tests. Same names and signatures as the real ones."""

from temporalio import activity

from northdesk import data
from northdesk.activities import ActionRequest, ClassifyInput, perform_action
from northdesk.domain import ActionResult, JevAnswer, Ticket, TicketContext, Triage
from northdesk.services.classifier import ClassifierError, FakeClassifier
from northdesk.services.helpdesk import FakeHelpdesk


class ScriptedRandom:
    """Stands in for random.Random: returns the given values in order, then never fails."""

    def __init__(self, *values: float) -> None:
        self._values = list(values)

    def random(self) -> float:
        return self._values.pop(0) if self._values else 1.0


class FakeActivities:
    def __init__(
        self,
        helpdesk: FakeHelpdesk | None = None,
        confidence: float | None = None,
        context_fails: bool = False,
        classifier_error: str | None = None,
    ) -> None:
        self.helpdesk = helpdesk or FakeHelpdesk()
        self.confidence = confidence  # override every Jev confidence
        self.context_fails = context_fails
        self.classifier_error = classifier_error
        self.helpdesk_attempts = 0

    @activity.defn(name="load_context")
    async def load_context(self, ticket: Ticket) -> TicketContext:
        if self.context_fails:
            raise RuntimeError("customer database unavailable")
        customer = data.get_customer(ticket.customer_id)
        invoices = data.get_invoices(ticket.customer_id) if customer else []
        return TicketContext(customer=customer, invoices=invoices, kb_articles=data.kb_articles())

    @activity.defn(name="classify_ticket")
    async def classify_ticket(self, request: ClassifyInput) -> Triage:
        if self.classifier_error:
            raise ClassifierError(self.classifier_error)
        triage = await FakeClassifier().classify(request.ticket, request.context)
        if self.confidence is not None:
            triage.answers = {
                q: JevAnswer(choice=a.choice, confidence=self.confidence) for q, a in triage.answers.items()
            }
        return triage

    @activity.defn(name="execute_action")
    async def execute_action(self, request: ActionRequest) -> ActionResult:
        # Same error handling as production, only the Helpdesk instance is ours.
        self.helpdesk_attempts += 1
        return perform_action(self.helpdesk, request)

    @property
    def all(self) -> list:
        return [self.load_context, self.classify_ticket, self.execute_action]
