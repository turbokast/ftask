import http.client
import io
import json
import os
import threading
import time
import unittest
from pathlib import Path

from helpers import TempDbCase, eventually
from ftask import hooks, paths
from ftask import server
from ftask import store as st


class HookTests(TempDbCase):
    def setUp(self):
        super().setUp()
        self.s = st.Store(self.db)
        self.t = self.s.upsert({"project": "Shop", "title": "Pay the invoice", "key": "acme/shop/pay"},
                               actor="agent")["task"]
        self.out = self.tmp / "out"
        self.out.mkdir()

    def recorder(self, name):
        """Hook script that dumps its env and stdin to out/<name>.<seq>.{env,json}."""
        return self.write_hook(name, f'''
seq="$FTASK_SEQ"
env | grep '^FTASK_' | sort > "{self.out}/{name}.$seq.env"
cat > "{self.out}/{name}.$seq.json"
''')

    def files(self, prefix):
        return sorted(p.name for p in self.out.iterdir() if p.name.startswith(prefix) and p.suffix == ".json")

    def test_on_done_gets_env_and_json_stdin(self):
        self.recorder("on-done")
        self.s.set_status("acme/shop/pay", "done", actor="founder")
        name = eventually(lambda: self.files("on-done"))[0]
        seq = name.split(".")[1]
        env = dict(line.split("=", 1) for line in (self.out / f"on-done.{seq}.env").read_text().splitlines())
        self.assertEqual((env["FTASK_ID"], env["FTASK_KEY"], env["FTASK_EVENT"], env["FTASK_PROJECT"]),
                         (self.t["id"], "acme/shop/pay", "done", "Shop"))
        self.assertEqual(env["FTASK_TITLE"], "Pay the invoice")
        self.assertEqual(env["FTASK_DB"], self.db)
        ev = json.loads((self.out / name).read_text())
        self.assertEqual((ev["kind"], ev["actor"], ev["task_id"], ev["task_key"], ev["task_title"], ev["task_project"]),
                         ("done", "founder", self.t["id"], "acme/shop/pay", "Pay the invoice", "Shop"))
        self.assertEqual(ev["seq"], int(seq))
        log = (self.data_dir / "hooks.log").read_text()
        self.assertIn("on-done", log)
        self.assertIn("rc=0", log)

    def test_which_hooks_run_for_which_events(self):
        for n in ("on-done", "on-note", "on-upload", "on-any"):
            self.recorder(n)
        self.s.set_note("acme/shop/pay", "hello", actor="founder")
        self.s.ingest("acme/shop/pay", io.BytesIO(b"zz"), name="a.csv", size=2, by="founder", direction="founder")
        self.s.set_step("acme/shop/pay", 0, True, actor="founder") if self.s.get("acme/shop/pay")["steps"] else None
        self.s.set_status("acme/shop/pay", "done", actor="founder")
        eventually(lambda: len(self.files("on-done")) == 1 and len(self.files("on-note")) == 1
                   and len(self.files("on-upload")) == 1 and len(self.files("on-any")) == 3)
        time.sleep(0.5)
        self.assertEqual((len(self.files("on-done")), len(self.files("on-note")), len(self.files("on-upload")),
                          len(self.files("on-any"))), (1, 1, 1, 3))
        kinds = sorted(json.loads((self.out / n).read_text())["kind"] for n in self.files("on-any"))
        self.assertEqual(kinds, ["done", "note", "upload"])

    def test_agent_events_and_non_executable_files_do_not_trigger(self):
        self.recorder("on-any")
        (self.hooks_dir / "on-done").write_text("#!/bin/sh\ntouch %s/not-executable\n" % self.out)  # no +x
        self.s.set_note("acme/shop/pay", "agent", actor="shop/claude")
        f = self.tmp / "f.txt"
        f.write_text("x")
        self.s.attach_path("acme/shop/pay", str(f), by="shop/claude")
        self.s.set_status("acme/shop/pay", "done", actor="shop/claude")
        time.sleep(1.5)
        self.assertEqual(os.listdir(self.out), [])

    def test_hook_does_not_block_the_writer(self):
        self.write_hook("on-done", "sleep 5\ntouch %s/finished\n" % self.out)
        t0 = time.monotonic()
        self.s.set_status("acme/shop/pay", "done", actor="founder")
        self.assertLess(time.monotonic() - t0, 1.5)
        t0 = time.monotonic()
        p = self.ftask("done", "acme/shop/pay", "--by", "founder")  # already done: no event, just checking speed
        self.assertEqual(p.returncode, 0)
        self.assertFalse((self.out / "finished").exists())
        eventually(lambda: (self.out / "finished").exists(), timeout=15)

    def test_cli_with_by_founder_runs_hooks_and_returns_fast(self):
        self.write_hook("on-any", "sleep 3\ncat > %s/cli.json\n" % self.out)
        t0 = time.monotonic()
        self.ftask("note", "acme/shop/pay", "from the terminal", "--by", "founder", check=0)
        self.assertLess(time.monotonic() - t0, 2.5)
        eventually(lambda: (self.out / "cli.json").exists(), timeout=15)
        self.assertEqual(json.loads((self.out / "cli.json").read_text())["detail"]["note"], "from the terminal")

    def test_hook_timeout_kills_and_logs(self):
        self.write_hook("on-done", "echo started\nsleep 30\ntouch %s/survived\n" % self.out)
        os.environ["FTASK_HOOK_TIMEOUT"] = "1"
        self.addCleanup(lambda: os.environ.pop("FTASK_HOOK_TIMEOUT", None))
        self.s.set_status("acme/shop/pay", "done", actor="founder")
        eventually(lambda: "TIMEOUT" in ((self.data_dir / "hooks.log").read_text() if (self.data_dir / "hooks.log").exists() else ""), timeout=15)
        log = (self.data_dir / "hooks.log").read_text()
        self.assertIn("started", log)
        time.sleep(0.5)
        self.assertFalse((self.out / "survived").exists())

    def test_failing_hook_is_logged_and_next_hook_still_runs(self):
        self.write_hook("on-done", "echo oops >&2\nexit 3\n")
        self.recorder("on-any")
        self.s.set_status("acme/shop/pay", "done", actor="founder")
        eventually(lambda: self.files("on-any"))
        eventually(lambda: "rc=3" in (self.data_dir / "hooks.log").read_text())
        self.assertIn("oops", (self.data_dir / "hooks.log").read_text())

    def test_hooks_do_not_recurse(self):
        # a hook that itself records a founder event must not retrigger hooks
        self.write_hook("on-note", 'echo x >> "%s/ran"\nftask_py="%s"\n' % (self.out, "")
                        + f'{os.sys.executable} {Path(__file__).resolve().parent.parent / "bin" / "ftask"} note "$FTASK_ID" "again" --by founder\n')
        self.s.set_note("acme/shop/pay", "first", actor="founder")
        eventually(lambda: (self.out / "ran").exists())
        eventually(lambda: self.s.get("acme/shop/pay")["note"] == "again")
        time.sleep(1.5)
        self.assertEqual((self.out / "ran").read_text().count("x"), 1)

    def test_temp_database_never_uses_the_real_hooks_dir(self):
        del os.environ["FTASK_HOOKS_DIR"]
        self.assertIsNone(hooks.hooks_dir(self.db))
        real = hooks.real_default_db()
        self.assertEqual(hooks.hooks_dir(real), paths.hooks_dir())

    def test_notify_cmd_runs_for_founder_events_only(self):
        out = self.out
        cmd = f'cat > "{out}/n.$FTASK_SEQ.json"; echo "$FTASK_EVENT $FTASK_KEY $FTASK_PROJECT" > "{out}/n.$FTASK_SEQ.txt"'
        r = self.s.upsert({"project": "Shop", "title": "Pay the invoice", "key": "acme/shop/pay", "notify_cmd": cmd},
                          actor="agent")
        self.assertEqual(r["action"], "updated")
        self.assertEqual(self.s.get("acme/shop/pay")["notify_cmd"], cmd)
        self.s.set_note("acme/shop/pay", "agent note", actor="shop/claude")  # not a founder event
        self.s.set_note("acme/shop/pay", "founder note", actor="founder")
        txt = eventually(lambda: [p for p in out.iterdir() if p.suffix == ".txt"])
        self.assertEqual(txt[0].read_text().strip(), "note acme/shop/pay Shop")
        time.sleep(0.5)
        self.assertEqual(len([p for p in out.iterdir() if p.suffix == ".txt"]), 1)
        ev = json.loads(next(p for p in out.iterdir() if p.suffix == ".json").read_text())
        self.assertEqual(ev["detail"]["note"], "founder note")
        self.assertIn("notify_cmd", (self.data_dir / "hooks.log").read_text())
        # clearing it stops the notifications
        self.s.upsert({"project": "Shop", "title": "Pay the invoice", "key": "acme/shop/pay", "notify_cmd": ""},
                      actor="agent")
        self.s.set_status("acme/shop/pay", "done", actor="founder")
        time.sleep(1.5)
        self.assertEqual(len([p for p in out.iterdir() if p.suffix == ".txt"]), 1)

    def test_notify_cmd_via_cli_flag_is_time_limited_and_nonblocking(self):
        os.environ["FTASK_HOOK_TIMEOUT"] = "1"
        self.addCleanup(lambda: os.environ.pop("FTASK_HOOK_TIMEOUT", None))
        self.ftask("add", "--project", "P", "--title", "T", "--key", "p/t", "--notify-cmd", "sleep 20", check=0)
        t0 = time.monotonic()
        self.ftask("done", "p/t", "--by", "founder", check=0)
        self.assertLess(time.monotonic() - t0, 2.5)
        eventually(lambda: "TIMEOUT" in ((self.data_dir / "hooks.log").read_text() if (self.data_dir / "hooks.log").exists() else ""), timeout=15)
        self.assertIn("notify_cmd: sleep 20", self.ftask("show", "p/t", check=0).stdout)


class HttpHookTests(TempDbCase):
    def setUp(self):
        super().setUp()
        self.srv = server.make_server(self.db, "127.0.0.1", 0)
        self.srv.quiet = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))
        self.store = self.srv.app.store
        self.t = self.store.upsert({"project": "P", "title": "T", "key": "k"}, actor="agent")["task"]

    def post(self, path, body):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", path, body=json.dumps(body), headers={
            "Host": f"localhost:{self.port}", "Content-Type": "application/json", "X-CSRF-Token": self.srv.app.token})
        r = c.getresponse()
        j = json.loads(r.read())
        c.close()
        return r.status, j

    def test_ui_events_run_hooks_and_report_notified(self):
        out = self.tmp / "o.txt"
        # nobody listening: plain "Done"
        s, j = self.post(f"/api/tasks/{self.t['id']}/status", {"status": "done"})
        self.assertEqual((s, j["notified"], j["listeners"]["any"]), (200, False, False))
        self.post(f"/api/tasks/{self.t['id']}/status", {"status": "open"})
        # an installed hook
        self.write_hook("on-done", f'echo "$FTASK_EVENT $FTASK_ID" >> {out}\n')
        s, j = self.post(f"/api/tasks/{self.t['id']}/status", {"status": "done"})
        self.assertTrue(j["notified"])
        self.assertEqual(j["listeners"]["hooks"], ["on-done"])
        eventually(lambda: out.exists())
        self.assertEqual(out.read_text().strip(), f"done {self.t['id']}")
        # a running `ftask wait`
        (self.hooks_dir / "on-done").unlink()
        self.post(f"/api/tasks/{self.t['id']}/status", {"status": "open"})
        p = self.spawn("wait", "k", "--for", "done")
        eventually(lambda: self.store.listeners("k", "done")["waiters"] == 1)
        s, j = self.post(f"/api/tasks/{self.t['id']}/status", {"status": "done"})
        self.assertTrue(j["notified"])
        self.assertEqual(j["listeners"]["waiters"], 1)
        self.assertEqual(p.wait(timeout=10), 0)
        # a waiter for notes only does not count as hearing "done"
        self.post(f"/api/tasks/{self.t['id']}/status", {"status": "open"})
        p = self.spawn("wait", "k", "--for", "note", "--timeout", "30")
        eventually(lambda: self.store.listeners("k", "note")["waiters"] == 1)
        s, j = self.post(f"/api/tasks/{self.t['id']}/status", {"status": "done"})
        self.assertFalse(j["notified"])
        p.kill()
        # a no-op (already done) never claims a notification
        s, j = self.post(f"/api/tasks/{self.t['id']}/status", {"status": "done"})
        self.assertFalse(j["notified"])
        # notify_cmd
        self.post(f"/api/tasks/{self.t['id']}/status", {"status": "open"})
        self.store.upsert({"project": "P", "title": "T", "key": "k", "notify_cmd": "true"}, actor="agent")
        s, j = self.post(f"/api/tasks/{self.t['id']}/status", {"status": "done"})
        self.assertTrue(j["notified"])
        self.assertTrue(j["listeners"]["notify_cmd"])


if __name__ == "__main__":
    unittest.main()
