"""Outside-service adapters: Jev response checking and Helpdesk idempotency across workers."""

import httpx
import pytest

from northdesk import data
from northdesk.domain import Refund, Ticket, TicketContext
from northdesk.services.classifier import ClassifierError, JevClassifier, build_questions
from northdesk.services.helpdesk import FakeHelpdesk, TransientHelpdeskError
from tests.fakes import ScriptedRandom

TICKET = Ticket(ticket_id="T-1", customer_id="C-200", subject="Can't log in", body="Forgot password")
CONTEXT = TicketContext(customer=data.get_customer("C-200"), invoices=[], kb_articles=data.kb_articles())


def jev_returning(answers: dict) -> JevClassifier:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "typesafe/jev-test", "answers": answers})

    return JevClassifier("key", "jev", transport=httpx.MockTransport(handler))


def full_answers(**overrides) -> dict:
    answers = {
        q: {"type": "choice", "choice": next(iter(spec["criteria"])), "confidence": 0.9}
        for q, spec in build_questions(CONTEXT).items()
    }
    return answers | overrides


async def test_complete_jev_answer_is_accepted():
    triage = await jev_returning(full_answers()).classify(TICKET, CONTEXT)
    assert set(triage.answers) == {"category", "priority", "action", "article"}


@pytest.mark.parametrize(
    ("answers", "error"),
    [
        ({k: v for k, v in full_answers().items() if k != "action"}, "missing answers: action"),
        (full_answers(category={"choice": "sales", "confidence": 0.9}), "not an option"),
        (full_answers(priority={"choice": "low", "confidence": 1.5}), "confidence"),
        (full_answers(priority={"choice": "low"}), "confidence"),
    ],
)
async def test_malformed_jev_answer_is_a_permanent_error(answers, error):
    with pytest.raises(ClassifierError, match=error):
        await jev_returning(answers).classify(TICKET, CONTEXT)


def test_refund_is_issued_once_across_two_workers(tmp_path):
    store = tmp_path / "helpdesk.sqlite3"
    refund = Refund(invoice_id="INV-1002", amount_eur=49.0)
    # Worker A applies the refund, then the response is lost.
    worker_a = FakeHelpdesk(store, failure_rate=0.5, rng=ScriptedRandom(0.9, 0.0))
    with pytest.raises(TransientHelpdeskError):
        worker_a.perform("T-2:refund", "T-2", refund)

    # The retry lands on worker B, a different process with the same store.
    worker_b = FakeHelpdesk(store)
    result = worker_b.perform("T-2:refund", "T-2", refund)

    assert result.action == "refund"
    assert worker_b.count("refund") == 1
