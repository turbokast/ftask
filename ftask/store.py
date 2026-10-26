"""SQLite store for ftask. Python 3 standard library only.

Every write runs inside BEGIN IMMEDIATE so concurrent processes (agents, cron,
the web server) serialise cleanly; WAL + busy_timeout make waiting cheap.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import files as fs
from . import paths

STATUSES = ("open", "waiting", "done", "cancelled")
CLOSED = ("done", "cancelled")
DEFAULT_PRIORITY = 100

ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"  # no 0/1/i/l/o
ID_LEN = 6

LIMITS = {
    "project": 100,
    "title": 500,
    "why": 5000,
    "key": 200,
    "waiting_on": 1000,
    "created_by": 100,
    "note": 20000,
    "step_text": 2000,
    "step_value": 200000,
    "step_link": 2000,
    "steps": 100,
    "notify_cmd": 2000,
    "step_label": 100,
    "accept": 200,
    "file_label": 200,
}

BUSY_TIMEOUT_MS = 30000


class FtaskError(Exception):
    """Base error; `code` is the CLI exit code."""

    code = 1


class ValidationError(FtaskError):
    code = 2


class NotFound(FtaskError):
    code = 3


class Ambiguous(FtaskError):
    code = 4


class DatabaseBusy(FtaskError):
    code = 5


class StorageError(FtaskError):
    """Disk full / cannot write the file store."""

    code = 6


class TooLarge(ValidationError):
    pass


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def now() -> str:
    """UTC ISO-8601 with millisecond precision; lexicographically sortable."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_REL = re.compile(r"^(\d+)\s*([smhdw])$")
_REL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(text: str) -> str:
    """Accept ISO date/datetime (UTC assumed when no offset) or relative like 2h, 7d."""
    s = (text or "").strip()
    if not s:
        raise ValidationError("empty time value")
    m = _REL.match(s)
    if m:
        dt = datetime.now(timezone.utc) - timedelta(seconds=int(m.group(1)) * _REL_UNITS[m.group(2)])
    else:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            raise ValidationError(
                f"cannot parse time {text!r}; use ISO-8601 (2026-10-22, 2026-10-22T09:30:00Z) or relative (30m, 2h, 7d)"
            )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# schema / migrations
# --------------------------------------------------------------------------

MIGRATIONS = [
    # v1
    """
    CREATE TABLE tasks (
        id          TEXT PRIMARY KEY,
        key         TEXT UNIQUE,
        project     TEXT NOT NULL,
        title       TEXT NOT NULL,
        why         TEXT NOT NULL DEFAULT '',
        minutes     INTEGER,
        priority    INTEGER NOT NULL DEFAULT 100,
        status      TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','waiting','done','cancelled')),
        waiting_on  TEXT NOT NULL DEFAULT '',
        steps       TEXT NOT NULL DEFAULT '[]',
        steps_done  TEXT NOT NULL DEFAULT '[]',
        note        TEXT NOT NULL DEFAULT '',
        note_at     TEXT,
        created_by  TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        done_at     TEXT
    );
    CREATE INDEX idx_tasks_status ON tasks(status, priority, created_at);
    CREATE INDEX idx_tasks_project ON tasks(project COLLATE NOCASE);
    CREATE TABLE events (
        seq      INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id  TEXT NOT NULL,
        at       TEXT NOT NULL,
        actor    TEXT NOT NULL,
        kind     TEXT NOT NULL,
        detail   TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX idx_events_task ON events(task_id, seq);
    CREATE INDEX idx_events_at ON events(at);
    """,
    # v2: file attachments (both directions), per-task notify command, task-level upload request,
    # and a registry of running `ftask wait` processes (so the UI can say whether anyone is listening)
    """
    ALTER TABLE tasks ADD COLUMN notify_cmd   TEXT NOT NULL DEFAULT '';
    ALTER TABLE tasks ADD COLUMN upload       INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE tasks ADD COLUMN accept       TEXT NOT NULL DEFAULT '';
    ALTER TABLE tasks ADD COLUMN upload_label TEXT NOT NULL DEFAULT '';
    CREATE TABLE files (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     TEXT NOT NULL,
        step_index  INTEGER,
        direction   TEXT NOT NULL CHECK (direction IN ('founder','agent')),
        label       TEXT NOT NULL DEFAULT '',
        name        TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        size        INTEGER NOT NULL,
        sha256      TEXT NOT NULL,
        mime        TEXT NOT NULL,
        uploaded_by TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_files_task ON files(task_id, id);
    CREATE TABLE waiters (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        pid        INTEGER NOT NULL,
        pidstart   TEXT NOT NULL DEFAULT '',
        task_id    TEXT,
        key_prefix TEXT,
        for_kind   TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    # v3: who last acted on a task from the agent side (terminal pane, session, folder), so human events
    # can be pushed back to that agent (see hooks.owner_context and the FTASK_OWNER hook variable)
    """
    CREATE TABLE owners (
        task_id  TEXT PRIMARY KEY,
        context  TEXT NOT NULL DEFAULT '{}',
        at       TEXT NOT NULL
    );
    """,
]


def default_db_path() -> Path:
    return paths.default_db_path()


class Conn(sqlite3.Connection):
    """Connection that remembers its db path and the event seqs written in the open transaction."""

    db_path = None
    new_events = None


def connect(path=None) -> sqlite3.Connection:
    path = Path(path) if path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    last_err = None
    for attempt in range(8):
        try:
            conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None, factory=Conn)
            conn.db_path = path
            conn.new_events = []
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            migrate(conn)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return conn
        except sqlite3.OperationalError as e:  # first-open races on WAL switch
            last_err = e
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(0.05 * (attempt + 1) + random.random() * 0.05)
    raise DatabaseBusy(f"could not open database {path}: {last_err}")


def migrate(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] >= len(MIGRATIONS):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]  # re-check under the lock
        for i in range(ver, len(MIGRATIONS)):
            for stmt in _split_sql(MIGRATIONS[i]):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={i + 1}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _split_sql(script: str):
    # our migration scripts contain no semicolons inside literals
    return [s.strip() for s in script.split(";") if s.strip()]


class _Tx:
    """with _Tx(conn): ...  -> BEGIN IMMEDIATE / COMMIT / ROLLBACK."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            raise DatabaseBusy(f"database is busy: {e}")
        self.conn.new_events = []
        return self.conn

    def __exit__(self, et, ev, tb):
        if et is None:
            self.conn.execute("COMMIT")
            seqs, self.conn.new_events = self.conn.new_events, []
            if seqs:
                try:  # hooks must never break a write that has already committed
                    from . import hooks
                    hooks.after_commit(self.conn, seqs)
                except Exception:
                    pass
        else:
            self.conn.new_events = []
            self.conn.execute("ROLLBACK")
        return False


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _str(name, v, limit, *, required=False, multiline=False):
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise ValidationError(f"{name} must be a string")
    v = v.replace("\r\n", "\n").strip() if not multiline else v.replace("\r\n", "\n")
    if multiline:
        v = v.strip("\n")
    if _CTRL.search(v):
        raise ValidationError(f"{name} contains control characters")
    if required and not v.strip():
        raise ValidationError(f"{name} is required")
    if len(v) > limit:
        raise ValidationError(f"{name} is too long ({len(v)} > {limit} characters)")
    if not multiline and "\n" in v:
        raise ValidationError(f"{name} must be a single line")
    return v


def _int(name, v, lo, hi, allow_none=False):
    if v is None or v == "":
        if allow_none:
            return None
        raise ValidationError(f"{name} is required")
    if isinstance(v, bool):
        raise ValidationError(f"{name} must be an integer")
    try:
        if isinstance(v, float):
            if v != int(v):
                raise ValueError
        i = int(v)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be an integer, got {v!r}")
    if not lo <= i <= hi:
        raise ValidationError(f"{name} must be between {lo} and {hi}")
    return i


def normalize_link(v):
    v = _str("link", v, LIMITS["step_link"])
    if not v:
        return ""
    if not re.match(r"^(https?://|mailto:)\S+$", v, re.I):
        raise ValidationError(f"link must be an http(s):// or mailto: URL without spaces, got {v!r}")
    return v


_ACCEPT_TOKEN = re.compile(r"^(\.[A-Za-z0-9_+-]{1,20}|[A-Za-z0-9.+-]+/(\*|[A-Za-z0-9.+_-]+))$")


def normalize_accept(v, what="accept"):
    """`accept` is the browser file-picker filter: comma list of .ext, type/* or type/sub (advisory only)."""
    v = _str(what, v, LIMITS["accept"])
    if not v:
        return ""
    toks = [t.strip() for t in v.split(",") if t.strip()]
    for t in toks:
        if not _ACCEPT_TOKEN.match(t):
            raise ValidationError(f"{what} entries must look like '.csv', 'video/*' or 'application/pdf', got {t!r}")
    return ",".join(toks)


def _abs_file(path, what):
    if not isinstance(path, str) or not path.strip():
        raise ValidationError(f"{what} must be a file path")
    p = os.path.abspath(os.path.expanduser(path))
    try:
        st = os.stat(p)
    except OSError as e:
        raise ValidationError(f"{what}: cannot read {path!r}: {e.strerror or e}")
    if not stat.S_ISREG(st.st_mode):
        raise ValidationError(f"{what}: {path!r} is not a regular file")
    return p


def _attach_list(v, what):
    if v is None or v == "" or v == []:
        return []
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list):
        raise ValidationError(f"{what} must be a path or a list of paths")
    return [_abs_file(x, what) for x in items]


def normalize_steps(steps, attach_out=None):
    """Validate steps. `attach` (path or list of paths) is not stored in the step: it is collected into
    attach_out as (step_index, absolute_path) and copied in after the task is saved."""
    if steps is None:
        return []
    if not isinstance(steps, list):
        raise ValidationError("steps must be a list")
    if len(steps) > LIMITS["steps"]:
        raise ValidationError(f"too many steps (max {LIMITS['steps']})")
    out = []
    for i, s in enumerate(steps, 1):
        if isinstance(s, str):
            s = {"text": s}
        if not isinstance(s, dict):
            raise ValidationError(f"step {i} must be a string or an object with text/value/link")
        unknown = set(s) - {"text", "value", "link", "upload", "accept", "label", "attach"}
        if unknown:
            raise ValidationError(f"step {i} has unknown fields: {', '.join(sorted(unknown))}")
        step = {"text": _str(f"step {i} text", s.get("text"), LIMITS["step_text"], required=True)}
        value = s.get("value")
        if value is not None and value != "":
            if not isinstance(value, str):
                raise ValidationError(f"step {i} value must be a string")
            value = value.replace("\r\n", "\n")
            if _CTRL.search(value):
                raise ValidationError(f"step {i} value contains control characters")
            if len(value) > LIMITS["step_value"]:
                raise ValidationError(f"step {i} value is too long")
            step["value"] = value  # kept byte-exact (it is meant to be pasted)
        link = s.get("link")
        if link:
            try:
                step["link"] = normalize_link(link)
            except ValidationError as e:
                raise ValidationError(f"step {i}: {e}")
        up = s.get("upload")
        if up is not None and not isinstance(up, bool):
            raise ValidationError(f"step {i} upload must be true or false")
        accept = normalize_accept(s.get("accept"), f"step {i} accept")
        label = _str(f"step {i} label", s.get("label"), LIMITS["step_label"])
        if (accept or label) and not up:
            raise ValidationError(f"step {i}: accept/label only make sense with \"upload\": true")
        if up:
            step["upload"] = True
            if accept:
                step["accept"] = accept
            if label:
                step["label"] = label
        out.append(step)
        if attach_out is not None:
            for path in _attach_list(s.get("attach"), f"step {i} attach"):
                attach_out.append((i - 1, path))
    return out


FIELD_ORDER = ("project", "title", "why", "minutes", "priority", "key", "waiting_on", "steps", "created_by", "status",
               "notify_cmd", "upload", "accept", "upload_label", "attach")


def normalize_fields(data: dict) -> dict:
    """Validate the fields that are present; return only those."""
    if not isinstance(data, dict):
        raise ValidationError("task must be a JSON object")
    data = dict(data)
    if "by" in data and "created_by" not in data:
        data["created_by"] = data.pop("by")
    unknown = set(data) - set(FIELD_ORDER) - {"id"}
    if unknown:
        raise ValidationError(f"unknown task fields: {', '.join(sorted(unknown))}")
    out = {}
    if "project" in data:
        out["project"] = _str("project", data["project"], LIMITS["project"], required=True)
    if "title" in data:
        out["title"] = _str("title", data["title"], LIMITS["title"], required=True)
    if "why" in data:
        out["why"] = _str("why", data["why"], LIMITS["why"], multiline=True)
    if "minutes" in data:
        out["minutes"] = _int("minutes", data["minutes"], 0, 100000, allow_none=True)
    if "priority" in data:
        out["priority"] = _int("priority", data["priority"], -1000000, 1000000)
    if "key" in data:
        k = _str("key", data["key"], LIMITS["key"])
        if k and re.search(r"\s", k):
            raise ValidationError("key must not contain whitespace")
        out["key"] = k or None
    if "waiting_on" in data:
        out["waiting_on"] = _str("waiting_on", data["waiting_on"], LIMITS["waiting_on"])
    attach = []
    if "steps" in data:
        out["steps"] = normalize_steps(data["steps"], attach)
    if "attach" in data:
        attach.extend((None, p) for p in _attach_list(data["attach"], "attach"))
    if attach:
        out["_attach"] = attach
    if "notify_cmd" in data:
        out["notify_cmd"] = _str("notify_cmd", data["notify_cmd"], LIMITS["notify_cmd"])
    if "upload" in data:
        if data["upload"] is not None and not isinstance(data["upload"], bool):
            raise ValidationError("upload must be true or false")
        out["upload"] = bool(data["upload"])
    if "accept" in data:
        out["accept"] = normalize_accept(data["accept"])
    if "upload_label" in data:
        out["upload_label"] = _str("upload_label", data["upload_label"], LIMITS["step_label"])
    if "created_by" in data:
        out["created_by"] = _str("created_by", data["created_by"], LIMITS["created_by"])
    if "status" in data:
        if data["status"] not in STATUSES:
            raise ValidationError(f"status must be one of {', '.join(STATUSES)}")
        out["status"] = data["status"]
    return out


def _pid_start(pid) -> str:
    """Process start time from /proc (guards against pid reuse); '' when unavailable."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("utf-8", "replace")
        return data.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def _pid_alive(pid, pidstart="") -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if pidstart:
        cur = _pid_start(pid)
        if cur and cur != pidstart:
            return False
    return True


def _actor(actor):
    return (actor or "cli").strip()[: LIMITS["created_by"]] or "cli"


# --------------------------------------------------------------------------
# row <-> dict
# --------------------------------------------------------------------------

def row_to_task(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "key": r["key"],
        "project": r["project"],
        "title": r["title"],
        "why": r["why"],
        "minutes": r["minutes"],
        "priority": r["priority"],
        "status": r["status"],
        "waiting_on": r["waiting_on"],
        "steps": json.loads(r["steps"]),
        "steps_done": json.loads(r["steps_done"]),
        "note": r["note"],
        "note_at": r["note_at"],
        "created_by": r["created_by"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "done_at": r["done_at"],
        "notify_cmd": r["notify_cmd"],
        "upload": bool(r["upload"]),
        "accept": r["accept"],
        "upload_label": r["upload_label"],
    }


def row_to_event(r: sqlite3.Row) -> dict:
    d = {
        "seq": r["seq"],
        "task_id": r["task_id"],
        "at": r["at"],
        "actor": r["actor"],
        "kind": r["kind"],
        "detail": json.loads(r["detail"]),
    }
    keys = r.keys()
    for extra in ("task_key", "task_title", "task_project"):
        if extra in keys:
            d[extra] = r[extra]
    return d


def _event(conn, task_id, actor, kind, detail=None, at=None):
    cur = conn.execute(
        "INSERT INTO events(task_id, at, actor, kind, detail) VALUES (?,?,?,?,?)",
        (task_id, at or now(), _actor(actor), kind, json.dumps(detail or {}, ensure_ascii=False)),
    )
    if getattr(conn, "new_events", None) is not None:
        conn.new_events.append(cur.lastrowid)
    return cur.lastrowid


def _new_id(conn) -> str:
    for _ in range(100):
        cand = "".join(random.choice(ID_ALPHABET) for _ in range(ID_LEN))
        if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (cand,)).fetchone():
            return cand
    raise FtaskError("could not allocate a task id")


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------

def resolve(conn, ref: str) -> sqlite3.Row:
    """Find a task by id, then exact key, then unique id prefix."""
    ref = (ref or "").strip()
    if not ref:
        raise ValidationError("task id or key required")
    r = conn.execute("SELECT * FROM tasks WHERE id=?", (ref,)).fetchone()
    if r:
        return r
    r = conn.execute("SELECT * FROM tasks WHERE key=?", (ref,)).fetchone()
    if r:
        return r
    like = ref.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute("SELECT * FROM tasks WHERE id LIKE ? ESCAPE '\\' LIMIT 5", (like,)).fetchall()
    if len(rows) == 1 and len(ref) >= 3:
        return rows[0]
    if len(rows) > 1:
        raise Ambiguous(f"{ref!r} matches several tasks: " + ", ".join(r["id"] for r in rows))
    raise NotFound(f"no task with id or key {ref!r}")


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

def file_to_dict(r, root: Path, task_key=None) -> dict:
    step_index = r["step_index"]
    return {
        "id": r["id"],
        "task_id": r["task_id"],
        "task_key": task_key,
        "step_index": step_index,
        "step": None if step_index is None else step_index + 1,
        "direction": r["direction"],
        "label": r["label"],
        "name": r["name"],
        "path": str(root / r["stored_path"]),
        "size": r["size"],
        "sha256": r["sha256"],
        "mime": r["mime"],
        "uploaded_by": r["uploaded_by"],
        "created_at": r["created_at"],
    }


def _root(c) -> Path:
    return fs.files_root(c.db_path)


def _task(c, r) -> dict:
    """row -> task dict including its files."""
    t = row_to_task(r)
    root = _root(c)
    t["files"] = [file_to_dict(f, root, t["key"]) for f in
                  c.execute("SELECT * FROM files WHERE task_id=? ORDER BY id", (t["id"],))]
    return t


def _tasks(c, rows) -> list:
    root = _root(c)
    by_task = {}
    for f in c.execute("SELECT * FROM files ORDER BY id"):
        by_task.setdefault(f["task_id"], []).append(f)
    out = []
    for r in rows:
        t = row_to_task(r)
        t["files"] = [file_to_dict(f, root, t["key"]) for f in by_task.get(t["id"], [])]
        out.append(t)
    return out


class Store:
    """Thin facade: each call opens a short-lived connection (cheap with WAL),
    so instances are safe to share between threads and processes."""

    def __init__(self, path=None):
        self.path = Path(path) if path else default_db_path()
        connect(self.path).close()  # create + migrate eagerly so errors surface early

    def _conn(self):
        return connect(self.path)

    # ---- reads ---------------------------------------------------------

    def version(self) -> int:
        c = self._conn()
        try:
            return c.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        finally:
            c.close()

    def get(self, ref) -> dict:
        c = self._conn()
        try:
            return _task(c, resolve(c, ref))
        finally:
            c.close()

    def task_events(self, ref) -> list:
        c = self._conn()
        try:
            t = resolve(c, ref)
            rows = c.execute("SELECT * FROM events WHERE task_id=? ORDER BY seq", (t["id"],)).fetchall()
            return [row_to_event(r) for r in rows]
        finally:
            c.close()

    def list(self, statuses=("open", "waiting"), project=None, closed_limit=None) -> list:
        c = self._conn()
        try:
            return self._list(c, statuses, project, closed_limit)
        finally:
            c.close()

    @staticmethod
    def _list(c, statuses, project, closed_limit=None):
        q = "SELECT * FROM tasks"
        where, args = [], []
        if statuses:
            for s in statuses:
                if s not in STATUSES:
                    raise ValidationError(f"unknown status {s!r}")
            where.append("status IN (%s)" % ",".join("?" * len(statuses)))
            args += list(statuses)
        if project:
            where.append("project = ? COLLATE NOCASE")
            args.append(project)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY priority ASC, created_at ASC, id ASC"
        tasks = _tasks(c, c.execute(q, args).fetchall())
        closed = [t for t in tasks if t["status"] in CLOSED]
        if closed:
            closed.sort(key=lambda t: t["done_at"] or t["updated_at"], reverse=True)
            if closed_limit is not None:
                closed = closed[:closed_limit]
            tasks = [t for t in tasks if t["status"] not in CLOSED] + closed
        return tasks

    def snapshot(self, closed_limit=300) -> dict:
        """One consistent read for the UI: tasks + per-project counts + version."""
        c = self._conn()
        try:
            c.execute("BEGIN")
            version = c.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            tasks = self._list(c, None, None, closed_limit)
            counts = {}
            for r in c.execute("SELECT project, status, COUNT(*) n FROM tasks GROUP BY project, status"):
                counts.setdefault(r["project"], {s: 0 for s in STATUSES})[r["status"]] = r["n"]
            c.execute("COMMIT")
            return {"version": version, "tasks": tasks, "counts": counts}
        finally:
            c.close()

    def projects(self) -> list:
        c = self._conn()
        try:
            return [r[0] for r in c.execute("SELECT DISTINCT project FROM tasks ORDER BY project COLLATE NOCASE")]
        finally:
            c.close()

    def events(self, since=None, after_seq=None, limit=None) -> list:
        c = self._conn()
        try:
            q = (
                "SELECT e.*, t.key AS task_key, t.title AS task_title, t.project AS task_project "
                "FROM events e LEFT JOIN tasks t ON t.id = e.task_id"
            )
            where, args = [], []
            if since:
                where.append("e.at > ?")
                args.append(since)
            if after_seq is not None:
                where.append("e.seq > ?")
                args.append(int(after_seq))
            if where:
                q += " WHERE " + " AND ".join(where)
            q += " ORDER BY e.seq"
            if limit:
                q += f" LIMIT {int(limit)}"
            return [row_to_event(r) for r in c.execute(q, args)]
        finally:
            c.close()

    def notes(self, since=None, recent_days=7) -> list:
        """Tasks carrying a human note and/or closed (done/cancelled) recently.

        With `since`: notes written after it, and tasks closed after it.
        Without: every task with a note, plus tasks closed in the last `recent_days`.
        """
        c = self._conn()
        try:
            close_since = since or (datetime.now(timezone.utc) - timedelta(days=recent_days)).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            closed_rows = c.execute(
                "SELECT task_id, MAX(at) AS at, kind FROM events WHERE kind IN ('done','cancelled') AND at > ? "
                "GROUP BY task_id",
                (close_since,),
            ).fetchall()
            closed_at = {r["task_id"]: r["at"] for r in closed_rows}
            out = []
            for r in c.execute("SELECT * FROM tasks ORDER BY updated_at DESC"):
                t = row_to_task(r)
                has_note = bool(t["note"]) and (not since or (t["note_at"] or "") > since)
                just_closed = t["status"] in CLOSED and t["id"] in closed_at
                if not (has_note or just_closed):
                    continue
                out.append(
                    {
                        "id": t["id"],
                        "key": t["key"],
                        "project": t["project"],
                        "title": t["title"],
                        "status": t["status"],
                        "note": t["note"],
                        "note_at": t["note_at"],
                        "closed_at": closed_at.get(t["id"]) if just_closed else None,
                        "steps_done": len(t["steps_done"]),
                        "steps_total": len(t["steps"]),
                        "waiting_on": t["waiting_on"],
                        "updated_at": t["updated_at"],
                    }
                )
            return out
        finally:
            c.close()

    # ---- writes --------------------------------------------------------

    def upsert(self, data: dict, actor="cli", reopen=False) -> dict:
        """Create a task, or update in place when `key` matches an existing one.

        Returns {"task", "action": created|updated|unchanged, "closed": bool,
        "reopened": bool, "changed": [field names]}.
        """
        fields = normalize_fields(data)
        attach = fields.pop("_attach", [])
        c = self._conn()
        try:
            with _Tx(c):
                existing = None
                if fields.get("key"):
                    r = c.execute("SELECT * FROM tasks WHERE key=?", (fields["key"],)).fetchone()
                    existing = _task(c, r) if r else None
                if existing is None:
                    res = self._create(c, fields, actor)
                else:
                    res = self._update(c, existing, fields, actor, reopen)
        finally:
            c.close()
        res["attached"] = []
        if attach:
            nsteps = len(res["task"]["steps"])
            for idx, path in attach:
                if idx is not None and idx >= nsteps:
                    continue
                res["attached"].append(self.attach_path(res["task"]["id"], path, step_index=idx, by=actor))
            res["task"] = self.get(res["task"]["id"])
        return res

    def _create(self, c, f, actor):
        for req in ("project", "title"):
            if not f.get(req):
                raise ValidationError(f"{req} is required")
        status = f.get("status") or ("waiting" if f.get("waiting_on") else "open")
        if status == "waiting" and not f.get("waiting_on"):
            raise ValidationError("status waiting requires waiting_on")
        ts = now()
        tid = _new_id(c)
        c.execute(
            "INSERT INTO tasks(id,key,project,title,why,minutes,priority,status,waiting_on,steps,steps_done,"
            "note,created_by,created_at,updated_at,done_at,notify_cmd,upload,accept,upload_label) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid,
                f.get("key"),
                f["project"],
                f["title"],
                f.get("why", ""),
                f.get("minutes"),
                f.get("priority", DEFAULT_PRIORITY),
                status,
                f.get("waiting_on", "") if status == "waiting" else "",
                json.dumps(f.get("steps", []), ensure_ascii=False),
                "[]",
                "",
                f.get("created_by") or _actor(actor),
                ts,
                ts,
                ts if status in CLOSED else None,
                f.get("notify_cmd", ""),
                1 if f.get("upload") else 0,
                f.get("accept", ""),
                f.get("upload_label", ""),
            ),
        )
        _event(c, tid, actor, "created", {"key": f.get("key"), "project": f["project"], "title": f["title"]}, ts)
        task = _task(c, c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        return {"task": task, "action": "created", "closed": False, "reopened": False, "changed": []}

    def _update(self, c, ex, f, actor, reopen):
        changed = []
        sets = {}
        for name in ("project", "title", "why", "minutes", "priority", "notify_cmd", "upload", "accept", "upload_label"):
            if name in f and f[name] != ex[name]:
                sets[name] = int(f[name]) if name == "upload" else f[name]
                changed.append(name)
        steps_reset = False
        if "steps" in f and f["steps"] != ex["steps"]:
            sets["steps"] = json.dumps(f["steps"], ensure_ascii=False)
            changed.append("steps")
            if len(f["steps"]) != len(ex["steps"]):
                sets["steps_done"] = "[]"
                steps_reset = ex["steps_done"] != []
            else:
                keep = [i for i in ex["steps_done"] if i < len(f["steps"])]
                if keep != ex["steps_done"]:
                    sets["steps_done"] = json.dumps(keep)
        is_closed = ex["status"] in CLOSED
        new_status = ex["status"]
        new_waiting = ex["waiting_on"]
        reopened = False
        if "waiting_on" in f:
            new_waiting = f["waiting_on"]
        if is_closed and reopen:
            reopened = True
            new_status = f.get("status") if f.get("status") in ("open", "waiting") else "open"
            if new_waiting and "status" not in f:
                new_status = "waiting"
        elif not is_closed:
            if "status" in f:
                new_status = f["status"]
            elif "waiting_on" in f:
                if new_waiting and ex["status"] == "open":
                    new_status = "waiting"
                elif not new_waiting and ex["status"] == "waiting":
                    new_status = "open"
        if is_closed and not reopened:
            new_waiting = ex["waiting_on"]  # leave closed tasks' waiting_on alone
        elif new_status != "waiting":
            new_waiting = ""
        elif not new_waiting:
            raise ValidationError("status waiting requires waiting_on")
        if new_waiting != ex["waiting_on"]:
            sets["waiting_on"] = new_waiting
            changed.append("waiting_on")
        status_changed = new_status != ex["status"]
        if status_changed:
            sets["status"] = new_status
            sets["done_at"] = now() if new_status in CLOSED else None
        if not sets:
            return {"task": ex, "action": "unchanged", "closed": is_closed and not reopened,
                    "reopened": False, "changed": []}
        ts = now()
        sets["updated_at"] = ts
        cols = ", ".join(f"{k}=?" for k in sets)
        c.execute(f"UPDATE tasks SET {cols} WHERE id=?", list(sets.values()) + [ex["id"]])
        if changed:
            _event(c, ex["id"], actor, "updated", {"fields": changed, "steps_reset": steps_reset}, ts)
        if status_changed:
            kind = {"open": "reopened", "waiting": "waiting", "done": "done", "cancelled": "cancelled"}[new_status]
            _event(c, ex["id"], actor, kind, {"from": ex["status"], "waiting_on": new_waiting}, ts)
        task = _task(c, c.execute("SELECT * FROM tasks WHERE id=?", (ex["id"],)).fetchone())
        return {"task": task, "action": "updated", "closed": is_closed and not reopened,
                "reopened": reopened, "changed": changed}

    def set_status(self, ref, status, actor="cli", waiting_on=None) -> dict:
        if status not in STATUSES:
            raise ValidationError(f"status must be one of {', '.join(STATUSES)}")
        if waiting_on is not None:
            waiting_on = _str("waiting_on", waiting_on, LIMITS["waiting_on"])
        c = self._conn()
        try:
            with _Tx(c):
                t = resolve(c, ref)
                if status == "waiting":
                    wo = waiting_on if waiting_on is not None else t["waiting_on"]
                    if not wo:
                        raise ValidationError("waiting requires a reason: waiting_on text")
                else:
                    wo = ""
                if t["status"] == status and (status != "waiting" or wo == t["waiting_on"]):
                    return {"task": _task(c, t), "changed": False}
                ts = now()
                c.execute(
                    "UPDATE tasks SET status=?, waiting_on=?, done_at=?, updated_at=? WHERE id=?",
                    (status, wo, ts if status in CLOSED else None, ts, t["id"]),
                )
                kind = {"open": "reopened", "waiting": "waiting", "done": "done", "cancelled": "cancelled"}[status]
                _event(c, t["id"], actor, kind, {"from": t["status"], "waiting_on": wo}, ts)
                return {"task": _task(c, c.execute("SELECT * FROM tasks WHERE id=?", (t["id"],)).fetchone()),
                        "changed": True}
        finally:
            c.close()

    def set_note(self, ref, note, actor="cli") -> dict:
        note = _str("note", note, LIMITS["note"], multiline=True)
        c = self._conn()
        try:
            with _Tx(c):
                t = resolve(c, ref)
                if t["note"] == note:
                    return {"task": _task(c, t), "changed": False}
                ts = now()
                c.execute("UPDATE tasks SET note=?, note_at=?, updated_at=? WHERE id=?", (note, ts, ts, t["id"]))
                _event(c, t["id"], actor, "note", {"note": note, "previous": t["note"]}, ts)
                return {"task": _task(c, c.execute("SELECT * FROM tasks WHERE id=?", (t["id"],)).fetchone()),
                        "changed": True}
        finally:
            c.close()

    def set_step(self, ref, index, done, actor="cli") -> dict:
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValidationError("step index must be an integer")
        if not isinstance(done, bool):
            raise ValidationError("done must be true or false")
        c = self._conn()
        try:
            with _Tx(c):
                t = resolve(c, ref)
                steps = json.loads(t["steps"])
                if not 0 <= index < len(steps):
                    raise ValidationError(f"step index {index} out of range (task has {len(steps)} steps)")
                cur = json.loads(t["steps_done"])
                new = sorted(set(cur) | {index}) if done else [i for i in cur if i != index]
                if new == cur:
                    return {"task": _task(c, t), "changed": False}
                ts = now()
                c.execute("UPDATE tasks SET steps_done=?, updated_at=? WHERE id=?", (json.dumps(new), ts, t["id"]))
                _event(c, t["id"], actor, "step",
                       {"index": index, "step": index + 1, "done": done, "text": steps[index]["text"]}, ts)
                return {"task": _task(c, c.execute("SELECT * FROM tasks WHERE id=?", (t["id"],)).fetchone()),
                        "changed": True}
        finally:
            c.close()

    # ---- files ---------------------------------------------------------

    def files_root(self) -> Path:
        return fs.files_root(self.path)

    def list_files(self, ref=None, direction=None) -> list:
        c = self._conn()
        try:
            root = _root(c)
            q = ("SELECT f.*, t.key AS task_key FROM files f LEFT JOIN tasks t ON t.id=f.task_id")
            where, args = [], []
            if ref is not None:
                where.append("f.task_id=?")
                args.append(resolve(c, ref)["id"])
            if direction:
                where.append("f.direction=?")
                args.append(direction)
            if where:
                q += " WHERE " + " AND ".join(where)
            q += " ORDER BY f.id"
            return [file_to_dict(r, root, r["task_key"]) for r in c.execute(q, args)]
        finally:
            c.close()

    def open_file(self, file_id):
        """-> (file dict, open binary file object, size) for streaming a download; refuses symlinks."""
        d = self.get_file(file_id)
        c = self._conn()
        try:
            rel = c.execute("SELECT stored_path FROM files WHERE id=?", (file_id,)).fetchone()["stored_path"]
        finally:
            c.close()
        try:
            f, size = fs.open_stored(fs.files_root(self.path), rel)
        except (OSError, PermissionError):
            raise NotFound(f"file #{file_id} is missing from the file store")
        return d, f, size

    def get_file(self, file_id) -> dict:
        c = self._conn()
        try:
            r = c.execute("SELECT f.*, t.key AS task_key FROM files f LEFT JOIN tasks t ON t.id=f.task_id "
                          "WHERE f.id=?", (file_id,)).fetchone()
            if not r:
                raise NotFound(f"no file with id {file_id}")
            return file_to_dict(r, _root(c), r["task_key"])
        finally:
            c.close()

    def ingest(self, ref, reader, *, name, size=None, step_index=None, label="", client_mime=None,
               by="cli", direction="agent", max_size=None, dedupe=False) -> dict:
        """Stream `reader` (anything with .read(n)) into the file store and record it on the task.

        Memory use is one chunk. `size`: exact number of bytes expected (HTTP uploads) or None to read to EOF.
        With dedupe=True (agent attachments) an identical file with the same name/step is a no-op and a changed
        file with the same name/step replaces the old one.
        Returns {"file": dict, "action": "created"|"unchanged"|"replaced"}.
        """
        if direction not in ("founder", "agent"):
            raise ValidationError("bad direction")
        label = _str("label", label, LIMITS["file_label"])
        name = fs.sanitize_name(name)
        c = self._conn()
        try:
            t = resolve(c, ref)
            nsteps = len(json.loads(t["steps"]))
        finally:
            c.close()
        if step_index is not None:
            if isinstance(step_index, bool) or not isinstance(step_index, int) or not 0 <= step_index < nsteps:
                raise ValidationError(f"step must be between 1 and {nsteps} (task has {nsteps} steps)"
                                      if nsteps else "this task has no steps")
        if size is not None and max_size is not None and size > max_size:
            raise TooLarge(f"file is too large ({fs.format_size(size)}; the limit is {fs.format_size(max_size)})")
        root = fs.files_root(self.path)
        try:
            if size is not None:
                need = size + fs.SPACE_RESERVE
                free = fs.free_space(root)
                if free < need:
                    raise StorageError(f"not enough disk space: the file needs {fs.format_size(size)} but only "
                                       f"{fs.format_size(max(0, free - fs.SPACE_RESERVE))} is free")
            tdir = fs.ensure_task_dir(root, t["id"])
        except PermissionError as e:
            raise StorageError(str(e))
        except OSError as e:
            raise StorageError(f"cannot create the file store at {root}: {e.strerror or e}")
        tmp = tdir / f".incoming-{secrets.token_hex(8)}"
        final = None
        try:
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                digest = hashlib.sha256()
                total = 0
                with os.fdopen(fd, "wb") as out:
                    while True:
                        want = fs.CHUNK if size is None else min(fs.CHUNK, size - total)
                        if want <= 0:
                            break
                        buf = reader.read(want)
                        if not buf:
                            break
                        total += len(buf)
                        if max_size is not None and total > max_size:
                            raise TooLarge(f"file is too large (the limit is {fs.format_size(max_size)})")
                        digest.update(buf)
                        out.write(buf)
                    if size is not None and total != size:
                        raise ValidationError(f"upload ended early ({total} of {size} bytes received)")
                    out.flush()
                    os.fsync(out.fileno())
            except OSError as e:
                if fs.is_enospc(e):
                    raise StorageError("the disk is full: the file could not be stored")
                raise
            sha = digest.hexdigest()
            mime = fs.guess_mime(name, client_mime)
            removed = []
            c = self._conn()
            try:
                with _Tx(c):
                    t = resolve(c, ref)
                    if dedupe:
                        same = c.execute(
                            "SELECT * FROM files WHERE task_id=? AND direction=? AND name=? AND step_index IS ? "
                            "ORDER BY id", (t["id"], direction, name, step_index)).fetchall()
                        for old in same:
                            if old["sha256"] == sha and old["label"] == label:
                                os.unlink(tmp)
                                return {"file": file_to_dict(old, _root(c), t["key"]), "action": "unchanged"}
                        removed = list(same)
                    reuse = None  # a replaced file keeps its path, so the name the human sees never changes
                    if removed:
                        try:
                            cand = fs.resolve_stored(root, removed[0]["stored_path"])
                            if stat.S_ISREG(os.lstat(cand).st_mode):
                                reuse = cand
                        except (OSError, PermissionError):
                            reuse = None
                    if reuse is None:
                        try:
                            final = fs.reserve_name(tdir, name, tmp)
                        except OSError as e:
                            if fs.is_enospc(e):
                                raise StorageError("the disk is full: the file could not be stored")
                            raise
                    stored = f"{t['id']}/{(reuse or final).name}"
                    ts = now()
                    cur = c.execute(
                        "INSERT INTO files(task_id,step_index,direction,label,name,stored_path,size,sha256,mime,"
                        "uploaded_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (t["id"], step_index, direction, label, name, stored, total, sha, mime, _actor(by), ts))
                    fid = cur.lastrowid
                    for old in removed:
                        c.execute("DELETE FROM files WHERE id=?", (old["id"],))
                        _event(c, t["id"], by, "detached", {"file_id": old["id"], "name": old["name"],
                                                          "reason": "replaced"}, ts)
                    row = c.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
                    d = file_to_dict(row, _root(c), t["key"])
                    _event(c, t["id"], by, "upload" if direction == "founder" else "attached",
                           {"file_id": fid, "name": name, "size": total, "mime": mime, "step_index": step_index,
                            "direction": direction, "label": label, "path": d["path"]}, ts)
                    c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (ts, t["id"]))
                    if reuse is not None:
                        os.replace(tmp, reuse)  # last step before COMMIT
            finally:
                c.close()
            for old in removed:
                if old["stored_path"] != d["path"][len(str(root)) + 1:]:
                    fs.delete_stored(root, old["stored_path"])
            return {"file": d, "action": "replaced" if removed else "created"}
        except BaseException:
            for leftover in (tmp, final):
                if leftover is not None:
                    try:
                        os.unlink(leftover)
                    except OSError:
                        pass
            raise

    def attach_path(self, ref, path, step_index=None, label="", by="cli") -> dict:
        """Copy a local file into the store as an agent -> human attachment."""
        p = _abs_file(path, "path")
        try:
            f = open(p, "rb")
        except OSError as e:
            raise ValidationError(f"cannot read {path!r}: {e.strerror or e}")
        with f:
            size = os.fstat(f.fileno()).st_size
            try:
                free = fs.free_space(fs.files_root(self.path))
            except OSError:
                free = None
            if free is not None and free < size + fs.SPACE_RESERVE:
                raise StorageError(f"not enough disk space: {os.path.basename(p)} is {fs.format_size(size)} but "
                                   f"only {fs.format_size(max(0, free - fs.SPACE_RESERVE))} is free")
            return self.ingest(ref, f, name=os.path.basename(p), step_index=step_index, label=label, by=by,
                               direction="agent", dedupe=True)

    def remove_file(self, file_id, ref=None, actor="cli", only_direction=None) -> dict:
        try:
            file_id = int(file_id)
        except (TypeError, ValueError):
            raise ValidationError("file id must be a number (see `ftask files`)")
        c = self._conn()
        try:
            with _Tx(c):
                r = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
                if r and ref is not None and r["task_id"] != resolve(c, ref)["id"]:
                    r = None
                if r is None:
                    raise NotFound(f"no file with id {file_id}" + (f" on task {ref!r}" if ref else ""))
                if only_direction and r["direction"] != only_direction:
                    raise ValidationError("this file cannot be removed here")
                t = c.execute("SELECT key FROM tasks WHERE id=?", (r["task_id"],)).fetchone()
                d = file_to_dict(r, _root(c), t["key"] if t else None)
                c.execute("DELETE FROM files WHERE id=?", (file_id,))
                ts = now()
                _event(c, r["task_id"], actor, "detached",
                       {"file_id": file_id, "name": r["name"], "direction": r["direction"], "size": r["size"]}, ts)
                c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (ts, r["task_id"]))
            fs.delete_stored(_root(c), r["stored_path"])
            return d
        finally:
            c.close()

    # ---- waiting / listening -------------------------------------------

    def register_waiter(self, task_id, key_prefix, for_kind) -> int:
        c = self._conn()
        try:
            with _Tx(c):
                self._prune_waiters(c)
                cur = c.execute("INSERT INTO waiters(pid,pidstart,task_id,key_prefix,for_kind,created_at) "
                                "VALUES (?,?,?,?,?,?)",
                                (os.getpid(), _pid_start(os.getpid()), task_id, key_prefix, for_kind, now()))
                return cur.lastrowid
        finally:
            c.close()

    def unregister_waiter(self, wid) -> None:
        c = self._conn()
        try:
            c.execute("DELETE FROM waiters WHERE id=?", (wid,))
        except sqlite3.Error:
            pass
        finally:
            c.close()

    @staticmethod
    def _prune_waiters(c):
        for w in c.execute("SELECT id,pid,pidstart FROM waiters").fetchall():
            if not _pid_alive(w["pid"], w["pidstart"]):
                c.execute("DELETE FROM waiters WHERE id=?", (w["id"],))

    def listeners(self, ref, kind) -> dict:
        """Who would hear a human event of this kind (done|note|upload) on this task right now?"""
        from . import hooks
        c = self._conn()
        try:
            t = resolve(c, ref)
            waiters = 0
            for w in c.execute("SELECT * FROM waiters").fetchall():
                if not _pid_alive(w["pid"], w["pidstart"]):
                    continue
                accepted = ("done", "any") if kind in ("done", "cancelled") else (kind, "any")
                if w["for_kind"] not in accepted:
                    continue
                if (w["task_id"] and w["task_id"] == t["id"]) or (
                        w["key_prefix"] and t["key"] and t["key"].startswith(w["key_prefix"])):
                    waiters += 1
            hook_names = [h.name for h in hooks.hook_files(self.path, kind)]
            notify = bool(t["notify_cmd"])
        finally:
            c.close()
        return {"waiters": waiters, "hooks": hook_names, "notify_cmd": notify,
                "any": bool(waiters or hook_names or notify)}

    def events_after(self, cursor, task_id=None, key_prefix=None, limit=1000) -> list:
        c = self._conn()
        try:
            return self._events_after(c, cursor, task_id, key_prefix, limit)
        finally:
            c.close()

    @staticmethod
    def _events_after(c, cursor, task_id=None, key_prefix=None, limit=1000):
        q = ("SELECT e.*, t.key AS task_key, t.title AS task_title, t.project AS task_project "
             "FROM events e LEFT JOIN tasks t ON t.id = e.task_id WHERE e.seq > ?")
        args = [int(cursor)]
        if task_id:
            q += " AND e.task_id = ?"
            args.append(task_id)
        if key_prefix:
            q += " AND t.key LIKE ? ESCAPE '\\'"
            args.append(key_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        q += " ORDER BY e.seq LIMIT ?"
        args.append(limit)
        return [row_to_event(r) for r in c.execute(q, args)]

    def backup(self, dest) -> str:
        dest = Path(dest)
        if dest.exists():
            raise ValidationError(f"{dest} already exists")
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = self._conn()
        try:
            out = sqlite3.connect(str(dest))
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        return str(dest)
