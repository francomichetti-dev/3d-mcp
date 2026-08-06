"""A job broker for CAD applications that cannot be pushed into.

Fusion lets an add-in run an HTTP listener and marshal work onto its main
thread with registerCustomEvent/fireCustomEvent. Rhino does not: its
InvokeOnUiThread is synchronous, so calling it from a script deadlocks (a
rhinocode script IS the UI thread), and calling it from a background thread
never serviced the callback at all.

This inverts the direction rather than fighting it. The CAD stops being a
server and becomes a client: a small poller inside the application asks this
broker for work, runs it, and posts the result back. Because that poller is
already executing on the UI thread, whatever it runs is on the right thread by
construction — there is no marshaling primitive to find, and nothing to
deadlock.

    MCP server  ──POST /submit──▶  broker  ◀──GET /claim───  poller in the CAD
                ◀───result─────                ──POST /result──▶

Single-flight, exactly like the bridge: one job in the CAD at a time. Loopback
only, same token, same fail-closed behaviour.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

BROKER_HOST = "127.0.0.1"
BROKER_PORT = int(os.environ.get("FUSION_BROKER_PORT") or 7656)

# How long a submitter waits for the CAD to finish. Matches the bridge's
# marshal timeout so behaviour is the same whichever transport is in use.
JOB_TIMEOUT_S = 60.0
# How long GET /claim blocks waiting for work. Long-polling rather than busy
# polling: one request per minute when idle instead of one per 200ms, and a job
# still starts within milliseconds of being submitted.
CLAIM_WAIT_S = 25.0
# A claimed job whose poller never came back. Slightly beyond JOB_TIMEOUT_S so
# the submitter always gives up first and sees a real error rather than a
# silently vanished job.
CLAIM_EXPIRY_S = 75.0

STATUS_QUEUED = "queued"
STATUS_CLAIMED = "claimed"
STATUS_DONE = "done"


@dataclass
class Job:
    """One unit of work handed to the CAD."""

    id: str
    kind: str
    payload: dict[str, Any]
    status: str = STATUS_QUEUED
    result: dict[str, Any] | None = None
    created: float = field(default_factory=time.monotonic)
    claimed_at: float | None = None
    done = None  # threading.Event, created in __post_init__

    def __post_init__(self) -> None:
        self.done = threading.Event()

    def age(self) -> float:
        return time.monotonic() - self.created


class Broker:
    """The queue itself, with no HTTP attached so it can be tested directly."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._work = threading.Condition(self._lock)
        self._queued: list[Job] = []
        self._active: Job | None = None
        self._last_seen: float | None = None      # when a poller last checked in
        self._stats = {"submitted": 0, "completed": 0, "failed": 0, "expired": 0}

    # -- submitter side ---------------------------------------------------- #

    def submit(self, kind: str, payload: dict[str, Any],
               timeout: float = JOB_TIMEOUT_S) -> dict[str, Any]:
        """Queue a job and wait for the CAD to finish it.

        Single-flight: refuses immediately rather than queueing behind another
        job, because a CAD operation can take a minute and silently waiting
        twice as long is worse than a clear "busy".
        """
        job = Job(id=uuid.uuid4().hex, kind=kind, payload=payload)
        with self._work:
            if self._active is not None or self._queued:
                return {"ok": False, "error": "previous job still running",
                        "busy": True}
            self._queued.append(job)
            self._stats["submitted"] += 1
            self._work.notify_all()

        if not job.done.wait(timeout):
            with self._work:
                self._drop(job)
                self._stats["failed"] += 1
            return {"ok": False, "error": (
                f"the CAD did not answer within {timeout:.0f}s. It may still be "
                "working; do not resend. Check that the poller is running.")}

        return job.result or {"ok": False, "error": "job finished without a result"}

    # -- CAD poller side --------------------------------------------------- #

    def claim(self, wait: float = CLAIM_WAIT_S) -> dict[str, Any] | None:
        """Block until there is work, or `wait` elapses. Returns one job."""
        deadline = time.monotonic() + wait
        with self._work:
            self._last_seen = time.time()
            while True:
                self._expire_locked()
                if self._queued and self._active is None:
                    job = self._queued.pop(0)
                    job.status = STATUS_CLAIMED
                    job.claimed_at = time.monotonic()
                    self._active = job
                    return {"id": job.id, "kind": job.kind, "payload": job.payload}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._work.wait(min(remaining, 1.0))

    def complete(self, job_id: str, result: dict[str, Any]) -> bool:
        """Hand back a result. False if the job is unknown or already timed out."""
        with self._work:
            job = self._active
            if job is None or job.id != job_id:
                return False
            job.result = result
            job.status = STATUS_DONE
            self._active = None
            self._stats["completed"] += 1
            self._work.notify_all()
        job.done.set()
        return True

    # -- state ------------------------------------------------------------- #

    def poller_connected(self, within: float = 60.0) -> bool:
        """Has a CAD poller checked in recently? Drives the 'is it up' message."""
        if self._last_seen is None:
            return False
        return (time.time() - self._last_seen) < within

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "poller_connected": self.poller_connected(),
                "seconds_since_poll": (
                    round(time.time() - self._last_seen, 1)
                    if self._last_seen else None),
                "active_job": self._active.id if self._active else None,
                "queued": len(self._queued),
                **self._stats,
            }

    # -- internals ---------------------------------------------------------- #

    def _drop(self, job: Job) -> None:
        """Caller must hold the lock."""
        if self._active is job:
            self._active = None
        if job in self._queued:
            self._queued.remove(job)

    def _expire_locked(self) -> None:
        """Release a job whose poller took it and never returned.

        Without this a CAD that is closed mid-job wedges the broker at
        permanently busy — the same failure the bridge's stranded-job guard
        exists to prevent.
        """
        job = self._active
        if job is None or job.claimed_at is None:
            return
        if time.monotonic() - job.claimed_at > CLAIM_EXPIRY_S:
            job.result = {"ok": False, "error": (
                "the CAD claimed this job and never returned a result — "
                "it was probably closed mid-operation")}
            self._active = None
            self._stats["expired"] += 1
            job.done.set()


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")
