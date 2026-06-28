"""Azure worker entrypoint (the production async path).

Drains the Azure Storage Queue, executes each job over the substrate, persists the
result to Postgres. Runs as its own Container App (scale-to-zero capable). Local boot
uses FastAPI background tasks instead, so this is only exercised in the Azure deployment.

    python -m strata_platform.jobs.worker
"""
from __future__ import annotations

import time

from strata_platform.config import get_settings
from strata_platform.jobs.runner import run_job


def main() -> int:  # pragma: no cover - requires Azure Storage Queue
    s = get_settings()
    if not s.queue_connection_string:
        raise RuntimeError("QUEUE_CONNECTION_STRING not set; worker requires a queue")
    from azure.storage.queue import QueueClient

    q = QueueClient.from_connection_string(s.queue_connection_string, s.queue_name)
    while True:
        for msg in q.receive_messages(messages_per_page=8, visibility_timeout=300):
            try:
                run_job(msg.content)         # message body is the job_id
                q.delete_message(msg)
            except Exception:                # noqa: BLE001 - leave for redelivery
                pass
        time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
