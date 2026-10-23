"""`ftask wait`: block until the human acts on a task, so an agent can be woken by a background command.

Cursor semantics: the cursor is an events.seq. Every result prints the cursor to pass as `--since` next time, so
nothing is missed between calls. Polling is one indexed `MAX(seq)` read on a long-lived connection with
exponential backoff (0.1 s .. 1 s): negligible CPU, wake-up latency at most about a second.
"""
from __future__ import annotations

import signal
import time

from . import hooks
from . import store as st

KINDS = ("done", "note", "upload", "any")
EXIT_TIMEOUT = 124
CLOSED_KINDS = ("done", "cancelled")


def matches(ev, kind) -> bool:
    k = ev["kind"]
    if kind == "done":
        return k in CLOSED_KINDS
    if kind == "note":
        return k == "note" and ev["actor"] == hooks.HUMAN
    if kind == "upload":
        return k == "upload"
    return k in CLOSED_KINDS or hooks.is_human_event(ev)


def wait(store: st.Store, *, ref=None, key_prefix=None, kind="any", since=None, timeout=None, sleep=time.sleep):
    """Return {"timeout": bool, "cursor": int, "events": [...], "task": dict|None, "reason": str}."""
    if kind not in KINDS:
        raise st.ValidationError(f"--for must be one of {', '.join(KINDS)}")
    if bool(ref) == bool(key_prefix):
        raise st.ValidationError("give exactly one of ID|KEY or --key-prefix")
    task = store.get(ref) if ref else None
    task_id = task["id"] if task else None
    conn = st.connect(store.path)
    wid = None
    old = {}
    try:
        def on_term(signum, frame):
            raise KeyboardInterrupt
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                old[sig] = signal.signal(sig, on_term)
            except ValueError:  # not the main thread (tests): leave the default handlers
                break
        cur_max = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        cursor = cur_max if since is None else int(since)
        # State-based answers: a task that is already finished satisfies `--for done` at once; `--for any`
        # does too unless the caller passed a cursor (then only events after the cursor count).
        if task is not None and task["status"] in st.CLOSED and (kind == "done" or (kind == "any" and since is None)):
            return {"timeout": False, "cursor": max(cursor, cur_max), "events": [], "task": task,
                    "reason": "already-" + task["status"]}
        wid = store.register_waiter(task_id, key_prefix, kind)
        deadline = None if timeout is None else time.monotonic() + timeout
        delay = 0.1
        while True:
            cur_max = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            if cur_max > cursor:
                delay = 0.1
                batch = st.Store._events_after(conn, cursor, task_id=task_id, key_prefix=key_prefix, limit=1000)
                # advance past everything examined (also events of other tasks)
                top = batch[-1]["seq"] if batch else 0
                scanned_to = top if len(batch) >= 1000 else max(cur_max, top)
                hits = [e for e in batch if matches(e, kind)]
                if hits:
                    t = store.get(hits[0]["task_id"])
                    return {"timeout": False, "cursor": scanned_to, "events": hits, "task": t, "reason": "event"}
                cursor = scanned_to
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return {"timeout": True, "cursor": cursor, "events": [], "task": task, "reason": "timeout"}
                sleep(min(delay, left))
            else:
                sleep(delay)
            delay = min(delay * 1.5, 1.0)
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
        if wid is not None:
            store.unregister_waiter(wid)
        conn.close()
