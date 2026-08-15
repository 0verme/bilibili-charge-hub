import json
import logging

FIELDS = (
    "request_id",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "user_id",
    "account_id",
    "job_id",
    "run_id",
    "upstream_error",
    "retry_count",
    "lookback_hours",
    "max_records",
    "scanned",
    "missing_outbox",
    "outbox_rebuilt",
    "missing_deliveries",
    "deliveries_created",
    "requeued",
    "already_complete",
    "skipped",
    "errors",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            {field: getattr(record, field) for field in FIELDS if hasattr(record, field)}
        )
        if record.exc_info:
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_app_logging() -> None:
    for name in ("app.request", "app.services.scheduler"):
        logger = logging.getLogger(name)
        if any(getattr(handler, "_charge_hub", False) for handler in logger.handlers):
            continue
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler._charge_hub = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
