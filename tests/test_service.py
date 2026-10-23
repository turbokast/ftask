import plistlib
import tempfile
import unittest
from pathlib import Path

from helpers import TempDbCase
from ftask import service


class RenderTests(unittest.TestCase):
    def test_systemd_quotes_awkward_paths(self):
        unit = service.render_systemd(["/opt/my tools/ftask", "serve", "--port", "4747"], {"FTASK_DB": "/data/a b/t.db"})
        self.assertIn('ExecStart="/opt/my tools/ftask" serve --port 4747', unit)
        self.assertIn('Environment="FTASK_DB=/data/a b/t.db"', unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_launchd_plist_roundtrips(self):
        text = service.render_launchd(["/usr/local/bin/ftask", "serve"], {"FTASK_DB": "/x/t.db"}, log=Path("/tmp/ftask.log"))
        d = plistlib.loads(text.encode())
        self.assertEqual(d["Label"], service.LAUNCHD_LABEL)
        self.assertEqual(d["ProgramArguments"], ["/usr/local/bin/ftask", "serve"])
        self.assertEqual(d["EnvironmentVariables"]["FTASK_DB"], "/x/t.db")
        self.assertTrue(d["RunAtLoad"])


class CliServiceTests(TempDbCase):
    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp(dir=self.tmp))
        self.isolated = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "cfg")}

    def test_print_writes_nothing(self):
        p = self.ftask("service", "install", "--print", "--manager", "systemd", env=self.isolated, check=0)
        self.assertIn("ExecStart=", p.stdout)
        self.assertFalse((self.home / "cfg").exists())

    def test_install_no_start_then_uninstall_systemd(self):
        self.ftask("service", "install", "--manager", "systemd", "--no-start", "--port", "4999", env=self.isolated, check=0)
        unit = self.home / "cfg/systemd/user/ftask.service"
        self.assertIn("--port 4999", unit.read_text())
        self.assertIn(self.db, unit.read_text())  # FTASK_DB set at install time is carried into the unit

    def test_install_no_start_launchd(self):
        self.ftask("service", "install", "--manager", "launchd", "--no-start", env=self.isolated, check=0)
        plist = self.home / "Library/LaunchAgents/ftask.serve.plist"
        self.assertEqual(plistlib.loads(plist.read_bytes())["ProgramArguments"][-5:-3], ["serve", "--host"])

    def test_snippet_and_version_need_no_database(self):
        p = self.ftask("snippet", check=0)
        self.assertIn("ftask wait", p.stdout)
        self.assertFalse(Path(self.db).exists())
        self.assertRegex(self.ftask("--version", check=0).stdout, r"^ftask \d+\.\d+\.\d+")

    def test_human_is_an_alias_for_the_stored_actor(self):
        self.ftask("add", "--project", "P", "--title", "T", "--key", "k", check=0)
        self.ftask("note", "k", "hello", "--by", "human", check=0)
        import json
        t = json.loads(self.ftask("show", "k", "--json", check=0).stdout)
        self.assertEqual([e["actor"] for e in t["events"] if e["kind"] == "note"], ["founder"])
        self.ftask("files", "k", "--from-human", check=0)


if __name__ == "__main__":
    unittest.main()
