import sqlite3
import unittest

from helpers import TempDbCase
from ftask import store as st


class StoreTests(TempDbCase):
    def setUp(self):
        super().setUp()
        self.s = st.Store(self.db)

    def add(self, **kw):
        d = {"project": "Shop", "title": "T"}
        d.update(kw)
        return self.s.upsert(d, actor="tester")

    def test_pragmas_and_migration(self):
        c = st.connect(self.db)
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(c.execute("PRAGMA busy_timeout").fetchone()[0], st.BUSY_TIMEOUT_MS)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], len(st.MIGRATIONS))
        c.close()
        st.connect(self.db).close()  # re-open: migrations are idempotent

    def test_create_defaults(self):
        r = self.add(why="because", minutes="15", steps=["a", {"text": "b", "value": "x\ny", "link": "https://e.com"}])
        t = r["task"]
        self.assertEqual(r["action"], "created")
        self.assertEqual(len(t["id"]), 6)
        self.assertEqual((t["status"], t["priority"], t["minutes"], t["created_by"]), ("open", 100, 15, "tester"))
        self.assertEqual(t["steps"][1], {"text": "b", "value": "x\ny", "link": "https://e.com"})
        self.assertEqual(t["steps_done"], [])
        self.assertIsNone(t["done_at"])

    def test_waiting_on_implies_waiting(self):
        t = self.add(waiting_on="Apple")["task"]
        self.assertEqual(t["status"], "waiting")
        self.assertEqual(t["waiting_on"], "Apple")

    def test_validation(self):
        bad = [
            {"project": "", "title": "x"},
            {"project": "p"},
            {"project": "p", "title": "x", "minutes": "abc"},
            {"project": "p", "title": "x", "priority": 1.5},
            {"project": "p", "title": "x", "steps": [{"text": "a", "link": "javascript:alert(1)"}]},
            {"project": "p", "title": "x", "steps": [{"text": ""}]},
            {"project": "p", "title": "x", "steps": [{"text": "a", "bogus": 1}]},
            {"project": "p", "title": "x", "key": "has space"},
            {"project": "p", "title": "x", "nope": 1},
            {"project": "p", "title": "x", "status": "waiting"},
            {"project": "p", "title": "line\nbreak"},
        ]
        for d in bad:
            with self.assertRaises(st.ValidationError, msg=str(d)):
                self.s.upsert(d)

    def test_upsert_idempotent_and_keeps_progress(self):
        steps = [{"text": "one", "value": "v1"}, {"text": "two"}]
        a = self.add(key="k/1", steps=steps)
        tid = a["task"]["id"]
        self.s.set_step(tid, 0, True, actor="founder")
        self.s.set_note(tid, "my note", actor="founder")
        # identical re-add: no change, no event
        before = self.s.version()
        b = self.add(key="k/1", steps=steps)
        self.assertEqual(b["action"], "unchanged")
        self.assertEqual(b["task"]["id"], tid)
        self.assertEqual(self.s.version(), before)
        # same shape, new value: ticks + note kept
        steps2 = [{"text": "one", "value": "v1-new"}, {"text": "two"}]
        c = self.add(key="k/1", steps=steps2, title="T2")
        self.assertEqual(c["action"], "updated")
        self.assertEqual(c["task"]["steps_done"], [0])
        self.assertEqual(c["task"]["note"], "my note")
        self.assertEqual(c["task"]["title"], "T2")
        # new shape: ticks reset, note kept
        d = self.add(key="k/1", steps=steps2 + [{"text": "three"}])
        self.assertEqual(d["task"]["steps_done"], [])
        self.assertEqual(d["task"]["note"], "my note")
        self.assertEqual(len(self.s.list(None, None)), 1)

    def test_upsert_partial_fields_untouched(self):
        self.add(key="k", why="orig", minutes=5, priority=3)
        r = self.add(key="k", title="only title changes")
        t = r["task"]
        self.assertEqual((t["why"], t["minutes"], t["priority"]), ("orig", 5, 3))

    def test_done_not_reopened_without_flag(self):
        t = self.add(key="k")["task"]
        self.s.set_status(t["id"], "done", actor="founder")
        r = self.add(key="k", why="new why")
        self.assertTrue(r["closed"])
        self.assertEqual(r["task"]["status"], "done")
        self.assertEqual(r["task"]["why"], "new why")
        r = self.s.upsert({"project": "Shop", "title": "T", "key": "k"}, reopen=True)
        self.assertEqual(r["task"]["status"], "open")
        self.assertTrue(r["reopened"])
        self.assertIsNone(r["task"]["done_at"])

    def test_status_transitions_and_events(self):
        t = self.add()["task"]
        with self.assertRaises(st.ValidationError):
            self.s.set_status(t["id"], "waiting", actor="x")
        t = self.s.set_status(t["id"], "waiting", actor="agent", waiting_on="Bank")["task"]
        self.assertEqual((t["status"], t["waiting_on"]), ("waiting", "Bank"))
        t = self.s.set_status(t["id"], "done", actor="founder")["task"]
        self.assertEqual((t["status"], t["waiting_on"]), ("done", ""))
        self.assertTrue(t["done_at"])
        self.assertFalse(self.s.set_status(t["id"], "done", actor="founder")["changed"])
        t = self.s.set_status(t["id"], "open", actor="founder")["task"]
        self.assertIsNone(t["done_at"])
        kinds = [(e["actor"], e["kind"]) for e in self.s.task_events(t["id"])]
        self.assertEqual(kinds, [("tester", "created"), ("agent", "waiting"), ("founder", "done"), ("founder", "reopened")])

    def test_steps(self):
        t = self.add(steps=["a", "b", "c"])["task"]
        self.s.set_step(t["id"], 2, True)
        self.s.set_step(t["id"], 0, True)
        self.assertEqual(self.s.get(t["id"])["steps_done"], [0, 2])
        self.assertFalse(self.s.set_step(t["id"], 0, True)["changed"])
        self.s.set_step(t["id"], 0, False)
        self.assertEqual(self.s.get(t["id"])["steps_done"], [2])
        with self.assertRaises(st.ValidationError):
            self.s.set_step(t["id"], 3, True)

    def test_resolve_by_id_key_prefix(self):
        t = self.add(key="a/b")["task"]
        self.assertEqual(self.s.get(t["id"])["id"], t["id"])
        self.assertEqual(self.s.get("a/b")["id"], t["id"])
        self.assertEqual(self.s.get(t["id"][:4])["id"], t["id"])
        with self.assertRaises(st.NotFound):
            self.s.get("zzzzzz")

    def test_list_order_filter(self):
        a = self.add(title="late", priority=50, project="A")["task"]
        b = self.add(title="soon", priority=1, project="B")["task"]
        c = self.add(title="done", priority=0, project="A")["task"]
        self.s.set_status(c["id"], "done")
        self.assertEqual([t["title"] for t in self.s.list(("open",))], ["soon", "late"])
        self.assertEqual([t["title"] for t in self.s.list(("open",), project="a")], ["late"])
        self.assertEqual({t["title"] for t in self.s.list(None)}, {"late", "soon", "done"})

    def test_notes_since(self):
        t = self.add(key="n")["task"]
        u = self.add(title="other")["task"]
        mark = st.now()
        self.assertEqual(self.s.notes(mark), [])
        self.s.set_note(t["id"], "from founder", actor="founder")
        self.s.set_status(u["id"], "done", actor="founder")
        got = {r["id"]: r for r in self.s.notes(mark)}
        self.assertEqual(set(got), {t["id"], u["id"]})
        self.assertEqual(got[t["id"]]["note"], "from founder")
        self.assertIsNotNone(got[u["id"]]["closed_at"])
        later = st.now()
        self.assertEqual(self.s.notes(later), [])

    def test_events_since_after(self):
        self.add()
        v = self.s.version()
        self.add(title="two")
        evs = self.s.events(after_seq=v)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["task_title"], "two")

    def test_parse_since(self):
        self.assertTrue(st.parse_since("2026-10-22").startswith("2026-10-22T00:00:00"))
        self.assertTrue(st.parse_since("2026-10-22T10:00:00+01:00").startswith("2026-10-22T09:00:00"))
        self.assertTrue(st.parse_since("2h") < st.now())
        with self.assertRaises(st.ValidationError):
            st.parse_since("yesterday-ish")

    def test_backup(self):
        self.add(title="keep me")
        dest = self.db + ".bak"
        self.s.backup(dest)
        c = sqlite3.connect(dest)
        self.assertEqual(c.execute("SELECT title FROM tasks").fetchone()[0], "keep me")
        c.close()


if __name__ == "__main__":
    unittest.main()
