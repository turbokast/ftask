import json
import unittest

from helpers import TempDbCase


class CliTests(TempDbCase):
    def test_add_with_steps_in_order(self):
        p = self.ftask("add", "--project", "Website", "--title", "Resubmit", "--minutes", "10", "--priority", "5",
                       "--key", "acme/website/resubmit", "--by", "website/claude", "--why", "Rejected.",
                       "--step", "Open console", "--link", "https://example.com/console",
                       "--step", "Paste text", "--value", "line one\nline two",
                       "--step", "Bare step", check=0)
        self.assertTrue(p.stdout.startswith("created "))
        t = json.loads(self.ftask("show", "acme/website/resubmit", "--json", check=0).stdout)
        self.assertEqual(t["steps"], [
            {"text": "Open console", "link": "https://example.com/console"},
            {"text": "Paste text", "value": "line one\nline two"},
            {"text": "Bare step"},
        ])
        self.assertEqual((t["created_by"], t["priority"], t["minutes"]), ("website/claude", 5, 10))
        self.assertEqual(t["events"][0]["actor"], "website/claude")

    def test_add_json_file_stdin_and_array(self):
        doc = {"project": "Docs", "title": "J", "key": "docs/j", "steps": [{"text": "s", "value": "  keep  spaces  "}]}
        self.ftask("add", "--json", "-", input=json.dumps(doc), check=0)
        t = json.loads(self.ftask("show", "docs/j", "--json", check=0).stdout)
        self.assertEqual(t["steps"][0]["value"], "  keep  spaces  ")
        arr = [{"project": "P", "title": "a"}, {"project": "P", "title": "b", "key": "kb"}]
        out = self.ftask("add", "--json", "-", input=json.dumps(arr), check=0).stdout
        self.assertEqual(len(out.strip().splitlines()), 2)
        self.assertEqual(len(json.loads(self.ftask("list", "--json", check=0).stdout)), 3)

    def test_idempotent_key_and_reopen(self):
        args = ["add", "--project", "P", "--title", "T", "--key", "k"]
        self.assertIn("created", self.ftask(*args, check=0).stdout)
        self.assertIn("unchanged", self.ftask(*args, check=0).stdout)
        self.ftask("done", "k", check=0)
        out = self.ftask(*args, "--why", "new", check=0).stdout
        self.assertIn("use --reopen", out)
        self.assertEqual(json.loads(self.ftask("show", "k", "--json").stdout)["status"], "done")
        self.assertIn("reopened", self.ftask(*args, "--reopen", check=0).stdout)
        self.assertEqual(json.loads(self.ftask("show", "k", "--json").stdout)["status"], "open")
        self.assertEqual(len(json.loads(self.ftask("list", "--status", "all", "--json").stdout)), 1)

    def test_lifecycle_commands(self):
        self.ftask("add", "--project", "P", "--title", "T", "--key", "k", check=0)
        self.ftask("wait", "k", "--on", "Apple review", check=0)
        t = json.loads(self.ftask("show", "k", "--json").stdout)
        self.assertEqual((t["status"], t["waiting_on"]), ("waiting", "Apple review"))
        self.assertEqual(len(json.loads(self.ftask("list", "--status", "waiting", "--json").stdout)), 1)
        self.ftask("open", "k", check=0)
        self.ftask("note", "k", "needs", "a", "new", "link", "--by", "founder", check=0)
        self.assertEqual(json.loads(self.ftask("show", "k", "--json").stdout)["note"], "needs a new link")
        n = json.loads(self.ftask("notes", "--json", check=0).stdout)
        self.assertEqual(n[0]["note"], "needs a new link")
        self.ftask("done", "k", "--by", "founder", check=0)
        self.assertIn("already done", self.ftask("done", "k", check=0).stdout)
        self.assertEqual(self.ftask("list", check=0).stdout.strip(), "(no tasks)")
        ev = json.loads(self.ftask("events", "--since", "1h", "--json", check=0).stdout)
        self.assertEqual([e["kind"] for e in ev], ["created", "waiting", "reopened", "note", "done"])
        self.ftask("cancel", "k", check=0)
        self.assertEqual(json.loads(self.ftask("show", "k", "--json").stdout)["status"], "cancelled")

    def test_exit_codes_and_errors(self):
        self.assertEqual(self.ftask("show", "nope").returncode, 3)
        p = self.ftask("add", "--project", "P")
        self.assertEqual(p.returncode, 2)
        self.assertIn("--title", p.stderr)
        self.assertEqual(self.ftask("add", "--project", "P", "--title", "T", "--value", "x").returncode, 2)
        self.assertEqual(self.ftask("add", "--project", "P", "--title", "T", "--minutes", "soon").returncode, 2)
        self.assertEqual(self.ftask("add", "--project", "P", "--title", "T", "--step", "s", "--link", "javascript:x").returncode, 2)
        self.assertEqual(self.ftask("add", "--json", "/nonexistent.json").returncode, 2)
        self.assertEqual(self.ftask("add", "--json", "-", input="{not json").returncode, 2)
        self.assertEqual(self.ftask("list", "--status", "bogus").returncode, 2)
        self.assertEqual(self.ftask("notes", "--since", "whenever").returncode, 2)
        self.assertEqual(self.ftask().returncode, 2)

    def test_help_has_examples(self):
        for sub in ("", "add", "list", "notes"):
            p = self.ftask(*([sub] if sub else []), "--help", check=0)
            self.assertIn("example", p.stdout.lower())

    def test_env_by_and_plain_output(self):
        self.ftask("add", "--project", "P", "--title", "Long title that must never be truncated in the list output at all",
                   env={"FTASK_BY": "cron/nightly"}, check=0)
        out = self.ftask("list", check=0).stdout
        self.assertIn("Long title that must never be truncated in the list output at all", out)
        self.assertEqual(json.loads(self.ftask("list", "--json").stdout)[0]["created_by"], "cron/nightly")

    def test_backup_and_path(self):
        self.ftask("add", "--project", "P", "--title", "T", check=0)
        self.assertEqual(self.ftask("path", check=0).stdout.strip(), self.db)
        dest = self.db + ".copy"
        self.ftask("backup", dest, check=0)
        self.assertEqual(self.ftask("backup", dest).returncode, 2)


if __name__ == "__main__":
    unittest.main()
