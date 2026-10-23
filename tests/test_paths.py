import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (puts the repo root on sys.path)
from ftask import paths


class PathTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.home = Path(self._t.name)
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        for k in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "FTASK_DB"):
            os.environ.pop(k, None)

    def test_linux_defaults_and_xdg(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(paths.default_db_path(), self.home / ".local/share/ftask/tasks.db")
            self.assertEqual(paths.hooks_dir(), self.home / ".config/ftask/hooks")
            os.environ["XDG_DATA_HOME"] = str(self.home / "d")
            os.environ["XDG_CONFIG_HOME"] = str(self.home / "c")
            self.assertEqual(paths.default_db_path(), self.home / "d/ftask/tasks.db")
            self.assertEqual(paths.hooks_dir(), self.home / "c/ftask/hooks")

    def test_macos_uses_library_application_support(self):
        with mock.patch.object(sys, "platform", "darwin"):
            os.environ["XDG_DATA_HOME"] = str(self.home / "ignored")
            self.assertEqual(paths.default_db_path(), self.home / "Library/Application Support/ftask/tasks.db")
            self.assertEqual(paths.hooks_dir(), self.home / "Library/Application Support/ftask/hooks")

    def test_ftask_db_overrides(self):
        os.environ["FTASK_DB"] = "~/x/y.db"
        self.assertEqual(paths.default_db_path(), self.home / "x/y.db")
        self.assertEqual(paths.real_db_path(), paths.data_dir() / "tasks.db")  # hooks key off the real one

    def test_existing_prerelease_database_is_used_in_place(self):
        with mock.patch.object(sys, "platform", "linux"):
            old = self.home / ".local/share/founder-tasks"
            old.mkdir(parents=True)
            (old / "tasks.db").write_bytes(b"")
            (self.home / ".config/founder-tasks/hooks").mkdir(parents=True)
            self.assertEqual(paths.default_db_path(), old / "tasks.db")
            self.assertEqual(paths.hooks_dir(), self.home / ".config/founder-tasks/hooks")
            # once a new-style database exists it wins
            new = self.home / ".local/share/ftask"
            new.mkdir(parents=True)
            (new / "tasks.db").write_bytes(b"")
            self.assertEqual(paths.default_db_path(), new / "tasks.db")


if __name__ == "__main__":
    unittest.main()
