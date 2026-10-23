import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FTASK = str(ROOT / "bin" / "ftask")
sys.path.insert(0, str(ROOT))


def eventually(fn, timeout=10.0, interval=0.05):
    """Poll fn() until it returns something truthy (returned), or fail after `timeout` seconds."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError("condition not met within %.1fs" % timeout)


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = os.path.join(self._tmp.name, "sub", "tasks.db")  # parent dir does not exist yet
        self.data_dir = Path(self.db).parent
        self.hooks_dir = self.tmp / "hooks"  # never the real the user's real hooks directory
        self.hooks_dir.mkdir()
        saved = {k: os.environ.get(k) for k in ("FTASK_HOOKS_DIR", "FTASK_DB", "FTASK_BY", "FTASK_IN_HOOK")}
        os.environ["FTASK_HOOKS_DIR"] = str(self.hooks_dir)
        os.environ["FTASK_DB"] = self.db
        os.environ.pop("FTASK_BY", None)
        os.environ.pop("FTASK_IN_HOOK", None)

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)

    def env(self, extra=None):
        e = dict(os.environ, FTASK_DB=self.db, FTASK_HOOKS_DIR=str(self.hooks_dir))
        e.pop("FTASK_BY", None)
        e.update(extra or {})
        return e

    def ftask(self, *args, input=None, env=None, check=None):
        p = subprocess.run([sys.executable, FTASK, *args], capture_output=True, text=True, input=input,
                           env=self.env(env), timeout=60)
        if check is not None:
            self.assertEqual(p.returncode, check, f"args={args} stdout={p.stdout!r} stderr={p.stderr!r}")
        return p

    def spawn(self, *args, env=None):
        """Start ftask in the background (for `wait`); returns the Popen."""
        p = subprocess.Popen([sys.executable, FTASK, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=self.env(env))

        def reap():
            if p.poll() is None:
                p.kill()
                p.wait()
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()
        self.addCleanup(reap)
        return p

    def write_hook(self, name, body):
        p = self.hooks_dir / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)
        return p
