import json
import os
import subprocess
import sys
import unittest

from helpers import FTASK, TempDbCase


class ConcurrencyTests(TempDbCase):
    def _spawn(self, args):
        env = dict(os.environ, FTASK_DB=self.db)
        return subprocess.Popen([sys.executable, FTASK, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env)

    def _wait_all(self, procs):
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, f"stdout={out!r} stderr={err!r}")

    def test_parallel_adds_on_fresh_db(self):
        # fresh db: also races first-open (WAL switch + migration)
        procs = [self._spawn(["add", "--project", "P", "--title", f"task {i}", "--by", f"w{i % 5}"]) for i in range(40)]
        self._wait_all(procs)
        tasks = json.loads(self.ftask("list", "--json", check=0).stdout)
        self.assertEqual(len(tasks), 40)
        self.assertEqual(len({t["id"] for t in tasks}), 40)
        evs = json.loads(self.ftask("events", "--json", check=0).stdout)
        self.assertEqual([e["seq"] for e in evs], sorted(e["seq"] for e in evs))
        self.assertEqual(len(evs), 40)

    def test_parallel_upsert_same_key_makes_one_task(self):
        procs = [self._spawn(["add", "--project", "P", "--title", f"v{i}", "--key", "same/key"]) for i in range(25)]
        self._wait_all(procs)
        tasks = json.loads(self.ftask("list", "--status", "all", "--json", check=0).stdout)
        self.assertEqual(len(tasks), 1)

    def test_mixed_readers_and_writers(self):
        self.ftask("add", "--project", "P", "--title", "T", "--key", "k", "--step", "a", "--step", "b", check=0)
        procs = []
        for i in range(12):
            procs.append(self._spawn(["note", "k", f"note {i}"]))
            procs.append(self._spawn(["list", "--json"]))
            procs.append(self._spawn(["add", "--project", "Q", "--title", f"x{i}"]))
        self._wait_all(procs)
        self.assertEqual(len(json.loads(self.ftask("list", "--json", check=0).stdout)), 13)
        notes = [e for e in json.loads(self.ftask("events", "--json", check=0).stdout) if e["kind"] == "note"]
        self.assertEqual(len(notes), 12)


if __name__ == "__main__":
    unittest.main()
