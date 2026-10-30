"""Fake Helpdesk API. Fails at random with temporary errors and is idempotent per request key.

Completed actions are stored in SQLite, so every worker sharing the file sees them: a retry that
lands on another worker (or after a restart) gets the stored result instead of acting again.
"""

import random
import sqlite3
import uuid
from pathlib import Path

from northdesk.domain import Action, ActionResult, Close, Escalate, Refund, Reply


class TransientHelpdeskError(Exception):
    """Temporary failure: safe to retry."""


class HelpdeskError(Exception):
    """Permanent failure: retrying will not help."""


class FakeHelpdesk:
    def __init__(
        self,
        store: str | Path = ":memory:",
        failure_rate: float = 0.0,
        rng: random.Random | None = None,
        fail_permanently: bool = False,
    ) -> None:
        self.failure_rate = failure_rate
        self.fail_permanently = fail_permanently
        self._rng = rng or random.Random()
        # Autocommit mode; transactions are opened explicitly below.
        self._db = sqlite3.connect(store, isolation_level=None, check_same_thread=False, timeout=10)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS actions (key TEXT PRIMARY KEY, action TEXT NOT NULL, result TEXT NOT NULL)"
        )

    def perform(self, idempotency_key: str, ticket_id: str, action: Action) -> ActionResult:
        if self.fail_permanently:
            raise HelpdeskError(f"ticket {ticket_id} is closed in the Helpdesk")
        # Fail before doing anything (request never arrived)...
        self._maybe_fail("helpdesk unavailable")
        # The write lock makes "check key, act, record" atomic across workers.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute("SELECT result FROM actions WHERE key = ?", (idempotency_key,)).fetchone()
            if row:
                result = ActionResult.model_validate_json(row[0])
            else:
                result = self._apply(ticket_id, action)
                self._db.execute(
                    "INSERT INTO actions (key, action, result) VALUES (?, ?, ?)",
                    (idempotency_key, action.type, result.model_dump_json()),
                )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        # ...or after doing it (the response got lost). A retry must then not repeat the action.
        self._maybe_fail("helpdesk response lost")
        return result

    def count(self, action_type: str) -> int:
        """How many actions of this type were really carried out."""
        return self._db.execute("SELECT COUNT(*) FROM actions WHERE action = ?", (action_type,)).fetchone()[0]

    def _maybe_fail(self, message: str) -> None:
        if self._rng.random() < self.failure_rate:
            raise TransientHelpdeskError(message)

    def _apply(self, ticket_id: str, action: Action) -> ActionResult:
        reference = f"HD-{uuid.uuid4().hex[:8]}"
        match action:
            case Reply(article_ids=articles):
                detail = f"reply sent to {ticket_id} (articles: {', '.join(articles) or 'none'})"
            case Refund(invoice_id=invoice, amount_eur=amount):
                detail = f"refunded {amount:.2f} EUR on {invoice}"
            case Escalate(reason=reason):
                detail = f"escalated to a person: {reason}"
            case Close(reason=reason):
                detail = f"closed: {reason}"
            case _:
                raise HelpdeskError(f"unsupported action {action!r}")
        return ActionResult(action=action.type, reference=reference, detail=detail)
