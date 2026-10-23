import errno
import hashlib
import http.client
import json
import os
import resource
import socket
import threading
import unittest
from pathlib import Path
from urllib.parse import quote
from unittest import mock

from helpers import TempDbCase
from ftask import files as fs
from ftask import server
from ftask import store as st


class SanitizeTests(unittest.TestCase):
    def test_names_are_single_safe_components(self):
        cases = {
            "../../etc/passwd": "passwd",
            "..\\..\\evil.exe": "evil.exe",
            "/abs/path/file.txt": "file.txt",
            "": "file",
            "..": "file",
            ".": "file",
            ".hidden": "hidden",
            "a\x00b\nc.txt": "a_b_c.txt",
            "report.pdf": "report.pdf",
            "na\u202eme.txt": "na_me.txt",
            "C:\\Users\\x\\clip.mp4": "clip.mp4",
            "dir/": "file",
        }
        for raw, want in cases.items():
            self.assertEqual(fs.sanitize_name(raw), want, raw)

    def test_long_names_keep_extension_and_fit(self):
        out = fs.sanitize_name("é" * 400 + ".mp4")
        self.assertLessEqual(len(out.encode()), fs.MAX_NAME_BYTES)
        self.assertTrue(out.endswith(".mp4"))
        out.encode("utf-8")

    def test_mime(self):
        self.assertEqual(fs.guess_mime("a.pdf"), "application/pdf")
        self.assertEqual(fs.guess_mime("a.weird123", "video/x-foo"), "video/x-foo")
        self.assertEqual(fs.guess_mime("a.weird123", "bad type"), "application/octet-stream")


class StoreFileTests(TempDbCase):
    def setUp(self):
        super().setUp()
        self.s = st.Store(self.db)
        self.t = self.s.upsert({"project": "P", "title": "T", "key": "p/t", "steps": [
            {"text": "a"}, {"text": "b", "upload": True, "accept": "video/*,.csv", "label": "Recording"}]})["task"]

    def src(self, name, data=b"hello"):
        p = self.tmp / "src" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(data)
        return str(p)

    def test_step_fields_validated(self):
        t = self.s.get("p/t")
        self.assertEqual(t["steps"][1], {"text": "b", "upload": True, "accept": "video/*,.csv", "label": "Recording"})
        self.assertEqual(t["steps"][0], {"text": "a"})  # existing shape untouched
        for bad in ({"text": "x", "accept": ".csv"},  # accept without upload
                    {"text": "x", "upload": True, "accept": "not a type"},
                    {"text": "x", "upload": "yes"}):
            with self.assertRaises(st.ValidationError):
                self.s.upsert({"project": "P", "title": "bad", "steps": [bad]})

    def test_attach_copies_hashes_and_logs(self):
        data = os.urandom(300_000)
        r = self.s.attach_path("p/t", self.src("cover.pdf", data), label="Cover", by="p/claude")
        f = r["file"]
        self.assertEqual((r["action"], f["name"], f["size"], f["direction"], f["mime"]),
                         ("created", "cover.pdf", len(data), "agent", "application/pdf"))
        self.assertEqual(f["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(Path(f["path"]).read_bytes(), data)
        self.assertTrue(f["path"].startswith(str(self.data_dir / "files" / self.t["id"]) + os.sep))
        self.assertEqual(oct(os.stat(f["path"]).st_mode & 0o777), "0o600")
        ev = self.s.task_events("p/t")[-1]
        self.assertEqual((ev["kind"], ev["actor"], ev["detail"]["file_id"], ev["detail"]["path"]),
                         ("attached", "p/claude", f["id"], f["path"]))
        self.assertEqual(self.s.get("p/t")["files"][0]["id"], f["id"])

    def test_reattach_same_is_noop_changed_replaces_in_place(self):
        a = self.s.attach_path("p/t", self.src("x.txt", b"one"))
        b = self.s.attach_path("p/t", self.src("x.txt", b"one"))
        self.assertEqual((b["action"], b["file"]["id"]), ("unchanged", a["file"]["id"]))
        c = self.s.attach_path("p/t", self.src("x.txt", b"two"))
        self.assertEqual(c["action"], "replaced")
        self.assertEqual(c["file"]["path"], a["file"]["path"])  # same path: the name the founder picks never changes
        self.assertEqual(Path(c["file"]["path"]).read_bytes(), b"two")
        self.assertEqual(len(self.s.list_files("p/t")), 1)
        # same name on another step is a different file
        d = self.s.attach_path("p/t", self.src("x.txt", b"two"), step_index=0)
        self.assertEqual(d["action"], "created")
        self.assertNotEqual(d["file"]["path"], c["file"]["path"])
        self.assertTrue(d["file"]["path"].endswith("x (2).txt"))

    def test_step_must_exist(self):
        with self.assertRaises(st.ValidationError):
            self.s.attach_path("p/t", self.src("a"), step_index=5)

    def test_attach_rejects_missing_and_directories(self):
        with self.assertRaises(st.ValidationError):
            self.s.attach_path("p/t", str(self.tmp / "nope"))
        with self.assertRaises(st.ValidationError):
            self.s.attach_path("p/t", str(self.tmp))

    def test_remove_deletes_file_row_and_logs(self):
        f = self.s.attach_path("p/t", self.src("gone.txt"))["file"]
        self.s.remove_file(f["id"], ref="p/t", actor="x")
        self.assertFalse(os.path.exists(f["path"]))
        self.assertFalse((self.data_dir / "files" / self.t["id"]).exists())  # empty dir tidied
        self.assertEqual(self.s.list_files("p/t"), [])
        self.assertEqual(self.s.task_events("p/t")[-1]["kind"], "detached")
        with self.assertRaises(st.NotFound):
            self.s.remove_file(f["id"])
        other = self.s.upsert({"project": "P", "title": "other"})["task"]
        g = self.s.attach_path("p/t", self.src("keep.txt"))["file"]
        with self.assertRaises(st.NotFound):  # wrong task
            self.s.remove_file(g["id"], ref=other["id"])

    def test_symlinked_stored_file_is_never_served_or_followed(self):
        f = self.s.attach_path("p/t", self.src("a.txt", b"safe"))["file"]
        secret = self.tmp / "secret.txt"
        secret.write_text("TOP SECRET")
        os.unlink(f["path"])
        os.symlink(secret, f["path"])
        with self.assertRaises(st.NotFound):
            self.s.open_file(f["id"])
        self.s.remove_file(f["id"])
        self.assertTrue(secret.exists())  # only the link was removed

    def test_symlinked_task_dir_refused(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        root = self.data_dir / "files"
        root.mkdir(parents=True, exist_ok=True)
        os.symlink(elsewhere, root / self.t["id"])
        with self.assertRaises(st.StorageError):
            self.s.attach_path("p/t", self.src("a.txt"))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_disk_full_mid_write_cleans_up(self):
        class Boom:
            n = 0

            def read(self, n):
                Boom.n += 1
                if Boom.n > 2:
                    raise OSError(errno.ENOSPC, "No space left on device")
                return b"x" * 1000

        with self.assertRaises(st.StorageError) as cm:
            self.s.ingest("p/t", Boom(), name="big.bin", by="founder", direction="founder")
        self.assertIn("disk is full", str(cm.exception))
        d = self.data_dir / "files" / self.t["id"]
        self.assertEqual(list(d.iterdir()) if d.exists() else [], [])
        self.assertEqual(self.s.list_files("p/t"), [])

    def test_not_enough_space_up_front(self):
        with mock.patch.object(fs, "free_space", return_value=1000):
            with self.assertRaises(st.StorageError) as cm:
                self.s.attach_path("p/t", self.src("a.txt"))
        self.assertIn("not enough disk space", str(cm.exception))

    def test_json_add_attach_and_upload_steps(self):
        a, b = self.src("one.csv", b"a,b\n1,2\n"), self.src("two.png", b"\x89PNG")
        doc = {"project": "P", "title": "J", "key": "p/j", "attach": a, "upload": True, "accept": ".csv",
               "upload_label": "Export", "notify_cmd": "true", "steps": [
                   {"text": "s1", "attach": [b]}, {"text": "s2", "upload": True, "accept": "image/*"}]}
        r = self.s.upsert(doc, actor="agent")
        self.assertEqual(len(r["attached"]), 2)
        files = self.s.list_files("p/j")
        self.assertEqual(sorted((f["name"], f["step"] or 0) for f in files), [("one.csv", 0), ("two.png", 1)])
        t = r["task"]
        self.assertEqual((t["upload"], t["accept"], t["upload_label"], t["notify_cmd"]), (True, ".csv", "Export", "true"))
        self.assertNotIn("attach", t["steps"][0])
        again = self.s.upsert(doc, actor="agent")
        self.assertEqual(again["action"], "unchanged")
        self.assertEqual([a["action"] for a in again["attached"]], ["unchanged", "unchanged"])
        self.assertEqual(len(self.s.list_files("p/j")), 2)
        with self.assertRaises(st.ValidationError):
            self.s.upsert({"project": "P", "title": "x", "steps": [{"text": "s", "attach": "/no/such/file"}]})
        self.assertEqual(len(self.s.list(("open",), None)), 2)  # the failed add created nothing

    def test_migration_from_v1_keeps_tasks(self):
        import sqlite3
        path = str(self.tmp / "old.db")
        c = sqlite3.connect(path)
        for stmt in st._split_sql(st.MIGRATIONS[0]):
            c.execute(stmt)
        c.execute("PRAGMA user_version=1")
        c.execute("INSERT INTO tasks(id,key,project,title,steps,steps_done,note,created_at,updated_at) "
                  "VALUES ('abc234','k','P','Old','[{\"text\":\"s\"}]','[0]','n','2026-01-01T00:00:00.000Z','2026-01-01T00:00:00.000Z')")
        c.execute("INSERT INTO events(task_id,at,actor,kind) VALUES ('abc234','2026-01-01T00:00:00.000Z','a','created')")
        c.commit()
        c.close()
        s2 = st.Store(path)
        t = s2.get("abc234")
        self.assertEqual((t["title"], t["steps"], t["steps_done"], t["note"], t["files"], t["upload"]),
                         ("Old", [{"text": "s"}], [0], "n", [], False))
        self.assertEqual(len(s2.task_events("abc234")), 1)
        c = sqlite3.connect(path)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], len(st.MIGRATIONS))


class CliFileTests(TempDbCase):
    def test_attach_files_detach_roundtrip(self):
        self.ftask("add", "--project", "P", "--title", "T", "--key", "p/t", "--step", "one", "--step", "two", check=0)
        f = self.tmp / "My Cover.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        p = self.ftask("attach", "p/t", str(f), "--step", "2", "--label", "Cover file", "--by", "p/claude", check=0)
        self.assertIn("attached #1 My Cover.pdf", p.stdout)
        out = json.loads(self.ftask("files", "p/t", "--json", check=0).stdout)
        self.assertEqual(len(out), 1)
        e = out[0]
        self.assertEqual((e["name"], e["step"], e["step_index"], e["direction"], e["label"], e["uploaded_by"], e["mime"]),
                         ("My Cover.pdf", 2, 1, "agent", "Cover file", "p/claude", "application/pdf"))
        self.assertTrue(os.path.isabs(e["path"]))
        self.assertEqual(Path(e["path"]).read_bytes(), b"%PDF-1.4 fake")
        human = self.ftask("files", "p/t", check=0).stdout
        self.assertIn(e["path"], human)
        self.assertIn("agent -> human step 2", human)
        self.assertIn(e["path"], self.ftask("show", "p/t", check=0).stdout)
        self.assertEqual(self.ftask("attach", "p/t", str(f), "--step", "2", "--label", "Cover file", check=0)
                         .stdout.splitlines()[0].split(" #")[0], "already attached, unchanged")
        self.ftask("detach", "p/t", "1", "--by", "p/claude", check=0)
        self.assertFalse(os.path.exists(e["path"]))
        self.assertEqual(self.ftask("files", "p/t", check=0).stdout.strip(), "(no files)")
        self.ftask("detach", "p/t", "1", check=3)
        self.ftask("attach", "p/t", str(self.tmp / "missing.bin"), check=2)
        self.ftask("attach", "p/t", str(f), "--step", "9", check=2)
        self.ftask("files", check=2)

    def test_add_flags_and_json_for_upload_steps(self):
        self.ftask("add", "--project", "P", "--title", "T", "--key", "p/t", "--upload", "--accept", ".zip",
                   "--label", "Archive", "--step", "Record", "--step", "Attach it", "--upload",
                   "--accept", "video/*,.csv", "--label", "Screen recording", check=0)
        t = json.loads(self.ftask("show", "p/t", "--json", check=0).stdout)
        self.assertEqual((t["upload"], t["accept"], t["upload_label"]), (True, ".zip", "Archive"))
        self.assertEqual(t["steps"], [{"text": "Record"}, {"text": "Attach it", "upload": True,
                                                            "accept": "video/*,.csv", "label": "Screen recording"}])
        a = self.tmp / "a.txt"
        a.write_text("x")
        doc = {"project": "P", "title": "J", "key": "p/j", "steps": [{"text": "s", "upload": True, "attach": str(a)}]}
        out = self.ftask("add", "--json", "-", input=json.dumps(doc), check=0).stdout
        self.assertIn("attached #1 a.txt", out)
        self.ftask("add", "--project", "P", "--title", "x", "--accept", "nonsense", check=2)

    def test_old_wait_on_still_marks_waiting(self):
        self.ftask("add", "--project", "P", "--title", "T", "--key", "k", check=0)
        self.ftask("wait", "k", "--on", "Apple review", check=0)
        self.assertEqual(json.loads(self.ftask("show", "k", "--json").stdout)["status"], "waiting")
        self.ftask("wait", "k", "--on", "x", "--for", "done", check=2)
        self.ftask("wait", check=2)


class HttpUploadTests(TempDbCase):
    def setUp(self):
        super().setUp()
        self.start()

    def start(self, **kw):
        self.srv = server.make_server(self.db, "127.0.0.1", 0, **kw)
        self.srv.quiet = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)
        self.store = self.srv.app.store
        self.t = self.store.upsert({"project": "P", "title": "T", "key": "k", "steps": [
            {"text": "a"}, {"text": "up", "upload": True}]}, actor="agent")["task"]

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def up(self, body, name="clip.mp4", step=None, ref=None, headers=None, query=None, token=True, host=None,
           ctype="application/octet-stream", length=True):
        q = query if query is not None else "name=" + quote(name) + ("" if step is None else f"&step={step}")
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or f"localhost:{self.port}", "Content-Type": ctype}
        if token:
            h["X-CSRF-Token"] = self.srv.app.token
        if length and isinstance(body, bytes):
            h["Content-Length"] = str(len(body))
        h.update(headers or {})
        c.request("POST", f"/api/tasks/{ref or self.t['id']}/upload?{q}", body=body, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if raw[:1] == b"{" else raw)

    def get(self, path, method="GET", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request(method, path, headers={"Host": f"localhost:{self.port}", **(headers or {})})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, r, raw

    def test_upload_download_roundtrip(self):
        data = os.urandom(200_000)
        s, j = self.up(data, name="My Clip.mp4", step=1)
        self.assertEqual(s, 200, j)
        f = j["file"]
        self.assertEqual((f["name"], f["size"], f["direction"], f["step_index"], f["mime"], f["uploaded_by"]),
                         ("My Clip.mp4", len(data), "founder", 1, "video/mp4", "founder"))
        self.assertEqual(f["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(Path(f["path"]).read_bytes(), data)
        ev = self.store.task_events("k")[-1]
        self.assertEqual((ev["kind"], ev["actor"]), ("upload", "founder"))
        snap = json.loads(self.get("/api/tasks")[2])
        self.assertEqual(snap["tasks"][0]["files"][0]["id"], f["id"])
        s, r, body = self.get(f"/api/files/{f['id']}/download")
        self.assertEqual(s, 200)
        self.assertEqual(body, data)
        self.assertEqual(r.getheader("Content-Type"), "video/mp4")
        self.assertTrue(r.getheader("Content-Disposition").startswith("attachment;"))
        self.assertIn("My%20Clip.mp4", r.getheader("Content-Disposition"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        s, r, body = self.get(f"/api/files/{f['id']}/download", method="HEAD")
        self.assertEqual((s, body, r.getheader("Content-Length")), (200, b"", str(len(data))))

    def test_agent_file_download_has_type_and_attachment(self):
        p = self.tmp / "doc.html"
        p.write_text("<script>alert(1)</script>")
        f = self.store.attach_path("k", str(p))["file"]
        s, r, body = self.get(f"/api/files/{f['id']}/download")
        self.assertEqual((s, r.getheader("Content-Type")), (200, "text/html"))
        self.assertTrue(r.getheader("Content-Disposition").startswith("attachment;"))
        self.assertIn("sandbox", r.getheader("Content-Security-Policy"))

    def test_csrf_origin_host_content_type_enforced(self):
        n0 = len(self.store.list_files("k"))
        self.assertEqual(self.up(b"x", token=False)[0], 403)
        self.assertEqual(self.up(b"x", headers={"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.up(b"x", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.up(b"x", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.up(b"x", host="evil.example")[0], 403)
        self.assertEqual(self.up(b"x", ctype="application/json")[0], 415)
        self.assertEqual(self.up(b"x", ctype="text/plain")[0], 415)
        self.assertEqual(len(self.store.list_files("k")), n0)
        self.assertEqual(self.up(b"x", headers={"Origin": f"http://localhost:{self.port}"})[0], 200)

    def test_traversal_attempts_stay_inside_the_store(self):
        root = self.data_dir / "files"
        for name in ("../../evil.txt", "..%2F..%2Fevil2.txt", "%2e%2e/%2e%2e/evil3.txt", "..\\..\\evil4.txt",
                     "/etc/cron.d/evil5", "a/b/c.txt"):
            s, j = self.up(b"data", query="name=" + name)
            self.assertEqual(s, 200, (name, j))
            real = Path(j["file"]["path"]).resolve()
            self.assertEqual(real.parent, (root / self.t["id"]).resolve(), name)
        found = {p.name for p in self.data_dir.rglob("*") if p.is_file() and p.suffix in (".txt", "")}
        self.assertFalse(any((self.tmp / n).exists() for n in ("evil.txt", "evil2.txt")))
        self.assertFalse((Path("/etc/cron.d/evil5")).exists())
        # task ref and file id traversal
        self.assertEqual(self.up(b"x", ref="..%2F..%2Fx")[0], 404)
        self.assertEqual(self.get("/api/files/..%2Ftasks/download")[0], 404)
        self.assertEqual(self.get("/api/files/9999/download")[0], 404)
        # a database row pointing outside the store must not be served
        import sqlite3
        c = sqlite3.connect(self.db)
        c.execute("UPDATE files SET stored_path='../../../../etc/passwd' WHERE id=1")
        c.commit()
        c.close()
        self.assertEqual(self.get("/api/files/1/download")[0], 404)

    def test_bad_requests(self):
        self.assertEqual(self.up(b"x", query="")[0], 400)                   # no name
        self.assertEqual(self.up(b"x", name="a", step=7)[0], 400)           # step out of range
        self.assertEqual(self.up(b"x", query="name=a&step=zz")[0], 400)
        sock = socket.create_connection(("127.0.0.1", self.port))                  # chunked: refused
        sock.sendall((f"POST /api/tasks/{self.t['id']}/upload?name=a HTTP/1.1\r\nHost: localhost:{self.port}\r\n"
                      f"Content-Type: application/octet-stream\r\nX-CSRF-Token: {self.srv.app.token}\r\n"
                      f"Transfer-Encoding: chunked\r\n\r\n1\r\nx\r\n0\r\n\r\n").encode())
        self.assertIn(b" 400 ", sock.recv(200).split(b"\r\n")[0])
        sock.close()
        self.assertEqual(self.up(b"", name="empty.txt")[0], 200)            # empty files are fine

    def test_size_limit_and_disk_full_give_clear_errors(self):
        self._stop()
        self.addCleanup(lambda: None)
        self.start(max_upload=1000)
        s, j = self.up(b"x" * 1001)
        self.assertEqual(s, 413)
        self.assertIn("too large", j["error"])
        self.assertEqual(self.up(b"x" * 1000)[0], 200)
        cfg = json.loads(self.get("/api/config")[2])
        self.assertEqual(cfg["max_upload"], 1000)
        with mock.patch.object(fs, "free_space", return_value=10):
            s, j = self.up(b"abc", name="d.bin")
        self.assertEqual(s, 507)
        self.assertIn("not enough disk space", j["error"])
        d = self.data_dir / "files" / self.t["id"]
        self.assertFalse([p for p in d.iterdir() if p.name.startswith(".incoming")])

    def test_truncated_upload_leaves_nothing_behind(self):
        n0 = len(self.store.list_files("k"))
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall((f"POST /api/tasks/{self.t['id']}/upload?name=cut.bin HTTP/1.1\r\nHost: localhost:{self.port}\r\n"
                   f"Content-Type: application/octet-stream\r\nX-CSRF-Token: {self.srv.app.token}\r\n"
                   f"Content-Length: 100000\r\n\r\n").encode() + b"x" * 500)
        s.close()
        d = self.data_dir / "files" / self.t["id"]
        from helpers import eventually
        eventually(lambda: not d.exists() or not list(d.iterdir()))
        self.assertEqual(len(self.store.list_files("k")), n0)

    def test_founder_remove_via_api(self):
        f = self.up(b"abc", name="mine.txt")[1]["file"]
        agent = self.store.attach_path("k", str(self._src("agent.txt")))["file"]
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(c.close)

        def post(path, token=True):
            h = {"Host": f"localhost:{self.port}", "Content-Type": "application/json"}
            if token:
                h["X-CSRF-Token"] = self.srv.app.token
            c.request("POST", path, body="{}", headers=h)
            r = c.getresponse()
            return r.status, json.loads(r.read())
        self.assertEqual(post(f"/api/files/{f['id']}/remove", token=False)[0], 403)
        self.assertEqual(post(f"/api/files/{agent['id']}/remove")[0], 400)   # the agent's own files stay
        self.assertEqual(post(f"/api/files/{f['id']}/remove")[0], 200)
        self.assertFalse(os.path.exists(f["path"]))
        self.assertEqual(post(f"/api/files/{f['id']}/remove")[0], 404)
        self.assertEqual(self.store.task_events("k")[-1]["kind"], "detached")

    def _src(self, name):
        p = self.tmp / name
        p.write_bytes(b"agent data")
        return p

    def test_large_upload_is_streamed_not_buffered(self):
        size = 160 * 1024 * 1024
        if os.environ.get("FTASK_BIG_TEST"):
            size = 2 * 1024 ** 3 + 123456  # just over 2 GiB
        self._stop()
        self.start(max_upload=3 * 1024 ** 3)
        chunk = (b"0123456789abcdef" * 65536)  # 1 MiB
        want = hashlib.sha256()
        sent = 0
        parts = []
        while sent < size:
            piece = chunk[: min(len(chunk), size - sent)]
            parts.append(len(piece))
            sent += len(piece)
        pieces = (chunk[:n] for n in parts)
        for n in parts:
            want.update(chunk[:n])
        rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KiB
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=600)
        c.request("POST", f"/api/tasks/{self.t['id']}/upload?name=big.bin", body=pieces, headers={
            "Host": f"localhost:{self.port}", "Content-Type": "application/octet-stream",
            "X-CSRF-Token": self.srv.app.token, "Content-Length": str(size)})
        r = c.getresponse()
        j = json.loads(r.read())
        self.assertEqual(r.status, 200, j)
        rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.assertEqual(j["file"]["size"], size)
        self.assertEqual(j["file"]["sha256"], want.hexdigest())
        self.assertEqual(os.path.getsize(j["file"]["path"]), size)
        self.assertLess(rss1 - rss0, 60 * 1024, "server buffered the upload in memory")  # KiB


if __name__ == "__main__":
    unittest.main()
