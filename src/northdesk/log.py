"""JSON-lines logging. Pass ticket_id and workflow_id via `extra=` and they become top-level fields."""

import json
import logging
import sys
from datetime import UTC, datetime

_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("temporal_"):
                line[key] = value
        # Temporal's own log lines (e.g. activity retries) carry a context dict instead of our fields.
        # The workflow ID is the ticket ID, so both can be filled in from it.
        temporal = getattr(record, "temporal_activity", None) or getattr(record, "temporal_workflow", None)
        if isinstance(temporal, dict) and "workflow_id" in temporal:
            line.setdefault("workflow_id", temporal["workflow_id"])
            line.setdefault("ticket_id", temporal["workflow_id"])
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
