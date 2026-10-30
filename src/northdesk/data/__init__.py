"""Read-only customer, invoice and knowledge base data, loaded from the JSON files next to this module."""

import json
import re
from functools import cache
from pathlib import Path

from northdesk.domain import Customer, Invoice, KbArticle

_DIR = Path(__file__).parent


@cache
def _customers() -> dict[str, Customer]:
    rows = json.loads((_DIR / "customers.json").read_text())
    return {row["customer_id"]: Customer(**row) for row in rows}


@cache
def _invoices() -> list[Invoice]:
    return [Invoice(**row) for row in json.loads((_DIR / "invoices.json").read_text())]


@cache
def kb_articles() -> list[KbArticle]:
    return [KbArticle(**row) for row in json.loads((_DIR / "kb.json").read_text())]


def get_customer(customer_id: str) -> Customer | None:
    return _customers().get(customer_id)


def get_invoices(customer_id: str) -> list[Invoice]:
    return [inv for inv in _invoices() if inv.customer_id == customer_id]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2}


def search_kb(query: str, limit: int = 2) -> list[KbArticle]:
    """Rank articles by how many query words appear in their title and text."""
    query_words = _words(query)
    scored = [(len(query_words & _words(f"{a.title} {a.text}")), a) for a in kb_articles()]
    return [a for score, a in sorted(scored, key=lambda s: -s[0]) if score > 0][:limit]
