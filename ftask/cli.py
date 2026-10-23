"""ftask command line interface."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from . import __version__
from . import files as fs
from . import store as st
from .hooks import HUMAN

PROG = "ftask"

EPILOG_MAIN = """\
exit codes: 0 ok | 1 error | 2 usage/validation | 3 not found | 4 ambiguous id | 5 database busy | 6 disk full / file store error | 124 wait timed out

environment:
  FTASK_DB   path to the SQLite file (default the per-user data directory (see 'ftask path'))
  FTASK_BY   default actor label recorded in history (override with --by)

examples:
  ftask add --project Website --title "Renew the domain" --minutes 10 \\
      --key website/renew-domain --by website/claude \\
      --why "The registrar will not take a card from an agent." \\
      --step "Open the registrar" --link https://registrar.example.com/domains \\
      --step "Paste this coupon code" --value "RENEW-2026"
  ftask add --json task.json            # full task as JSON (best for long values); '-' reads stdin
  ftask list                            # open + waiting tasks
  ftask list --project website --status all --json
  ftask show website/renew-domain
  ftask notes --since 2h                # what the human wrote back / finished recently
  ftask attach KEY ./cover.pdf          # give the human a file (Download + full path in the UI)
  ftask files KEY --json                # files in both directions, absolute paths (read human uploads here)
  ftask wait KEY --for done             # block until the human acts; run it in the background
  ftask done k3m9xq
"""


class UsageError(st.ValidationError):
    pass


class _StepAction(argparse.Action):
    """Records --step/--value/--link in command-line order."""

    def __call__(self, parser, ns, values, option_string=None):
        ops = getattr(ns, "step_ops", None)
        if ops is None:
            ops = []
            ns.step_ops = ops
        ops.append((self.dest, True if self.dest == "upload" else values))


def _build_steps(ops):
    """-> (steps, task_level) ; --upload/--accept/--label before the first --step apply to the task itself."""
    steps = []
    task_level = {}
    for kind, val in ops or []:
        if kind == "step":
            steps.append({"text": val})
            continue
        if not steps:
            if kind in ("upload", "accept", "label"):
                key = {"label": "upload_label"}.get(kind, kind)
                if key in task_level:
                    raise st.ValidationError(f"--{kind} given twice for the task")
                task_level[key] = val
                continue
            raise st.ValidationError(f"--{kind} must come after a --step")
        if kind in steps[-1]:
            raise st.ValidationError(f"step {len(steps)} already has a --{kind}; start a new --step")
        steps[-1][kind] = val
    return steps, task_level


def _actor(args):
    by = getattr(args, "by", None) or os.environ.get("FTASK_BY") or "cli"
    return HUMAN if by == "human" else by  # `--by human` is the documented spelling of the stored 'founder' actor


def _out(obj):
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _load_json(spec):
    try:
        text = sys.stdin.read() if spec == "-" else open(spec, encoding="utf-8").read()
    except OSError as e:
        raise st.ValidationError(f"cannot read {spec}: {e}")
    try:
        return json.loads(text)
    except ValueError as e:
        raise st.ValidationError(f"invalid JSON in {spec if spec != '-' else 'stdin'}: {e}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_add(store, args):
    flag_data = {}
    for attr, name in (("project", "project"), ("title", "title"), ("why", "why"), ("minutes", "minutes"),
                       ("priority", "priority"), ("key", "key"), ("waiting_on", "waiting_on")):
        v = getattr(args, attr)
        if v is not None:
            flag_data[name] = v
    if getattr(args, "step_ops", None):
        steps, task_level = _build_steps(args.step_ops)
        if steps:
            flag_data["steps"] = steps
        flag_data.update(task_level)
    if args.notify_cmd is not None:
        flag_data["notify_cmd"] = args.notify_cmd
    actor = _actor(args)
    if args.json:
        payload = _load_json(args.json)
        items = payload if isinstance(payload, list) else [payload]
        if isinstance(payload, list) and flag_data:
            raise st.ValidationError("task flags cannot be combined with a JSON array; put fields in the JSON")
        if not items:
            raise st.ValidationError("JSON array is empty")
        results = []
        for item in items:
            if not isinstance(item, dict):
                raise st.ValidationError("each task in the JSON must be an object")
            merged = dict(item)
            merged.update(flag_data)
            if "created_by" not in merged and "by" not in merged:
                merged["created_by"] = actor
            results.append(store.upsert(merged, actor=actor, reopen=args.reopen))
    else:
        missing = [n for n in ("project", "title") if n not in flag_data]
        if missing:
            raise st.ValidationError("missing required: " + ", ".join("--" + n for n in missing)
                                     + " (or use --json)")
        flag_data["created_by"] = actor
        results = [store.upsert(flag_data, actor=actor, reopen=args.reopen)]
    for r in results:
        t = r["task"]
        label = f" [{t['key']}]" if t["key"] else ""
        extra = ""
        if r["closed"]:
            extra = f" (task is {t['status']}; left as is, use --reopen to reopen)"
        elif r["reopened"]:
            extra = " (reopened)"
        print(f"{r['action']} {t['id']}{label}{extra}")
        for a in r.get("attached", []):
            f = a["file"]
            print(f"  attached #{f['id']} {f['name']} ({fs.format_size(f['size'])}) -> {f['path']}"
                  + ("  (unchanged)" if a["action"] == "unchanged" else ""))
    return 0


def _statuses(spec):
    if spec in (None, "", "active"):
        return ("open", "waiting")
    if spec == "all":
        return None
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    for p in parts:
        if p not in st.STATUSES:
            raise st.ValidationError(f"unknown status {p!r}; use open, waiting, done, cancelled, active or all")
    return tuple(parts)


def cmd_list(store, args):
    tasks = store.list(_statuses(args.status), args.project)
    if args.json:
        _out(tasks)
        return 0
    if not tasks:
        print("(no tasks)")
        return 0
    rows = [["ID", "STATUS", "PRI", "MIN", "PROJECT", "TITLE"]]
    for t in tasks:
        title = t["title"]
        if t["status"] == "waiting":
            title += f"  (waiting on: {t['waiting_on']})"
        if t["steps"]:
            title += f"  [{len(t['steps_done'])}/{len(t['steps'])} steps]"
        rows.append([t["id"], t["status"], str(t["priority"]), "" if t["minutes"] is None else str(t["minutes"]),
                     t["project"], title])
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(r[i].ljust(widths[i]) for i in range(5)) + "  " + r[5])
    return 0


def _fmt_task(t, events):
    L = []
    L.append(f"{t['id']}  [{t['status']}]  {t['project']}: {t['title']}")
    if t["key"]:
        L.append(f"key:        {t['key']}")
    meta = [f"priority {t['priority']}"]
    if t["minutes"] is not None:
        meta.append(f"~{t['minutes']} min")
    meta.append(f"created {t['created_at']} by {t['created_by'] or '?'}")
    if t["done_at"]:
        meta.append(f"closed {t['done_at']}")
    L.append("            " + " | ".join(meta))
    if t["why"]:
        L.append(f"why:        {t['why']}")
    if t["waiting_on"]:
        L.append(f"waiting on: {t['waiting_on']}")
    if t.get("upload"):
        want = t.get("upload_label") or "a file"
        L.append(f"file wanted: {want}" + (f"  (accept: {t['accept']})" if t.get("accept") else ""))
    if t.get("notify_cmd"):
        L.append(f"notify_cmd: {t['notify_cmd']}")
    if t["steps"]:
        L.append(f"steps ({len(t['steps_done'])}/{len(t['steps'])} done):")
        for i, s in enumerate(t["steps"]):
            mark = "x" if i in t["steps_done"] else " "
            L.append(f"  {i + 1}. [{mark}] {s['text']}")
            if s.get("upload"):
                L.append("       file wanted: " + (s.get("label") or "a file")
                         + (f"  (accept: {s['accept']})" if s.get("accept") else ""))
            if s.get("link"):
                L.append(f"       link:  {s['link']}")
            if s.get("value"):
                vlines = s["value"].split("\n")
                L.append(f"       value: {vlines[0]}")
                for extra in vlines[1:]:
                    L.append(f"              {extra}")
    if t["note"]:
        L.append(f"note ({t['note_at']}):")
        for ln in t["note"].split("\n"):
            L.append(f"  {ln}")
    if t.get("files"):
        L.append("files:")
        for f in t["files"]:
            L.extend(_fmt_file(f))
    if events:
        L.append("history:")
        for e in events:
            L.append(f"  {e['at']}  {e['actor']:<18} {e['kind']:<9} {_detail(e)}")
    return "\n".join(L)


def _fmt_file(f):
    where = "" if f["step"] is None else f" step {f['step']}"
    arrow = "human -> agent" if f["direction"] == HUMAN else "agent -> human"
    lab = f"  \"{f['label']}\"" if f["label"] else ""
    return [f"  #{f['id']} [{arrow}{where}] {f['name']}  {fs.format_size(f['size'])}  {f['created_at']}  "
            f"by {f['uploaded_by']}{lab}", f"       {f['path']}"]


def _detail(e):
    d = e["detail"]
    k = e["kind"]
    if k == "step":
        return f"step {d.get('step')} {'ticked' if d.get('done') else 'unticked'}: {d.get('text', '')}"
    if k == "note":
        return json.dumps(d.get("note", ""), ensure_ascii=False)
    if k == "updated":
        return "fields: " + ", ".join(d.get("fields", []))
    if k == "created":
        return d.get("title", "")
    if k == "waiting":
        return d.get("waiting_on", "")
    if k in ("done", "cancelled", "reopened"):
        return f"(was {d.get('from')})"
    if k in ("upload", "attached"):
        return f"#{d.get('file_id')} {d.get('name')} ({fs.format_size(d.get('size', 0))})"
    if k == "detached":
        return f"#{d.get('file_id')} {d.get('name')}"
    return json.dumps(d, ensure_ascii=False)


def cmd_show(store, args):
    t = store.get(args.ref)
    events = store.task_events(t["id"])
    if args.json:
        t["events"] = events
        _out(t)
    else:
        print(_fmt_task(t, events))
    return 0


def _simple_status(status, past):
    def run(store, args):
        wo = getattr(args, "on", None)
        r = store.set_status(args.ref, status, actor=_actor(args), waiting_on=wo)
        t = r["task"]
        print(f"{t['id']} {'is now ' + past if r['changed'] else 'already ' + past}")
        return 0
    return run


def cmd_note(store, args):
    text = " ".join(args.text) if args.text != ["-"] else sys.stdin.read()
    r = store.set_note(args.ref, text, actor=_actor(args))
    print(f"{r['task']['id']} note {'saved' if r['changed'] else 'unchanged'}")
    return 0


def cmd_notes(store, args):
    since = st.parse_since(args.since) if args.since else None
    rows = store.notes(since)
    if args.json:
        _out(rows)
        return 0
    if not rows:
        print("(nothing new)")
        return 0
    for r in rows:
        head = f"{r['id']}  [{r['status']}]  {r['project']}: {r['title']}"
        if r["key"]:
            head += f"  <{r['key']}>"
        print(head)
        if r["closed_at"]:
            print(f"  {r['status']} at {r['closed_at']}  ({r['steps_done']}/{r['steps_total']} steps ticked)")
        if r["note"]:
            print(f"  note ({r['note_at']}):")
            for ln in r["note"].split("\n"):
                print(f"    {ln}")
    return 0


def cmd_events(store, args):
    since = st.parse_since(args.since) if args.since else None
    evs = store.events(since=since, after_seq=args.after, limit=args.limit)
    if args.json:
        _out(evs)
        return 0
    if not evs:
        print("(no events)")
        return 0
    for e in evs:
        print(f"{e['seq']:>5}  {e['at']}  {e['actor']:<18} {e['kind']:<9} {e['task_id']} "
              f"{e.get('task_title') or ''} -- {_detail(e)}")
    return 0


def _task_line(t):
    return f"{t['id']}  [{t['status']}]  {t['project']}: {t['title']}" + (f"  <{t['key']}>" if t["key"] else "")


def cmd_wait(store, args):
    if args.on is not None:
        if args.for_kind or args.timeout is not None or args.since is not None or args.key_prefix:
            raise st.ValidationError("--on marks a task waiting; it cannot be combined with --for/--timeout/--since/--key-prefix")
        if not args.ref:
            raise st.ValidationError("task id or key required")
        return _simple_status("waiting", "waiting")(store, args)
    from . import waiting
    if args.ref and args.key_prefix:
        raise st.ValidationError("give either ID|KEY or --key-prefix, not both")
    if not args.ref and not args.key_prefix:
        raise st.ValidationError("give a task ID|KEY or --key-prefix (or --on TEXT to mark the task waiting)")
    if args.timeout is not None and args.timeout < 0:
        raise st.ValidationError("--timeout must be >= 0")
    res = waiting.wait(store, ref=args.ref, key_prefix=args.key_prefix, kind=args.for_kind or "any",
                       since=args.since, timeout=args.timeout)
    code = waiting.EXIT_TIMEOUT if res["timeout"] else 0
    if args.json:
        _out({"timeout": res["timeout"], "reason": res["reason"], "cursor": res["cursor"],
              "events": res["events"], "task": res["task"]})
        return code
    t = res["task"]
    if res["timeout"]:
        what = args.ref or f"key prefix {args.key_prefix}"
        print(f"timeout: nothing happened on {what} (--for {args.for_kind or 'any'})")
    elif res["reason"].startswith("already-"):
        print(f"already {t['status']}: " + _task_line(t))
        if t["done_at"]:
            print(f"  {t['status']} at {t['done_at']}")
    else:
        for e in res["events"]:
            tt = e.get("task_title") or ""
            print(f"event {e['seq']}  {e['at']}  {e['actor']}  {e['kind']}  {e['task_id']} "
                  f"{('<' + e['task_key'] + '> ') if e.get('task_key') else ''}{tt} -- {_detail(e)}")
            if e["kind"] == "upload" and e["detail"].get("path"):
                print(f"  file: {e['detail']['path']}")
            if e["kind"] == "note":
                for ln in (e["detail"].get("note") or "(note cleared)").split("\n"):
                    print(f"  | {ln}")
        print("task: " + _task_line(t))
    if t and not res["timeout"] and not res["reason"].startswith("already-") and t.get("note") and not any(
            e["kind"] == "note" for e in res["events"]):
        print(f"  note: {t['note']}")
    print(f"cursor: {res['cursor']}")
    return code


def cmd_attach(store, args):
    idx = None
    if args.step is not None:
        if args.step < 1:
            raise st.ValidationError("--step is 1-based (as in `ftask show`)")
        idx = args.step - 1
    r = store.attach_path(args.ref, args.path, step_index=idx, label=args.label, by=_actor(args))
    f = r["file"]
    verb = {"created": "attached", "replaced": "attached (replaced the earlier version)",
            "unchanged": "already attached, unchanged"}[r["action"]]
    print(f"{verb} #{f['id']} {f['name']} ({fs.format_size(f['size'])}, {f['mime']})")
    print(f"path: {f['path']}")
    return 0


def cmd_files(store, args):
    if args.ref and args.all:
        raise st.ValidationError("give a task or --all, not both")
    if not args.ref and not args.all:
        raise st.ValidationError("give a task ID|KEY, or --all for every task")
    files = store.list_files(args.ref, HUMAN if args.from_founder else None)
    if args.json:
        _out(files)
        return 0
    if not files:
        print("(no files)")
        return 0
    for f in files:
        for ln in _fmt_file(f):
            print(("[" + f["task_id"] + "] " if args.all and ln.startswith("  #") else "") + ln)
    return 0


def cmd_detach(store, args):
    f = store.remove_file(args.file_id, ref=args.ref, actor=_actor(args))
    print(f"removed #{f['id']} {f['name']}")
    return 0


def cmd_hook_run(store, args):
    from . import hooks
    seqs = [int(x) for x in args.seq.split(",") if x.strip().isdigit()]
    return hooks.run_events(store.path, seqs)


def cmd_backup(store, args):
    dest = args.dest or f"{store.path}.{st.now().replace(':', '').replace('.', '')}.bak"
    print(store.backup(dest))
    return 0


def cmd_path(store, args):
    print(store.path)
    return 0


def cmd_serve(store, args):
    from . import server

    def ready(url):
        if args.open:
            import webbrowser
            webbrowser.open(url)
    return server.serve(store.path, args.host, args.port, allow_hosts=args.allow_host,
                        max_upload=int(args.max_upload_gb * 1024 ** 3) if args.max_upload_gb else None,
                        on_ready=ready)


def cmd_snippet(store, args):
    from importlib import resources
    sys.stdout.write(resources.files(__package__).joinpath("AGENTS-SNIPPET.md").read_text(encoding="utf-8"))
    return 0


def cmd_service(store, args):
    from . import service
    manager = args.manager or service.default_manager()
    if args.action == "install":
        return service.install(manager, args.host, args.port, args.print, start=not args.no_start)
    if args.action == "uninstall":
        return service.uninstall(manager)
    return service.status(manager)


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def _by_parent():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--by", metavar="LABEL", help="who is making this change, recorded in history "
                   "(default: $FTASK_BY or 'cli'), e.g. 'shop/claude'")
    return p


def build_parser():
    ap = argparse.ArgumentParser(
        prog=PROG,
        description="Task inbox for AI agents: agents hand the human exact steps; the human works through them in a browser.",
        epilog=EPILOG_MAIN,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--db", metavar="PATH", help="SQLite file (default: $FTASK_DB or the per-user data directory (see 'ftask path'))")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True
    by = _by_parent()
    raw = argparse.RawDescriptionHelpFormatter

    p = sub.add_parser("add", parents=[by], formatter_class=raw, help="add a task (or update the one with the same --key)",
        description="Add a task. With --key, an existing task with that key is updated in place instead of duplicated "
                    "(human's ticked steps and note are kept unless the number of steps changes). "
                    "A done/cancelled task is only reopened with --reopen.",
        epilog="""\
examples:
  ftask add --project Shop --title "Approve the Stripe webhook" --minutes 3 --priority 10 \\
      --key shop/stripe/webhook --by shop/claude --why "Needed before launch." \\
      --step "Open the webhook page" --link https://dashboard.stripe.com/webhooks \\
      --step "Paste the endpoint URL" --value "https://api.example.com/hooks/stripe" \\
      --step "Tick 'checkout.session.completed'"
  ftask add --project Shop --title "Wait for review" --waiting-on "App store review (24-72h)"
  ftask add --json task.json
  echo '{"project":"Shop","title":"x","steps":[{"text":"do it","value":"a\\nb"}]}' | ftask add --json -

--step starts a new step; the --value / --link / --upload / --accept / --label that follow attach to that step.
JSON fields: project, title, why, minutes, priority, key, waiting_on, created_by, notify_cmd,
upload/accept/upload_label (task-level file request),
steps:[{text, value?, link?, upload?, accept?, label?, attach?}]. A JSON array adds several tasks.
"attach": "/path/to/file" (or a list) copies the file in for the human to download.

file request:
  ftask add --project Shop --title "Send the screen recording" --key shop/rec \\
      --step "Record the checkout flow" --step "Attach it here" --upload --accept "video/*" --label "Screen recording"
""")
    p.add_argument("--project", help="project label, e.g. Website, Shop, 'Mobile app'")
    p.add_argument("--title", help="short imperative title")
    p.add_argument("--why", help="one or two sentences: why the human must do this")
    p.add_argument("--minutes", type=str, help="estimate in minutes")
    p.add_argument("--priority", type=str, help="integer, lower = sooner (default 100)")
    p.add_argument("--key", help="idempotency key, e.g. website/renew-domain")
    p.add_argument("--waiting-on", dest="waiting_on", help="makes the task 'waiting' (not actionable yet)")
    p.add_argument("--step", action=_StepAction, dest="step", metavar="TEXT", help="add a step (repeatable)")
    p.add_argument("--value", action=_StepAction, dest="value", metavar="TEXT",
                   help="exact text to paste for the preceding --step")
    p.add_argument("--link", action=_StepAction, dest="link", metavar="URL", help="URL for the preceding --step")
    p.add_argument("--upload", action=_StepAction, dest="upload", nargs=0,
                   help="the preceding --step asks the human to attach a file (before any --step: the task does)")
    p.add_argument("--accept", action=_StepAction, dest="accept", metavar="TYPES",
                   help="file picker filter for an upload step, e.g. 'video/*,.csv' (advisory)")
    p.add_argument("--label", action=_StepAction, dest="label", metavar="TEXT",
                   help="short name of the file wanted, e.g. 'Screen recording' (upload steps)")
    p.add_argument("--notify-cmd", dest="notify_cmd", metavar="CMD",
                   help="shell command run (as the current user) whenever the human acts on this task: marks it "
                        "done/cancelled/reopened, ticks a step, writes a note or uploads a file. Event JSON on stdin; "
                        "env FTASK_ID/KEY/EVENT/PROJECT. Runs arbitrary commands: only set it from an agent on this machine. "
                        "'' clears it.")
    p.add_argument("--json", metavar="FILE|-", help="read the full task (or array of tasks) as JSON")
    p.add_argument("--reopen", action="store_true", help="reopen the task if it is done/cancelled")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", formatter_class=raw, help="list tasks",
        epilog="examples:\n  ftask list\n  ftask list --project Shop --status done\n  ftask list --status all --json\n")
    p.add_argument("--project", help="only this project (case-insensitive)")
    p.add_argument("--status", metavar="S", help="open|waiting|done|cancelled, comma list, 'active' (default: open+waiting) or 'all'")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", formatter_class=raw, help="show one task with steps and history",
        epilog="examples:\n  ftask show k3m9xq\n  ftask show shop/stripe/webhook --json\n")
    p.add_argument("ref", metavar="ID|KEY")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    for name, status, past, hlp in (
        ("done", "done", "done", "mark a task done"),
        ("open", "open", "open", "(re)open a task"),
        ("cancel", "cancelled", "cancelled", "cancel a task"),
    ):
        p = sub.add_parser(name, parents=[by], formatter_class=raw, help=hlp,
                           epilog=f"example:\n  ftask {name} k3m9xq\n")
        p.add_argument("ref", metavar="ID|KEY")
        p.set_defaults(func=_simple_status(status, past))

    p = sub.add_parser("wait", parents=[by], formatter_class=raw,
        help="two uses: --on TEXT marks a task waiting; otherwise blocks until the human acts",
        description="Without --on: block until the human acts on the task (or any task whose key starts with "
                    "--key-prefix), then print what happened and exit 0. Exit 124 on --timeout. Meant to be run "
                    "with a coding agent's background-command facility so the agent is notified when it exits.\n\n"
                    "--for done   the task is done or cancelled (answers at once if it already is)\n"
                    "--for note   the human wrote a note\n"
                    "--for upload the human uploaded a file\n"
                    "--for any    any of the above, or the human ticked a step / reopened it (default)\n\n"
                    "Every result prints 'cursor: N'. Pass it back as --since N so nothing is missed between calls "
                    "(with --since, only events after N count). With --on TEXT this command instead marks the task "
                    "as waiting on a third party (the older meaning).",
        epilog="examples:\n  ftask wait shop/x/y --for done          # run in the background; exits when done\n"
               "  ftask wait k3m9xq --for any --timeout 3600 --json\n"
               "  ftask wait --key-prefix shop/ --for any --since 120\n"
               '  ftask wait k3m9xq --on "Apple review (2-3 days)"   # mark the task as waiting on a third party\n'
               "\nexit codes: 0 event happened | 124 timeout | 3 task not found | 130 interrupted\n"
               "Human events are those recorded by the web UI (actor 'human'; use --by human on the CLI).\n")
    p.add_argument("ref", nargs="?", metavar="ID|KEY")
    p.add_argument("--on", metavar="TEXT", help="mark the task waiting on this (instead of blocking)")
    p.add_argument("--for", dest="for_kind", choices=("done", "note", "upload", "any"), default=None)
    p.add_argument("--timeout", type=float, metavar="SECONDS", help="give up after this long (exit 124)")
    p.add_argument("--since", type=int, metavar="CURSOR", help="only events after this cursor (as printed by a previous wait)")
    p.add_argument("--key-prefix", dest="key_prefix", metavar="PREFIX", help="wait on any task whose key starts with this")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("attach", parents=[by], formatter_class=raw, help="attach a file for the human to download",
        description="Copy a local file into the task's file store. The UI shows a Download link and the full local path "
                    "with a Copy button (for web forms that need the file picked from disk). Attaching the same name "
                    "again replaces the earlier version; identical content is a no-op.",
        epilog="examples:\n  ftask attach website/invoice ./build/invoice.pdf --label 'Invoice PDF'\n"
               "  ftask attach k3m9xq ./icon.png --step 2\n")
    p.add_argument("ref", metavar="ID|KEY")
    p.add_argument("path", metavar="PATH")
    p.add_argument("--step", type=int, metavar="N", help="show it under step N (1-based, as in `ftask show`)")
    p.add_argument("--label", default="", metavar="TEXT", help="short description shown next to the file")
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("files", formatter_class=raw, help="list files on a task (both directions) with absolute paths",
        description="Lists every file attached to the task: 'agent -> human' (ftask attach) and "
                    "'human -> agent' (uploaded in the UI). The path is absolute: read human uploads from there.",
        epilog="examples:\n  ftask files shop/rec\n  ftask files k3m9xq --json\n  ftask files --all\n")
    p.add_argument("ref", nargs="?", metavar="ID|KEY")
    p.add_argument("--all", action="store_true", help="files of every task")
    p.add_argument("--from-human", "--from-founder", dest="from_founder", action="store_true",
                   help="only files the human uploaded")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_files)

    p = sub.add_parser("detach", parents=[by], formatter_class=raw, help="remove an attached file from a task",
                       epilog="example:\n  ftask detach k3m9xq 12      # file ids are listed by `ftask files`\n")
    p.add_argument("ref", metavar="ID|KEY")
    p.add_argument("file_id", metavar="FILE_ID")
    p.set_defaults(func=cmd_detach)

    p = sub.add_parser("_hook-run", help=argparse.SUPPRESS)  # internal: runs hooks for events; spawned by the recording process
    p.add_argument("--seq", required=True)
    p.set_defaults(func=cmd_hook_run)

    p = sub.add_parser("note", parents=[by], formatter_class=raw, help="set the note on a task (replaces the previous note)",
                       epilog='examples:\n  ftask note k3m9xq "Used the other account; link is wrong"\n  echo "long text" | ftask note k3m9xq -\n')
    p.add_argument("ref", metavar="ID|KEY")
    p.add_argument("text", nargs="+", metavar="TEXT", help="note text ('-' reads stdin; empty string clears)")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("notes", formatter_class=raw, help="tasks with a human note, or completed recently (for agents to poll)",
        description="Tasks that carry a human note and/or were closed (done/cancelled) recently. "
                    "With --since: notes written after that time and tasks closed after it. "
                    "Without: every task with a note plus tasks closed in the last 7 days.",
        epilog="examples:\n  ftask notes --since 2h\n  ftask notes --since 2026-10-22T09:00:00Z --json\n")
    p.add_argument("--since", metavar="ISO|REL", help="ISO-8601 (UTC if no offset) or relative: 30m, 2h, 7d")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("events", formatter_class=raw, help="append-only history: who changed what, when",
        epilog="examples:\n  ftask events --since 1d\n  ftask events --after 120 --json\n")
    p.add_argument("--since", metavar="ISO|REL")
    p.add_argument("--after", type=int, metavar="SEQ", help="only events with seq greater than this")
    p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("backup", formatter_class=raw, help="write a consistent copy of the database",
                       epilog="example:\n  ftask backup ~/backups/tasks-2026-10-22.db\n")
    p.add_argument("dest", nargs="?", help="destination file (default: next to the db, timestamped)")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("path", help="print the database path")
    p.set_defaults(func=cmd_path)

    p = sub.add_parser("serve", formatter_class=raw, help="run the web UI (default http://127.0.0.1:4747)",
        epilog="examples:\n  ftask serve\n  ftask serve --port 4747\n\n"
               "Binds to 127.0.0.1 only by default. There is no login: do not expose it beyond localhost.\n")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4747)
    p.add_argument("--open", action="store_true", help="open the board in your browser once it is listening")
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="extra Host header name to accept (e.g. behind a reverse proxy); repeatable")
    p.add_argument("--max-upload-gb", type=float, metavar="GB",
                   help="largest file the human can upload in the UI (default 10; env FTASK_MAX_UPLOAD_BYTES)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("snippet", help="print the CLAUDE.md / AGENTS.md instructions that teach an agent to use ftask",
        epilog="example:\n  ftask snippet >> AGENTS.md\n  ftask snippet >> CLAUDE.md\n")
    p.set_defaults(func=cmd_snippet, no_store=True)

    p = sub.add_parser("service", formatter_class=raw, help="optional: keep `ftask serve` running (systemd user unit / launchd agent)",
        description="Install, remove or inspect a background service that runs `ftask serve` at login. "
                    "Linux: a systemd user unit. macOS: a launchd agent. Not needed to use ftask.",
        epilog="examples:\n  ftask service install --print     # show what would be written, change nothing\n"
               "  ftask service install\n  ftask service status\n  ftask service uninstall\n")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument("--manager", choices=["systemd", "launchd"], help="default: systemd on Linux, launchd on macOS")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4747)
    p.add_argument("--print", action="store_true", help="install: print the unit/plist instead of writing it")
    p.add_argument("--no-start", action="store_true", help="install: write the file but do not start the service")
    p.set_defaults(func=cmd_service, no_store=True)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        store = None if getattr(args, "no_store", False) else st.Store(args.db)
        return args.func(store, args) or 0
    except st.FtaskError as e:
        print(f"{PROG}: error: {e}", file=sys.stderr)
        return e.code
    except sqlite3.OperationalError as e:
        print(f"{PROG}: database error: {e}", file=sys.stderr)
        return 5
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
