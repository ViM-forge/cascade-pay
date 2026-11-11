import json
import logging


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Do not serialize arbitrary args/exceptions (may include URLs or secrets).
        return json.dumps(
            {
                "level": record.levelname,
                "logger": record.name,
                "event": getattr(record, "event", "runtime"),
                "pid": record.process,
            }
        )


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
