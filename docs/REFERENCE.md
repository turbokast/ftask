# ftask reference

Full command, data-model, file, notification and security reference. For the 60-second tour see the [README](../README.md).

## CLI

```
ftask add --project P --title T [--why ..] [--minutes N] [--priority N] [--key K] [--by LABEL]
          [--waiting-on ..] [--step "text" [--value "exact text"] [--link URL]]... [--reopen]
          [--upload [--accept "video/*,.csv"] [--label TEXT]]  # after a --step: that step asks the human for a file
          [--notify-cmd "CMD"]
ftask add --json FILE|-            # whole task (or an array of tasks) as JSON; best for long values
ftask list [--project P] [--status open|waiting|done|cancelled|active|all] [--json]
ftask show ID|KEY [--json]         # steps, ticks, note, full history
ftask done|open|cancel ID|KEY      ftask wait ID|KEY --on TEXT      ftask note ID|KEY TEXT
ftask attach ID|KEY PATH [--step N] [--label TEXT]   # copy a file in for the human (Download + full path in the UI)
ftask files ID|KEY [--json]        # every file on the task, both directions, with absolute paths
ftask detach ID|KEY FILE_ID
ftask wait ID|KEY [--for done|note|upload|any] [--timeout S] [--since CURSOR] [--json]   # block until the human acts
ftask wait --key-prefix shop/ --for any [--since CURSOR]
ftask notes [--since ISO|2h|7d] [--json]    # tasks with a human note, or completed recently (agents poll this)
ftask events [--since ..] [--after SEQ] [--json]   # append-only history: who changed what, when
ftask serve [--host 127.0.0.1] [--port 4747] [--open]
ftask service install|uninstall|status [--print] [--no-start] [--manager systemd|launchd]
ftask snippet      # CLAUDE.md / AGENTS.md instructions for agents
ftask backup [DEST]   ftask path
```

`ftask --help` and `ftask <command> --help` have examples. Exit codes: 0 ok, 1 error, 2 usage/validation, 3 not found, 6 disk full, 124 wait timed out,
4 ambiguous id, 5 database busy. Environment: `FTASK_DB` (database path), `FTASK_BY` (default actor label).

Semantics worth knowing:

* **Task fields**: `id` (6 chars, stable), `key` (optional, unique), `project`, `title`, `why`, `minutes`, `priority`
  (lower = sooner, default 100), `status` (open | waiting | done | cancelled), `waiting_on`, `steps`
  (`[{text, value?, link?}]`), `steps_done` (indices), `note` (the human's reply to the agent), `created_by`,
  `created_at`, `updated_at`, `done_at` (when closed: done or cancelled). Timestamps are UTC ISO-8601.
* **Idempotent upsert**: `add --key K` updates the existing task in place. Only the fields you pass change. Ticked steps
  and the note are kept unless the *number* of steps changes (then ticks reset). Re-adding identical data is a no-op
  (no event, no UI refresh). A done/cancelled task stays closed unless `--reopen`.
* `--step` starts a step; the `--value` / `--link` after it attach to that step. Links must be `http(s)://` or `mailto:`.
* Any command accepts an id, a unique id prefix (3+ chars) or the key.
* `--waiting-on` makes the task `waiting`; clearing it (JSON `"waiting_on": ""`) makes it `open` again.
* **History**: every change appends an event (`created`, `updated`, `waiting`, `done`, `cancelled`, `reopened`, `note`,
  `step`) with actor (`human` for the web UI; stored as `founder`), timestamp and detail. Agents read it with `ftask events`, or `ftask notes`.

## Files (both directions)

Files live in `<data dir>/files/<task-id>/` (next to the database, so a temporary `FTASK_DB` never
touches them), are recorded in the `files` table (task, step, name, stored path, size, sha256, mime, uploaded_by,
created_at, label, direction) and logged as events (`upload`, `attached`, `detached`).

* **Agent to human**: `ftask attach KEY ./cover.pdf --label "Cover file" [--step 2]` copies the file in (JSON `add` can
  do it too: a step or the task may carry `"attach": "/path"` or a list). The UI shows a Download link (served as an
  attachment with its real type) and the full local path with a Copy button, for web forms that need the file picked from
  disk. Attaching the same name again replaces the earlier version in place (same path); identical content is a no-op.
* **Human to agent**: a step with `upload: true` (CLI: `--step ".." --upload --accept "video/*,.csv" --label "Screen
  recording"`) or a task with `upload` (the same flags before the first `--step`) asks for a file. Every open task also has
  a general Files section. The UI offers a file picker and drag-and-drop (several files, progress bar, Cancel, Remove). The
  agent reads uploads with `ftask files KEY --json` (absolute `path`). `accept` is only a picker filter, not enforced.
* Uploads are streamed to disk in 1 MiB chunks (never held in memory). Default maximum 10 GiB (`ftask serve
  --max-upload-gb N` or `FTASK_MAX_UPLOAD_BYTES`); a full disk (less than the file plus 128 MiB free) gives a clear error
  and leaves nothing behind. The UI checks size and free space before sending.
* Safety: same Host/Origin/CSRF-token checks as every POST (the body must be `application/octet-stream`); file names are
  reduced to one safe path component; each task has its own 0700 directory that must be a real directory; stored files are
  opened with `O_NOFOLLOW` and symlinks are never served or followed; downloads carry `Content-Disposition: attachment`,
  `nosniff` and a sandbox CSP.

## Being notified when the human acts

"Human events" are `done`, `cancelled`, `reopened`, `step`, `note` and `upload` recorded with actor `human` (the web UI; from a terminal use
`--by human` or `FTASK_BY=human`). For compatibility with early databases the stored value is the string `founder`, and that is what
`--json` output shows in `actor` and in a file's `direction`; `human` is accepted wherever you pass an actor..

1. **`ftask wait`** blocks until something happens, then prints it and exits 0 (124 on `--timeout`). Run it as a background
   command (e.g. Claude Code's Bash `run_in_background`) and the agent is notified when it exits:
   `ftask wait shop/upload --for done`. `--for done` answers at once if the task is already done/cancelled.
   Every result prints `cursor: N`; pass it as `--since N` next time so nothing is missed (`--since 0` replays history).
   `--key-prefix shop/` watches every task whose key starts with it. It polls one indexed row with 0.1 to 1 s backoff
   (negligible CPU) and registers itself so the UI knows someone is listening.
2. **Hooks**: executables `on-done`, `on-note`, `on-upload`, `on-any` in `<config dir>/hooks/`, run by whichever
   process recorded the event (UI server or CLI), detached so nothing waits for them. stdin is the event JSON; env
   `FTASK_ID FTASK_KEY FTASK_EVENT FTASK_PROJECT FTASK_TITLE FTASK_SEQ FTASK_DB`. 30 s limit (`FTASK_HOOK_TIMEOUT`), killed
   after that; output and exit codes go to `<data dir>/hooks.log`. Events recorded from inside a hook
   never trigger hooks (no loops). Hooks only run for the real database (or when `FTASK_HOOKS_DIR` is set), so tests with a
   temporary `FTASK_DB` never fire them. Example (not installed): `docs/hooks/on-any.example` shows a `notify-send` desktop
   notification.
3. **`--notify-cmd "CMD"`** (JSON `notify_cmd`) stores a command on one task; the same runner executes it for that task's
   human events, with the same stdin/env/time limit/log. **It runs arbitrary shell commands as your user**, so only agents
   on this machine should set it, and the UI cannot. Clear it with `--notify-cmd ""`.

After "Mark done" / "Cancel task" the UI says "Done — the agent has been notified" only if a matching waiter, hook or
notify-cmd exists; otherwise just "Done".

## Web UI

`ftask serve` (optionally kept running by `ftask service install`). Views **To do** (by priority), **Waiting**, **Done**; project chips with counts;
each card shows the project, title, estimate, why, a waiting-on banner, numbered steps with persistent tick boxes, each
`value` in a monospace block with a Copy button, clickable links (new tab), a "Note for the agent" box (saves on blur or
Ctrl+Enter, with a visible "Saved"), and Mark done / Reopen. It refreshes itself within 5 seconds of any change by polling
`/api/version`. No external resources; light/dark by `prefers-color-scheme`; keyboard and screen-reader friendly; works at
phone width. The view and project filter live in the URL hash (`#view=waiting&project=Shop`).

Security: there is no login, so it binds `127.0.0.1` only. It also rejects foreign `Host` headers (DNS rebinding),
requires a CSRF token + JSON content type + same-origin `Origin` on every POST, and sends a strict CSP. Do not expose it
beyond localhost (a non-loopback `--host` prints a warning).

JSON API (used by the UI): `GET /api/version`, `GET /api/tasks`, `GET /api/tasks/ID`, `GET /api/events`,
`GET /api/config`, `GET /api/files/N/download`, `POST /api/tasks/ID/{step,note,status}`,
`POST /api/tasks/ID/upload?name=..&step=..` (raw body), `POST /api/files/N/remove`. Tasks are created through the CLI only.

## Backup

Everything is in one SQLite file: `<data dir>/tasks.db` (WAL mode, so `-wal`/`-shm` files sit beside it
while running). Take a consistent copy with `ftask backup ~/backups/tasks.db` (uses SQLite's online backup), or copy the
file while the service is stopped. Restore by putting a copy at that path. Schema changes are applied automatically
(`PRAGMA user_version` migrations in `ftask/store.py`). **Back up the `files/` directory too** (`cp -a`); `ftask backup` covers only the database.

## Tests

```
python3 -m unittest discover -s tests
```

Covers the store, the CLI (subprocess), idempotent upsert, concurrent writers (many processes), and the HTTP API
including CSRF/Origin/Host rejection, uploads (a 160 MB streamed file; set `FTASK_BIG_TEST=1` for 2 GiB), traversal and
symlink attempts, attach/detach, `ftask wait` for every event type, hooks and notify commands. Tests use temporary databases; `FTASK_DB` points anything at another file.

## For agents

`ftask snippet >> AGENTS.md` (or `CLAUDE.md`) appends the instructions that teach an agent to use ftask. The source is `ftask/AGENTS-SNIPPET.md`.
