## Tasks for the human: use `ftask`

When the human must do something you cannot (create an account, pay, verify identity, approve, click in a web console, make a decision), add a task to their `ftask` inbox instead of (or as well as) describing it in chat. They work through the board in a browser (`ftask serve`, default http://localhost:4747).

- Put the exact values in the task. Each step may carry a `--value` (the exact text to paste, shown with a Copy button) and a `--link` (the exact URL). Never ask the human to open a file to find a value.
- Always pass `--key <project>/<slug>` (stable, e.g. `website/renew-domain`) so re-running updates the same task instead of duplicating it. Add `--by <project>/agent` and `--project "<Project name>"`.
- Keep it short: one imperative title, one or two sentences of `--why`, a `--minutes` estimate, a `--priority` (lower = sooner; 10 urgent, 100 default). One action per step.
- If you are blocked on a third party rather than on the human, use `--waiting-on "what"` (the task shows under Waiting).
- Long or multi-line values: `ftask add --json -` with `{"project","title","key","why","minutes","priority","steps":[{"text","value","link"}]}` on stdin.

```
ftask add --project Website --title "Renew the domain" --minutes 10 --priority 20 \
  --key website/renew-domain --by website/agent \
  --why "The registrar needs a card and a 3-D Secure confirmation; renewal lapses on Friday." \
  --step "Open the registrar" --link https://registrar.example.com/domains \
  --step "Enter this coupon at checkout" --value "RENEW-2026" \
  --step "Pay and confirm the 3-D Secure prompt"
```

Files:

- A file the human needs (PDF, installer, export): put it on the task with `ftask attach <key> <path> [--step N] [--label ".."]` (JSON steps can carry `"attach": "/path"`). The UI shows a Download link and the full local path with a Copy button. Never tell the human to find a file in a folder.
- A file you need from the human (screen recording, CSV export, screenshot): add a step with `--upload [--accept "video/*,.csv"] --label "Screen recording"` after its `--step` (JSON: `"upload": true, "accept", "label"`). When it is done, read the upload with `ftask files <key> --json` (absolute `path` of each file; `direction` is `founder` for files the human uploaded).

Check what the human did (do this at session start and before asking again):

- `ftask notes --since 1d` lists tasks the human completed or cancelled and any note they wrote back (`--json` for scripts).
- `ftask show <id|key>` shows steps ticked, the human's note, files and the history; `ftask list --project "<Project>" --status all` shows everything for your project.
- Treat the human's note as an instruction or answer. A task that is `done` is finished: do not re-add it (re-adding the same `--key` leaves it done; only pass `--reopen` if it genuinely needs doing again). Update a task in place by re-running `ftask add` with the same `--key`; ticked steps and notes are kept unless the number of steps changes.
- Do not add tasks you can do yourself, and do not add duplicates under a new key.

Get notified instead of polling: after creating a task you are waiting on, run `ftask wait <key> --for done` (or `--for any` for notes, uploads and ticked steps) as a background command (for example Claude Code's Bash `run_in_background`); you are notified when it exits and its output says what happened. It prints `cursor: N`; pass `--since N` on the next wait so nothing is missed. `--key-prefix <project>/` waits on any of your tasks. Exit code 124 means `--timeout` expired. Do not use `--notify-cmd` unless the human asks (it runs a shell command as the user).
