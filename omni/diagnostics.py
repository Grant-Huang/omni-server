"""Voice session diagnostics logger.

Logs all critical events in the audio pipeline to a file for easy troubleshooting.
Run: tail -f logs/audio-diagnostics.log
"""
import logging
import os
from datetime import datetime

# Ensure logs directory exists
os.makedirs("logs", exist_ok=True)

# Create a dedicated diagnostics logger
diagnostics_logger = logging.getLogger("omni.diagnostics")
diagnostics_logger.setLevel(logging.DEBUG)

# Remove default handlers
diagnostics_logger.handlers = []

# File handler - write to logs/audio-diagnostics.log
handler = logging.FileHandler("logs/audio-diagnostics.log", encoding="utf-8")
handler.setLevel(logging.DEBUG)

# Format: timestamp | level | component | message
formatter = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
handler.setFormatter(formatter)
diagnostics_logger.addHandler(handler)

# Also print to console for immediate visibility
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(formatter)
diagnostics_logger.addHandler(console_handler)


def log_upstream_event(event_type: str, size: int = 0, session_id: str = "") -> None:
    """Log event received from DashScope upstream."""
    diagnostics_logger.info(
        f"[UPSTREAM] {event_type} session={session_id} size={size}B"
    )


def log_session_event(event_type: str, session_id: str, details: str = "") -> None:
    """Log session processing event."""
    diagnostics_logger.info(
        f"[SESSION] {event_type} session={session_id} {details}"
    )


def log_client_send(event_type: str, session_id: str, size: int = 0, success: bool = True) -> None:
    """Log event sent to client."""
    status = "OK" if success else "FAIL"
    diagnostics_logger.info(
        f"[CLIENT] {event_type} session={session_id} size={size}B status={status}"
    )


def log_error(component: str, error: str, session_id: str = "", context: str = "") -> None:
    """Log errors in the pipeline."""
    diagnostics_logger.error(
        f"[ERROR] {component} session={session_id} error={error} {context}"
    )


def log_debug(component: str, message: str, session_id: str = "") -> None:
    """Log debug-level diagnostics."""
    diagnostics_logger.debug(
        f"[DEBUG] {component} session={session_id} {message}"
    )
