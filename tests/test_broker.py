"""The job broker - no CAD, no network, no threads left running.

The broker is what lets a CAD that cannot be pushed into (Rhino) still be
driven: it holds a single-flight queue, the CAD polls for work, and the
submitter waits for the result. These are the behaviours that matter when the
CAD on the other end is unreliable - and Rhino has crashed repeatedly during
this spike, so "the poller vanished mid-job" is a real case, not a hypothetical.

    cd server && uv run --frozen --no-sync python ../tests/test_broker.py
"""

import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server/src"))

from fusion_mcp import broker as bk  # noqa: E402

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


def truthy(label, got):
    check(label, bool(got), True)


# ------------------------------------------------------------- empty ----
print("An idle broker")
b = bk.Broker()
check("no poller has ever checked in", b.poller_connected(), False)
check("claim returns nothing when there is no work", b.claim(wait=0.05), None)
health = b.health()
check("health reports no active job", health["active_job"], None)
check("health reports nothing queued", health["queued"], 0)
truthy("claiming counts as the poller checking in", b.poller_connected())


# ------------------------------------------------- the happy path ----
print("A job, start to finish")
b = bk.Broker()
answers = {}


def submitter():
    answers["result"] = b.submit("execute", {"code": "result = 1 + 1"}, timeout=5)


thread = threading.Thread(target=submitter, daemon=True)
thread.start()
time.sleep(0.1)

job = b.claim(wait=2.0)
truthy("the poller receives the job", job is not None)
check("with its kind", job["kind"], "execute")
check("and its payload", job["payload"]["code"], "result = 1 + 1")
check("the broker now shows it active", b.health()["active_job"], job["id"])

check("completing it succeeds", b.complete(job["id"], {"ok": True, "result": 2}), True)
thread.join(timeout=5)
check("the submitter got the result", answers["result"], {"ok": True, "result": 2})
check("and the broker is idle again", b.health()["active_job"], None)
check("completion counted", b.health()["completed"], 1)


# ------------------------------------------------------ single flight ----
print("Single flight")
b = bk.Broker()
blocked = {}


def slow_submitter():
    blocked["first"] = b.submit("execute", {"code": "slow"}, timeout=3)


thread = threading.Thread(target=slow_submitter, daemon=True)
thread.start()
time.sleep(0.1)

second = b.submit("execute", {"code": "fast"}, timeout=1)
check("a second job is refused, not queued", second["ok"], False)
truthy("and says why", "still running" in second["error"])
truthy("flagged as busy so the caller can retry", second.get("busy"))

job = b.claim(wait=2.0)
b.complete(job["id"], {"ok": True, "result": "slow done"})
thread.join(timeout=5)
check("the first job still completed", blocked["first"]["result"], "slow done")

after = b.submit("execute", {"code": "now free"}, timeout=0.3)
check("the slot is released afterwards", after["ok"], False)   # times out, not busy
check("and it is a timeout, not a busy refusal", after.get("busy"), None)


# ------------------------------------------------------------ timeouts ----
print("When the CAD does not answer")
b = bk.Broker()
result = b.submit("execute", {"code": "nobody home"}, timeout=0.3)
check("the submitter gives up", result["ok"], False)
truthy("naming the poller as the thing to check", "poller" in result["error"])
truthy("and warning against resending", "do not resend" in result["error"])
check("the queue is left clean", b.health()["queued"], 0)
check("so a later job can still run", b.health()["active_job"], None)


# ------------------------------------------ the CAD dies mid-job ----
# Rhino crashed repeatedly during this spike, so this is the realistic failure:
# the poller takes a job and never comes back. Without expiry the broker would
# wedge at permanently busy.
print("The CAD claims a job and vanishes")
b = bk.Broker()
original_expiry = bk.CLAIM_EXPIRY_S
bk.CLAIM_EXPIRY_S = 0.2
try:
    got = {}

    def orphan_submitter():
        got["result"] = b.submit("execute", {"code": "doomed"}, timeout=4)

    thread = threading.Thread(target=orphan_submitter, daemon=True)
    thread.start()
    time.sleep(0.1)

    job = b.claim(wait=2.0)
    truthy("the job was claimed", job is not None)
    time.sleep(0.4)                      # poller never returns

    # A later claim is what notices the abandonment.
    b.claim(wait=0.05)
    thread.join(timeout=5)
    check("the submitter is released, not left hanging", got["result"]["ok"], False)
    truthy("and told the CAD probably closed",
           "closed" in got["result"]["error"])
    check("the broker is usable again", b.health()["active_job"], None)
    check("and it was counted as expired", b.health()["expired"], 1)
finally:
    bk.CLAIM_EXPIRY_S = original_expiry


# ------------------------------------------------------ stale results ----
print("Late and bogus results")
b = bk.Broker()
check("a result for an unknown job is rejected",
      b.complete("nonexistent", {"ok": True}), False)

held = {}


def late_submitter():
    held["result"] = b.submit("execute", {"code": "x"}, timeout=0.3)


thread = threading.Thread(target=late_submitter, daemon=True)
thread.start()
time.sleep(0.05)
job = b.claim(wait=1.0)
thread.join(timeout=3)                   # submitter has already timed out
check("a result arriving after the submitter gave up is rejected",
      b.complete(job["id"], {"ok": True, "result": "too late"}), False)


# ------------------------------------------------------- long polling ----
print("Long polling")
b = bk.Broker()
started = time.monotonic()
check("claim waits rather than spinning", b.claim(wait=0.4), None)
waited = time.monotonic() - started
truthy("it really waited", waited >= 0.35)
truthy("and did not overshoot", waited < 1.5)

# work arriving mid-wait must be picked up promptly, not after the full wait
b = bk.Broker()
picked = {}


def waiter():
    started_at = time.monotonic()
    picked["job"] = b.claim(wait=3.0)
    picked["waited"] = time.monotonic() - started_at


thread = threading.Thread(target=waiter, daemon=True)
thread.start()
time.sleep(0.2)
threading.Thread(target=lambda: b.submit("execute", {"code": "y"}, timeout=2),
                 daemon=True).start()
thread.join(timeout=5)
truthy("a waiting poller is woken by new work", picked.get("job") is not None)
truthy("promptly, not after the full wait", picked.get("waited", 99) < 1.5)


# ------------------------------------------------------------ config ----
print("Configuration")
check("loopback only", bk.BROKER_HOST, "127.0.0.1")
truthy("claim expiry outlives the job timeout",
       bk.CLAIM_EXPIRY_S > bk.JOB_TIMEOUT_S)
truthy("long-poll wait is shorter than the job timeout",
       bk.CLAIM_WAIT_S < bk.JOB_TIMEOUT_S)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
