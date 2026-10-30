"""System 1: TypeSafe Jev through the OpenRouter Decisions API, plus an offline fake with the same output.

Jev answers several fixed-choice questions about one ticket in a single call and returns a
confidence for each answer. It never writes text and never calls tools.
"""

import math
import re
from typing import Protocol

import httpx

from northdesk.domain import JevAnswer, Ticket, TicketContext, Triage

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


class TransientClassifierError(Exception):
    """Rate limit, timeout or server error: safe to retry."""


class ClassifierError(Exception):
    """Bad request, auth or credits problem: retrying will not help."""


class Classifier(Protocol):
    async def classify(self, ticket: Ticket, context: TicketContext) -> Triage: ...


def build_state(ticket: Ticket, context: TicketContext) -> str:
    customer = context.customer
    lines = [f"Customer plan: {customer.plan}, account status: {customer.status}"] if customer else []
    lines += [f"Invoice {i.invoice_id}: {i.month}, {i.amount_eur:.2f} EUR, {i.status}" for i in context.invoices]
    # The ticket is untrusted text. Jev can only answer with the options below, whatever it says.
    lines += ["--- Customer ticket (untrusted) ---", f"Subject: {ticket.subject}", f"Body: {ticket.body}"]
    return "\n".join(lines)


def build_questions(context: TicketContext) -> dict:
    articles = {a.article_id: a.title for a in context.kb_articles} | {"none": "No article answers this ticket"}
    return {
        "category": {
            "type": "choice",
            "instructions": "Which category does this support ticket belong to?",
            "criteria": {
                "account": "Sign-in, passwords, access to the account, suspended or locked accounts",
                "billing": "Invoices, charges, payments, refunds",
                "technical": "Bugs, errors, outages, API problems",
                "other": "Anything not about the Northdesk product or the customer's account",
            },
        },
        "priority": {
            "type": "choice",
            "instructions": "How urgent is this ticket for the support team?",
            "criteria": {
                "low": "Off-topic or no impact on the customer",
                "normal": "One user or a routine question",
                "high": "The customer cannot use the product or is blocked",
                "urgent": "An outage hurting the customer's business right now",
            },
        },
        "action": {
            "type": "choice",
            "instructions": "What should support do with this ticket?",
            "criteria": {
                "reply": "A knowledge base article fully answers the question",
                "refund": "The customer asks for money back for a charge",
                "escalate": "A person on the team has to look at it (outages, unclear or risky cases)",
                "close": "Spam or not a support request",
            },
        },
        "article": {
            "type": "choice",
            "instructions": "Which knowledge base article answers this ticket?",
            "criteria": articles,
        },
    }


class JevClassifier:
    def __init__(
        self, api_key: str, model: str, timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._model = model
        self._timeout = timeout
        self._transport = transport

    async def classify(self, ticket: Ticket, context: TicketContext) -> Triage:
        questions = build_questions(context)
        body = {"model": self._model, "state": build_state(ticket, context), "questions": questions}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(DECISIONS_URL, json=body, headers=self._headers)
        except httpx.TransportError as exc:
            raise TransientClassifierError(f"Jev request failed: {exc}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientClassifierError(f"Jev returned {response.status_code}")
        if response.status_code >= 400:
            raise ClassifierError(f"Jev returned {response.status_code}: {response.text[:200]}")
        data = response.json()
        return Triage(model=data.get("model", self._model), answers=_parse_answers(data.get("answers"), questions))


def _parse_answers(raw: object, questions: dict) -> dict[str, JevAnswer]:
    """Accept only a complete answer set: every question, a listed option, a confidence in [0, 1].

    Anything else is a permanent error, so the workflow hands the ticket to the agent.
    """
    if not isinstance(raw, dict):
        raise ClassifierError("Jev response has no answers")
    missing = [q for q in questions if q not in raw]
    if missing:
        raise ClassifierError(f"Jev response is missing answers: {', '.join(missing)}")
    answers = {}
    for name, spec in questions.items():
        answer = raw[name] if isinstance(raw[name], dict) else {}
        choice, confidence = answer.get("choice"), answer.get("confidence")
        if choice not in spec["criteria"]:
            raise ClassifierError(f"Jev answer {name}={choice!r} is not an option")
        if not isinstance(confidence, int | float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ClassifierError(f"Jev answer {name} has an invalid confidence: {confidence!r}")
        answers[name] = JevAnswer(choice=choice, confidence=confidence)
    return answers


# Keyword rules for the fake: (pattern, category, priority, action, article, confidence).
_FAKE_RULES = [
    (r"password|log ?in|sign in", "account", "normal", "reply", "KB-1", 0.93),
    (r"\bdown\b|outage|errors", "technical", "high", "escalate", "KB-3", 0.91),
    (r"refund|charged", "billing", "normal", "refund", "KB-2", 0.9),
    (r"pizza|lunch|restaurant", "other", "low", "close", "none", 0.92),
]


class FakeClassifier:
    """Offline stand-in for Jev. Unclear tickets get a low confidence, so the agent decides them."""

    async def classify(self, ticket: Ticket, context: TicketContext) -> Triage:
        text = f"{ticket.subject} {ticket.body}".lower()
        unclear = ("", "other", "normal", "escalate", "none", 0.45)
        match = next((rule for rule in _FAKE_RULES if re.search(rule[0], text)), unclear)
        _, category, priority, action, article, confidence = match
        if action == "escalate" and "urgent" in text:
            priority = "urgent"
        answers = {
            "category": JevAnswer(choice=category, confidence=confidence),
            "priority": JevAnswer(choice=priority, confidence=confidence),
            "action": JevAnswer(choice=action, confidence=confidence),
            "article": JevAnswer(choice=article, confidence=confidence),
        }
        return Triage(model="fake-jev", answers=answers)
