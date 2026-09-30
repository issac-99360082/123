"""Standalone worker entrypoint: `python -m app.worker`.

Runs the scheduler without the HTTP server. Use this when you scale the API
horizontally - exactly one worker process should own the check loop.
"""

from __future__ import annotations

import signal
import threading

from app.core.scheduler import start_scheduler, stop_scheduler
from app.db import init_db
from app.logging_conf import configure_logging, get_logger

log = get_logger(__name__)


def main() -> None:
    configure_logging()
    init_db()
    start_scheduler()
    log.info("worker_ready")

    stop_event = threading.Event()

    def _handle(signum, _frame):
        log.info("worker_shutting_down", signal=signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    try:
        stop_event.wait()
    finally:
        stop_scheduler()
        log.info("worker_stopped")


if __name__ == "__main__":
    main()
