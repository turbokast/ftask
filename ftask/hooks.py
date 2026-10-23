"""Hooks: run user scripts and per-task notify commands when the human acts.

Whichever process records a human event (the web UI server or the CLI) calls `after_commit`, which spawns a
detached runner process (`ftask _hook-run`) so the request / command never waits for a hook. The runner executes,
for each human event, the matching executables in the hooks directory and the task's `notify_cmd`, each with a
time limit, and appends the outcome to hooks.log.

A "human event" is an event whose actor is `human` (stored as `founder`; the web UI records it, and on the CLI use
`--by human` / FTASK_BY=human) of kind done, cancelled, reopened, note, step or upload.

Hooks directory: $FTASK_HOOKS_DIR, else <config dir>/hooks (~/.config/ftask/hooks on Linux) -- but only for the real database. A
temporary database (FTASK_DB elsewhere) never triggers the real hooks unless FTASK_HOOKS_DIR is set explicitly.
"""
from __future__ import annotations

import json
import os
import pwd
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import paths

HUMAN = "founder"  # stored actor / file-direction value for the person using the UI. Kept as the pre-release
# spelling so existing databases keep working; `--by human` is accepted and normalised to it.
HUMAN_KINDS = ("done", "cancelled", "reopened", "note", "step", "upload")
HOOK_FOR_KIND = {"done": "on-done", "note": "on-note", "upload": "on-upload"}
DEFAULT_TIMEOUT = 30.0
LOG_MAX = 1_000_000
OUTPUT_KEEP = 2000


def real_default_db() -> Path:
    return paths.real_db_path()


def hooks_dir(db_path):
    env = os.environ.get("FTASK_HOOKS_DIR")
    if env:
        return Path(env).expanduser()
    try:
        same = Path(db_path).resolve() == real_default_db().resolve()
    except OSError:
        same = False
    if not same:
        return None
    return paths.hooks_dir()


def log_path(db_path) -> Path:
    return Path(db_path).parent / "hooks.log"


def hook_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("FTASK_HOOK_TIMEOUT", DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT


def hook_files(db_path, kind) -> list:
    """Executable hook files that apply to an event of this kind: on-<kind> first, then on-any."""
    d = hooks_dir(db_path)
    if d is None:
        return []
    names = []
    if kind in HOOK_FOR_KIND:
        names.append(HOOK_FOR_KIND[kind])
    if kind in HUMAN_KINDS:
        names.append("on-any")
    out = []
    for n in names:
        p = d / n
        try:
            if p.is_file() and os.access(p, os.X_OK):
                out.append(p)
        except OSError:
            pass
    return out


def is_human_event(ev) -> bool:
    return ev["actor"] == HUMAN and ev["kind"] in HUMAN_KINDS


# --------------------------------------------------------------------------
# recording side: decide whether anything needs to run, then spawn the runner
# --------------------------------------------------------------------------

def after_commit(conn, seqs) -> None:
    if os.environ.get("FTASK_IN_HOOK"):  # events recorded by a hook never trigger hooks (no loops)
        return
    db_path = conn.db_path
    marks = ",".join("?" * len(seqs))
    rows = conn.execute(
        f"SELECT e.seq, e.kind, e.actor, t.notify_cmd FROM events e LEFT JOIN tasks t ON t.id=e.task_id "
        f"WHERE e.seq IN ({marks}) ORDER BY e.seq", list(seqs)).fetchall()
    todo = []
    for r in rows:
        ev = {"actor": r["actor"], "kind": r["kind"]}
        if not is_human_event(ev):
            continue
        if (r["notify_cmd"] or "") or hook_files(db_path, r["kind"]):
            todo.append(r["seq"])
    if todo:
        spawn_runner(db_path, todo)


def spawn_runner(db_path, seqs) -> None:
    root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ftask", "--db", str(db_path), "_hook-run", "--seq",
         ",".join(str(s) for s in seqs)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True, env=env, cwd=str(Path.home()))
    # reap the child if this process lives on (the web server); a CLI process just exits and init adopts it
    threading.Thread(target=proc.wait, daemon=True).start()


# --------------------------------------------------------------------------
# runner side
# --------------------------------------------------------------------------

def _log(db_path, line: str) -> None:
    p = log_path(db_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if p.stat().st_size > LOG_MAX:
                os.replace(p, str(p) + ".1")
        except OSError:
            pass
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line.rstrip("\n") + "\n").encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except OSError:
        pass


def _shell() -> str:
    cands = [os.environ.get("SHELL")]
    try:
        cands.append(pwd.getpwuid(os.getuid()).pw_shell)
    except KeyError:
        pass
    for c in cands:
        if c and os.path.basename(c) in ("sh", "bash", "zsh", "dash", "ksh") and os.access(c, os.X_OK):
            return c
    return "/bin/sh"


def _child_env(db_path, ev) -> dict:
    env = dict(os.environ)
    env.update({
        "FTASK_ID": ev["task_id"],
        "FTASK_KEY": ev.get("task_key") or "",
        "FTASK_EVENT": ev["kind"],
        "FTASK_PROJECT": ev.get("task_project") or "",
        "FTASK_TITLE": ev.get("task_title") or "",
        "FTASK_SEQ": str(ev["seq"]),
        "FTASK_ACTOR": ev["actor"],
        "FTASK_DB": str(db_path),
        "FTASK_IN_HOOK": "1",
    })
    env.pop("FTASK_BY", None)
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    # a systemd user service has no desktop session variables; notify-send needs the session bus
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if "XDG_RUNTIME_DIR" not in env and os.path.isdir(runtime):
        env["XDG_RUNTIME_DIR"] = runtime
    bus = os.path.join(runtime, "bus")
    if "DBUS_SESSION_BUS_ADDRESS" not in env and os.path.exists(bus):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def _run_one(db_path, label, argv, payload: bytes, env, timeout) -> dict:
    t0 = time.monotonic()
    out = tempfile.TemporaryFile()
    status = None
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, env=env,
                                cwd=str(Path.home()), start_new_session=True, close_fds=True)
        try:
            proc.communicate(payload, timeout=timeout)
            status = f"rc={proc.returncode}"
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=3)
                    break
                except subprocess.TimeoutExpired:
                    continue
            status = f"TIMEOUT after {timeout:g}s (killed)"
        except BrokenPipeError:
            proc.wait()
            status = f"rc={proc.returncode} (did not read stdin)"
        out.seek(0)
        text = out.read(OUTPUT_KEEP + 1).decode("utf-8", "replace")
    except OSError as e:
        status, text = f"FAILED to start: {e}", ""
    finally:
        out.close()
    ms = int((time.monotonic() - t0) * 1000)
    if len(text) > OUTPUT_KEEP:
        text = text[:OUTPUT_KEEP] + "..."
    from .store import now
    ev = env
    _log(db_path, f"{now()} {label} seq={ev['FTASK_SEQ']} task={ev['FTASK_ID']} event={ev['FTASK_EVENT']} "
                  f"{status} {ms}ms" + (f" output={json.dumps(text, ensure_ascii=False)}" if text.strip() else ""))
    return {"label": label, "status": status}


def run_events(db_path, seqs) -> int:
    """Runner entry point: execute hooks and notify commands for the given human events."""
    from . import store as st
    conn = st.connect(db_path)
    try:
        notify = {}
        events = []
        for seq in seqs:
            evs = st.Store._events_after(conn, seq - 1, limit=1)
            if not evs or evs[0]["seq"] != seq:
                continue
            ev = evs[0]
            if not is_human_event(ev):
                continue
            row = conn.execute("SELECT notify_cmd FROM tasks WHERE id=?", (ev["task_id"],)).fetchone()
            notify[seq] = (row["notify_cmd"] if row else "") or ""
            events.append(ev)
    finally:
        conn.close()
    timeout = hook_timeout()
    for ev in events:
        payload = json.dumps(ev, ensure_ascii=False).encode("utf-8")
        env = _child_env(db_path, ev)
        for hook in hook_files(db_path, ev["kind"]):
            _run_one(db_path, hook.name, [str(hook)], payload, env, timeout)
        cmd = notify.get(ev["seq"])
        if cmd:
            _run_one(db_path, "notify_cmd", [_shell(), "-c", cmd], payload, env, timeout)
    return 0
