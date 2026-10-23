import json
import os
import resource
import subprocess
import sys
import threading
import time
import unittest

from helpers import FTASK, TempDbCase, eventually
from ftask import store as st


class WaitTests(TempDbCase):
    def setUp(self):
        super().setUp()
        self.s = st.Store(self.db)
        self.t = self.s.upsert({"project": "P", "title": "T", "key": "acme/p/t", "steps": [
            {"text": "a"}, {"text": "up", "upload": True}]}, actor="agent")["task"]
        self.o = self.s.upsert({"project": "Q", "title": "O", "key": "other/q/o"}, actor="agent")["task"]

    def later(self, fn, delay=0.6):
        th = threading.Timer(delay, fn)
        th.daemon = True
        th.start()
        self.addCleanup(th.cancel)

    def finish(self, p, timeout=15):
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err

    def test_wait_done_wakes_when_founder_marks_done(self):
        p = self.spawn("wait", "acme/p/t", "--for", "done", "--json")
        eventually(lambda: self.s.listeners("acme/p/t", "done")["waiters"] == 1)
        self.later(lambda: self.s.set_status("acme/p/t", "done", actor="founder"), 0.2)
        rc, out, _ = self.finish(p)
        self.assertEqual(rc, 0)
        j = json.loads(out)
        self.assertFalse(j["timeout"])
        self.assertEqual((j["task"]["status"], j["events"][0]["kind"], j["events"][0]["actor"]), ("done", "done", "founder"))
        self.assertEqual(j["cursor"], self.s.version())
        eventually(lambda: self.s.listeners("acme/p/t", "done")["waiters"] == 0)  # deregistered on exit

    def test_already_done_returns_immediately(self):
        self.s.set_status("acme/p/t", "done", actor="founder")
        t0 = time.monotonic()
        p = self.ftask("wait", "acme/p/t", "--for", "done", check=0)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("already done", p.stdout)
        self.assertIn(f"cursor: {self.s.version()}", p.stdout)
        j = json.loads(self.ftask("wait", "acme/p/t", "--for", "any", "--json", check=0).stdout)
        self.assertEqual(j["reason"], "already-done")

    def test_cancelled_counts_as_closed(self):
        p = self.spawn("wait", self.t["id"], "--for", "done")
        self.later(lambda: self.s.set_status(self.t["id"], "cancelled", actor="founder"))
        rc, out, _ = self.finish(p)
        self.assertEqual(rc, 0)
        self.assertIn("cancelled", out)

    def test_wait_note_prints_the_note(self):
        p = self.spawn("wait", "acme/p/t", "--for", "note")
        self.later(lambda: self.s.set_note("acme/p/t", "use the other account\nthanks", actor="founder"))
        rc, out, _ = self.finish(p)
        self.assertEqual(rc, 0)
        self.assertIn("use the other account", out)
        self.assertIn("cursor:", out)

    def test_note_by_agent_does_not_wake_note_waiter(self):
        p = self.spawn("wait", "acme/p/t", "--for", "note", "--timeout", "2")
        self.later(lambda: self.s.set_note("acme/p/t", "agent talking", actor="p/claude"), 0.3)
        rc, out, _ = self.finish(p)
        self.assertEqual(rc, 124)

    def test_wait_upload_prints_path(self):
        p = self.spawn("wait", "acme/p/t", "--for", "upload", "--json")
        import io
        self.later(lambda: self.s.ingest("acme/p/t", io.BytesIO(b"abc"), name="rec.mp4", size=3, step_index=1,
                                         by="founder", direction="founder"))
        rc, out, _ = self.finish(p)
        self.assertEqual(rc, 0)
        j = json.loads(out)
        self.assertEqual(j["events"][0]["kind"], "upload")
        self.assertTrue(j["events"][0]["detail"]["path"].endswith("rec.mp4"))
        self.assertEqual(j["task"]["files"][0]["name"], "rec.mp4")

    def test_agent_attach_does_not_count_as_upload(self):
        p = self.spawn("wait", "acme/p/t", "--for", "any", "--timeout", "2")
        f = self.tmp / "x.txt"
        f.write_text("x")
        self.later(lambda: self.s.attach_path("acme/p/t", str(f), by="p/claude"), 0.3)
        self.assertEqual(self.finish(p)[0], 124)

    def test_any_wakes_on_step_tick_and_ignores_agent_edits(self):
        p = self.spawn("wait", "acme/p/t", "--for", "any", "--json")
        self.later(lambda: self.s.upsert({"project": "P", "title": "T2", "key": "acme/p/t"}, actor="p/claude"), 0.2)
        self.later(lambda: self.s.set_step("acme/p/t", 0, True, actor="founder"), 1.0)
        rc, out, _ = self.finish(p)
        j = json.loads(out)
        self.assertEqual((rc, j["events"][0]["kind"]), (0, "step"))

    def test_timeout_exit_code_and_cursor(self):
        t0 = time.monotonic()
        p = self.ftask("wait", "acme/p/t", "--for", "any", "--timeout", "1")
        self.assertEqual(p.returncode, 124)
        self.assertLess(time.monotonic() - t0, 8)
        self.assertIn(f"cursor: {self.s.version()}", p.stdout)
        j = json.loads(self.ftask("wait", "acme/p/t", "--timeout", "0.5", "--json").stdout)
        self.assertTrue(j["timeout"])
        self.assertEqual(j["cursor"], self.s.version())

    def test_since_cursor_misses_nothing(self):
        c0 = self.s.version()
        self.s.set_note("acme/p/t", "first", actor="founder")
        self.s.set_step("acme/p/t", 0, True, actor="founder")
        a = json.loads(self.ftask("wait", "acme/p/t", "--for", "any", "--since", str(c0), "--json", check=0).stdout)
        self.assertEqual([e["kind"] for e in a["events"]], ["note", "step"])  # both events returned together
        self.s.set_note("acme/p/t", "second", actor="founder")
        b = json.loads(self.ftask("wait", "acme/p/t", "--for", "note", "--since", str(a["cursor"]), "--json", check=0).stdout)
        self.assertEqual([e["detail"]["note"] for e in b["events"]], ["second"])
        c = self.ftask("wait", "acme/p/t", "--for", "any", "--since", str(b["cursor"]), "--timeout", "0.5")
        self.assertEqual(c.returncode, 124)

    def test_since_zero_replays_history(self):
        self.s.set_note("acme/p/t", "old", actor="founder")
        j = json.loads(self.ftask("wait", "acme/p/t", "--for", "note", "--since", "0", "--json", check=0).stdout)
        self.assertEqual(j["events"][0]["detail"]["note"], "old")

    def test_since_on_finished_task_waits_for_new_events_only_for_any(self):
        self.s.set_status("acme/p/t", "done", actor="founder")
        cur = self.s.version()
        p = self.ftask("wait", "acme/p/t", "--for", "any", "--since", str(cur), "--timeout", "0.5")
        self.assertEqual(p.returncode, 124)
        # but --for done is level-triggered
        self.ftask("wait", "acme/p/t", "--for", "done", "--since", str(cur), "--timeout", "0.5", check=0)

    def test_key_prefix(self):
        p = self.spawn("wait", "--key-prefix", "acme/", "--for", "any", "--json")
        eventually(lambda: self.s.listeners("acme/p/t", "done")["waiters"] == 1)
        self.assertEqual(self.s.listeners("other/q/o", "done")["waiters"], 0)
        self.later(lambda: self.s.set_note(self.o["id"], "not for you", actor="founder"), 0.2)
        self.later(lambda: self.s.set_note(self.t["id"], "for you", actor="founder"), 0.8)
        rc, out, _ = self.finish(p)
        j = json.loads(out)
        self.assertEqual((rc, j["events"][0]["task_key"], j["events"][0]["detail"]["note"]),
                         (0, "acme/p/t", "for you"))
        # prefix wait does not answer from state: it waits for events
        q = self.ftask("wait", "--key-prefix", "acme/", "--for", "done", "--timeout", "0.5")
        self.assertEqual(q.returncode, 124)

    def test_errors(self):
        self.ftask("wait", "nope-nope", "--for", "done", "--timeout", "1", check=3)
        self.ftask("wait", "acme/p/t", "--key-prefix", "x/", check=2)
        self.ftask("wait", "--for", "done", check=2)
        self.ftask("wait", "acme/p/t", "--timeout", "-1", check=2)

    def test_polling_is_cheap(self):
        p = subprocess.Popen([sys.executable, FTASK, "wait", "acme/p/t", "--for", "any", "--timeout", "4"],
                             env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, status, ru = os.wait4(p.pid, 0)
        cpu = ru.ru_utime + ru.ru_stime
        p.returncode = os.waitstatus_to_exitcode(status)  # we reaped it ourselves
        self.assertEqual(p.returncode, 124)
        self.assertLess(cpu, 1.0, f"waiting used {cpu:.2f}s of CPU in 4s")
        p.stdout.close()
        p.stderr.close()

    def test_sigterm_deregisters(self):
        p = self.spawn("wait", "acme/p/t", "--for", "any")
        eventually(lambda: self.s.listeners("acme/p/t", "done")["waiters"] == 1)
        p.terminate()
        p.communicate(timeout=10)
        self.assertEqual(self.s.listeners("acme/p/t", "done")["waiters"], 0)


if __name__ == "__main__":
    unittest.main()
