"""Unit tests for step 2 (decision checks) and for how Jev answers become a decision."""

import pytest

from northdesk.domain import (
    Close,
    Decision,
    Escalate,
    JevAnswer,
    ProposedDecision,
    Refund,
    Reply,
    Triage,
)
from northdesk.rules import check_decision, enforce
from northdesk.rules.triage import decision_from_triage
from tests.fakes import context


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
        (Refund.model_construct(invoice_id="INV-1001", amount_eur=float("nan")), "C-100", "must be a finite number"),
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


@pytest.mark.parametrize("action", [Escalate(reason=""), Close(reason="")])
def test_escalate_and_close_need_a_reason(action):
    assert check_decision(proposal(action), context("C-200")) == [f"{action.type} needs a reason"]


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


@pytest.mark.parametrize(
    ("answers", "customer", "reason"),
    [
        ({"confidence": 0.5}, "C-200", "Jev confidence below 0.8"),
        ({"action": "refund"}, "C-100", "Jev chose 'refund', which only the agent may decide"),
        ({"article": "none"}, "C-200", "Jev found no matching KB article"),
    ],
)
def test_jev_hands_over_to_the_agent(answers, customer, reason):
    decision, why = decision_from_triage(triage(**answers), context(customer), min_confidence=0.8)
    assert decision is None
    assert why.startswith(reason)


def test_incomplete_jev_answers_go_to_the_agent():
    partial = triage()
    del partial.answers["action"]
    decision, why = decision_from_triage(partial, context("C-200"), min_confidence=0.8)
    assert decision is None
    assert why == "Jev answers incomplete: action"
