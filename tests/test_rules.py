"""Unit tests for step 2 (decision checks) and for how Jev answers become a decision."""

import pytest

from northdesk import data
from northdesk.domain import (
    Close,
    Decision,
    Escalate,
    JevAnswer,
    ProposedDecision,
    Refund,
    Reply,
    TicketContext,
    Triage,
)
from northdesk.rules import check_decision, enforce
from northdesk.rules.triage import decision_from_triage


def context(customer_id: str) -> TicketContext:
    return TicketContext(
        customer=data.get_customer(customer_id),
        invoices=data.get_invoices(customer_id),
        kb_articles=data.kb_articles(),
    )


def proposal(action, category="billing", priority="normal") -> ProposedDecision:
    return ProposedDecision(category=category, priority=priority, action=action)


def test_valid_refund_passes():
    assert check_decision(proposal(Refund(invoice_id="INV-1002", amount_eur=49.0)), context("C-100")) == []


@pytest.mark.parametrize(
    ("refund", "customer", "expected"),
    [
        (Refund(invoice_id="INV-1002", amount_eur=5000), "C-100", "exceeds invoice"),
        (Refund(invoice_id="INV-3001", amount_eur=10), "C-100", "does not belong to customer C-100"),
        (Refund(invoice_id="INV-9999", amount_eur=10), "C-100", "does not belong"),
        (Refund(invoice_id="INV-4001", amount_eur=49), "C-400", "is overdue"),
        (Refund(invoice_id="INV-1001", amount_eur=0), "C-100", "must be positive"),
    ],
)
def test_invalid_refunds_are_rejected(refund, customer, expected):
    violations = check_decision(proposal(refund), context(customer))
    assert any(expected in v for v in violations), violations


def test_unknown_enum_values_are_rejected():
    violations = check_decision(proposal(Close(reason="spam"), category="sales", priority="asap"), context("C-200"))
    assert violations == ["category 'sales' is not allowed", "priority 'asap' is not allowed"]


def test_reply_must_cite_known_articles_and_have_text():
    violations = check_decision(proposal(Reply(message=" ", article_ids=["KB-1", "KB-99"])), context("C-200"))
    assert violations == ["reply message is empty", "reply cites unknown KB articles: KB-99"]


def test_escalate_and_close_need_a_reason():
    assert check_decision(proposal(Escalate(reason="")), context("C-200")) == ["escalate needs a reason"]


def test_enforce_turns_a_broken_decision_into_an_escalation():
    bad = Decision(
        category="billing",
        priority="urgent",
        action=Refund(invoice_id="INV-1002", amount_eur=5000),
        decided_by="agent",
    )
    decision, violations = enforce(bad, context("C-100"))
    assert decision.action.type == "escalate"
    assert decision.decided_by == "rules"
    assert (decision.category, decision.priority) == ("billing", "urgent")
    assert "exceeds invoice" in decision.action.reason
    assert violations


def triage(confidence=0.95, action="reply", article="KB-1") -> Triage:
    answers = {"category": "account", "priority": "normal", "action": action, "article": article}
    return Triage(model="test", answers={q: JevAnswer(choice=c, confidence=confidence) for q, c in answers.items()})


def test_confident_jev_reply_uses_the_article_text():
    decision, _ = decision_from_triage(triage(), context("C-200"), min_confidence=0.8)
    assert decision.decided_by == "jev"
    assert decision.action.article_ids == ["KB-1"]
    assert "Forgot password" in decision.action.message


def test_low_confidence_goes_to_the_agent():
    decision, why = decision_from_triage(triage(confidence=0.5), context("C-200"), min_confidence=0.8)
    assert decision is None
    assert why.startswith("Jev confidence below 0.8")


def test_jev_never_decides_refunds():
    decision, why = decision_from_triage(triage(action="refund"), context("C-100"), min_confidence=0.8)
    assert decision is None
    assert "only the agent" in why


def test_reply_without_article_goes_to_the_agent():
    decision, why = decision_from_triage(triage(article="none"), context("C-200"), min_confidence=0.8)
    assert decision is None
    assert "no matching KB article" in why
