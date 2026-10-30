"""Domain types shared by the API, workflow, activities and rules. No framework imports."""

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, Field

CATEGORIES = ("account", "billing", "technical", "other")
PRIORITIES = ("low", "normal", "high", "urgent")
ACTIONS = ("reply", "refund", "escalate", "close")

Category = Literal["account", "billing", "technical", "other"]
Priority = Literal["low", "normal", "high", "urgent"]


class Ticket(BaseModel):
    # URL-safe, because the ticket ID is also the workflow ID and part of the API paths.
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    customer_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    subject: str = Field(max_length=500)
    body: str = Field(max_length=10_000)


class Customer(BaseModel):
    customer_id: str
    name: str
    plan: str
    status: str


class Invoice(BaseModel):
    invoice_id: str
    customer_id: str
    month: str
    amount_eur: float
    status: str


class KbArticle(BaseModel):
    article_id: str
    title: str
    text: str


# --- Actions -----------------------------------------------------------------


class Reply(BaseModel):
    type: Literal["reply"] = "reply"
    message: str
    article_ids: list[str] = Field(description="IDs of the KB articles the message is based on.")


class Refund(BaseModel):
    type: Literal["refund"] = "refund"
    invoice_id: str
    amount_eur: float = Field(allow_inf_nan=False)


class Escalate(BaseModel):
    type: Literal["escalate"] = "escalate"
    reason: str


class Close(BaseModel):
    type: Literal["close"] = "close"
    reason: str


Action = Annotated[Reply | Refund | Escalate | Close, Field(discriminator="type")]


class ProposedDecision(BaseModel):
    """What a decider (Jev, the agent) proposes. Values are checked by the rules, not trusted."""

    category: str
    priority: str
    action: Action


class AgentDecision(ProposedDecision):
    """Output type for the LLM agent: same shape, with enums in the schema to guide the model."""

    category: Category
    priority: Priority


DecidedBy = Literal["precheck", "jev", "agent", "rules", "approval"]


class Decision(ProposedDecision):
    decided_by: DecidedBy
    """Who produced this action: the precheck, Jev, the agent, a rule override or the approver."""


# --- Workflow I/O --------------------------------------------------------------


class TicketContext(BaseModel):
    """Snapshot of everything the deterministic code needs, loaded once by an activity."""

    customer: Customer | None
    invoices: list[Invoice]
    kb_articles: list[KbArticle]


class JevAnswer(BaseModel):
    choice: str
    confidence: float


class Triage(BaseModel):
    """Jev's answers, keyed by question name (category, priority, action, article)."""

    model: str
    answers: dict[str, JevAnswer]


class WorkflowOptions(BaseModel):
    decider: Literal["agent", "jev_first"] = "agent"
    jev_min_confidence: float = 0.8
    approval_timeout: timedelta = timedelta(hours=24)


class TicketWorkflowInput(BaseModel):
    ticket: Ticket
    options: WorkflowOptions = WorkflowOptions()


class Approval(BaseModel):
    approved: bool
    approver: str = Field(min_length=1)
    comment: str | None = None


class ActionResult(BaseModel):
    action: str
    reference: str
    detail: str


Step = Literal[
    "received",
    "loading_context",
    "classifying",
    "deciding",
    "validating",
    "awaiting_approval",
    "acting",
    "done",
    "failed",
]


class TicketState(BaseModel):
    """Everything the API shows for a ticket. Kept by the workflow and read with a query."""

    ticket_id: str
    step: Step = "received"
    triage: Triage | None = None
    route: str | None = None
    """Why the ticket was decided where it was (precheck, Jev or the agent)."""
    proposal: Decision | None = None
    violations: list[str] = []
    approval: Approval | None = None
    decision: Decision | None = None
    result: ActionResult | None = None
    error: str | None = None
