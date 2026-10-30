"""Rule-based stand-in for the LLM. It speaks the same protocol as the real model:
first it asks for tool calls, then it returns the decision through the output tool.
"""

import re

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_core import to_jsonable_python


def fake_agent(messages: list, info: AgentInfo) -> ModelResponse:
    prompt = ""
    tool_results: dict[str, object] = {}
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    prompt = part.content
                elif isinstance(part, ToolReturnPart):
                    tool_results[part.tool_name] = to_jsonable_python(part.content)

    if not tool_results:
        ticket_text = prompt.split("<ticket>")[-1].split("</ticket>")[0]
        return ModelResponse(
            parts=[
                ToolCallPart("get_customer", {}),
                ToolCallPart("get_invoices", {}),
                ToolCallPart("search_kb", {"query": ticket_text}),
            ]
        )
    decision = _decide(prompt.lower(), tool_results)
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, decision)])


def _decide(text: str, tools: dict) -> dict:
    customer = tools.get("get_customer") or {}
    invoices = tools.get("get_invoices") or []
    ticket = text.split("<ticket>")[-1].split("</ticket>")[0]

    if customer.get("status") == "suspended":
        overdue = [i["invoice_id"] for i in invoices if i["status"] == "overdue"]
        message = (
            "Your account is suspended because an invoice is overdue"
            f" ({', '.join(overdue) or 'see billing'}). Pay the invoice to unlock the account."
        )
        return _decision("account", "high", {"type": "reply", "message": message, "article_ids": ["KB-4"]})

    if re.search(r"refund|charged", ticket):
        paid = [i for i in invoices if i["status"] == "paid"]
        if not paid:
            return _decision("billing", "normal", {"type": "escalate", "reason": "Refund asked, no paid invoice"})
        invoice = paid[-1]
        # Like a naive model, take an amount from the ticket if there is one. The rules catch it.
        asked = re.search(r"(\d+(?:\.\d+)?)\s*eur", ticket)
        amount = float(asked.group(1)) if asked else invoice["amount_eur"]
        refund = {"type": "refund", "invoice_id": invoice["invoice_id"], "amount_eur": amount}
        return _decision("billing", "normal", refund)

    if re.search(r"password|log ?in|sign in", ticket):
        message = "Open the sign-in page, choose 'Forgot password' and follow the link we email you."
        return _decision("account", "normal", {"type": "reply", "message": message, "article_ids": ["KB-1"]})

    if re.search(r"\bdown\b|outage|error", ticket):
        priority = "urgent" if "urgent" in ticket or customer.get("plan") == "enterprise" else "high"
        return _decision("technical", priority, {"type": "escalate", "reason": "Possible outage reported"})

    return _decision("other", "low", {"type": "close", "reason": "Not a support request for Northdesk"})


def _decision(category: str, priority: str, action: dict) -> dict:
    return {"category": category, "priority": priority, "action": action}


fake_model = FunctionModel(fake_agent, model_name="fake-agent")
