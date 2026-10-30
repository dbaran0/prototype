"""Turn Jev's answers into a decision, or explain why the agent has to decide instead."""

from northdesk.domain import Close, Decision, Escalate, Reply, TicketContext, Triage

# Jev only picks from fixed lists. Refunds need an invoice and an amount checked against the
# customer's billing, so they always go to the agent (and then to a human for approval).
JEV_ACTIONS = ("reply", "escalate", "close")


def decision_from_triage(triage: Triage, context: TicketContext, min_confidence: float) -> tuple[Decision | None, str]:
    """Return (decision, why). The decision is None when the agent has to decide."""
    answers = triage.answers
    action = answers["action"].choice
    needed = ["category", "priority", "action"] + (["article"] if action == "reply" else [])
    low = [f"{q} {answers[q].confidence:.2f}" for q in needed if answers[q].confidence < min_confidence]
    if low:
        return None, f"Jev confidence below {min_confidence}: {', '.join(low)}"
    if action not in JEV_ACTIONS:
        return None, f"Jev chose '{action}', which only the agent may decide"

    category, priority = answers["category"].choice, answers["priority"].choice
    if action == "reply":
        article = next((a for a in context.kb_articles if a.article_id == answers["article"].choice), None)
        if article is None:
            return None, "Jev found no matching KB article"
        name = context.customer.name if context.customer else "there"
        message = f"Hi {name},\n\n{article.title}: {article.text}\n\nNorthdesk Support"
        result = Reply(message=message, article_ids=[article.article_id])
    elif action == "escalate":
        result = Escalate(reason=f"Needs a person: {category} issue with {priority} priority")
    else:
        result = Close(reason="Not a support request for Northdesk")
    decision = Decision(category=category, priority=priority, action=result, decided_by="jev")
    return decision, f"Jev confident (>= {min_confidence})"
