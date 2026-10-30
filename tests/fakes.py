"""Fake activities for workflow tests. Same names and signatures as the real ones."""

from temporalio import activity

from northdesk.activities import ActionRequest, ClassifyInput
from northdesk.domain import ActionResult, JevAnswer, Triage
from northdesk.services.classifier import FakeClassifier
from northdesk.services.helpdesk import FakeHelpdesk


class ScriptedRandom:
    """Stands in for random.Random: returns the given values in order, then never fails."""

    def __init__(self, *values: float) -> None:
        self._values = list(values)

    def random(self) -> float:
        return self._values.pop(0) if self._values else 1.0


class FakeActivities:
    def __init__(self, helpdesk: FakeHelpdesk | None = None, confidence: float | None = None) -> None:
        self.helpdesk = helpdesk or FakeHelpdesk()
        self.confidence = confidence  # override every Jev confidence
        self.helpdesk_attempts = 0

    @activity.defn(name="classify_ticket")
    async def classify_ticket(self, request: ClassifyInput) -> Triage:
        triage = await FakeClassifier().classify(request.ticket, request.context)
        if self.confidence is not None:
            triage.answers = {
                q: JevAnswer(choice=a.choice, confidence=self.confidence) for q, a in triage.answers.items()
            }
        return triage

    @activity.defn(name="execute_action")
    async def execute_action(self, request: ActionRequest) -> ActionResult:
        self.helpdesk_attempts += 1
        key = f"{activity.info().workflow_id}:{request.decision.action.type}"
        return self.helpdesk.perform(key, request.ticket_id, request.decision.action)
