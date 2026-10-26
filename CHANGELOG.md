# Changelog

## 0.2.0 - 2026-10-26

* **Push human replies to the agent that owns the task.** ftask now records, per task, the agent context of the last
  agent-side event (herdr pane, Claude Code session, Codex thread, folder) in a new `owners` table (database migration v3,
  applied automatically) and passes it to hooks and `--notify-cmd` commands as `FTASK_OWNER` (JSON). Nothing is recorded
  when none of those environment variables is set; human events never change the owner.
* New example hooks in `docs/hooks/`: `on-any.herdr-push.example` (sends the reply into the owner's herdr pane, or queues it
  in an inbox file) and `inbox-hook.example` (a Claude Code / Codex `SessionStart` and `UserPromptSubmit` hook that shows
  queued replies). Configuration snippets in the README and `docs/REFERENCE.md`.
* README: demo GIF.

## 0.1.0 - 2026-10-23

* Initial public release.
