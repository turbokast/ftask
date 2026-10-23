import http.client
import json
import re
import threading
import unittest

from helpers import TempDbCase
from ftask import server


class HttpTests(TempDbCase):
    def setUp(self):
        super().setUp()
        self.srv = server.make_server(self.db, "127.0.0.1", 0)
        self.srv.quiet = True
        self.port = self.srv.server_address[1]
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()
        self.addCleanup(self._stop)
        self.store = self.srv.app.store
        self.t = self.store.upsert({"project": "Shop", "title": "Do it", "key": "k", "steps": [
            {"text": "a", "value": "v"}, {"text": "b"}]}, actor="agent")["task"]

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"localhost:{self.port}"}
        h.update(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, r, raw

    def post(self, path, body, token=True, **extra):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-CSRF-Token"] = self.srv.app.token
        h.update(extra)
        return self.req("POST", path, body, h)

    # ---- reads ----
    def test_index_html(self):
        s, r, raw = self.req("GET", "/")
        self.assertEqual(s, 200)
        html = raw.decode()
        self.assertIn("ftask", html)
        self.assertIn(self.srv.app.token, html)
        self.assertNotIn("__CSRF__", html)
        self.assertNotIn("__NONCE__", html)
        csp = r.getheader("Content-Security-Policy")
        nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
        self.assertIn(f'nonce="{nonce}"', html)
        self.assertNotRegex(html, r"(src|href)=[\"']https?://")  # no external resources

    def test_api_version_and_tasks(self):
        s, _, raw = self.req("GET", "/api/version")
        v = json.loads(raw)["version"]
        self.assertEqual(s, 200)
        self.assertEqual(v, self.store.version())
        s, _, raw = self.req("GET", "/api/tasks")
        d = json.loads(raw)
        self.assertEqual(d["tasks"][0]["id"], self.t["id"])
        self.assertEqual(d["counts"]["Shop"]["open"], 1)
        s, _, raw = self.req("GET", f"/api/tasks/{self.t['id']}")
        self.assertEqual(json.loads(raw)["events"][0]["kind"], "created")
        self.assertEqual(self.req("GET", "/api/tasks/nope")[0], 404)
        self.assertEqual(self.req("GET", "/nothing")[0], 404)

    # ---- writes ----
    def test_step_note_status_flow_and_version_bump(self):
        v0 = self.store.version()
        s, _, raw = self.post(f"/api/tasks/{self.t['id']}/step", {"index": 1, "done": True})
        self.assertEqual(s, 200, raw)
        self.assertEqual(json.loads(raw)["task"]["steps_done"], [1])
        s, _, raw = self.post(f"/api/tasks/{self.t['id']}/note", {"note": "line1\nline2"})
        self.assertEqual(s, 200, raw)
        s, _, raw = self.post(f"/api/tasks/{self.t['id']}/status", {"status": "done"})
        self.assertEqual(json.loads(raw)["task"]["status"], "done")
        self.assertGreater(json.loads(self.req("GET", "/api/version")[2])["version"], v0)
        evs = self.store.task_events(self.t["id"])
        self.assertEqual([(e["actor"], e["kind"]) for e in evs],
                         [("agent", "created"), ("founder", "step"), ("founder", "note"), ("founder", "done")])
        s, _, _ = self.post(f"/api/tasks/{self.t['id']}/status", {"status": "open"})
        self.assertEqual(s, 200)

    def test_validation_errors(self):
        self.assertEqual(self.post(f"/api/tasks/{self.t['id']}/step", {"index": 9, "done": True})[0], 400)
        self.assertEqual(self.post(f"/api/tasks/{self.t['id']}/status", {"status": "bogus"})[0], 400)
        self.assertEqual(self.post(f"/api/tasks/{self.t['id']}/note", {"note": 5})[0], 400)
        self.assertEqual(self.post("/api/tasks/zzzzzz/note", {"note": "x"})[0], 404)
        self.assertEqual(self.post(f"/api/tasks/{self.t['id']}/note", "not json")[0], 400)
        self.assertEqual(self.post("/api/tasks", {"project": "x", "title": "y"})[0], 404)  # no create via HTTP

    # ---- CSRF / origin / host ----
    def test_csrf_rejected(self):
        p = f"/api/tasks/{self.t['id']}/note"
        s, _, _ = self.post(p, {"note": "x"}, token=False)
        self.assertEqual(s, 403)
        s, _, _ = self.post(p, {"note": "x"}, **{"X-CSRF-Token": "wrong"})
        self.assertEqual(s, 403)
        self.assertEqual(self.store.get(self.t["id"])["note"], "")

    def test_cross_origin_rejected(self):
        p = f"/api/tasks/{self.t['id']}/note"
        s, _, _ = self.post(p, {"note": "x"}, Origin="http://evil.example")
        self.assertEqual(s, 403)
        s, _, _ = self.post(p, {"note": "x"}, **{"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(s, 403)
        self.assertEqual(self.store.get(self.t["id"])["note"], "")
        s, _, _ = self.post(p, {"note": "ok"}, Origin=f"http://localhost:{self.port}")
        self.assertEqual(s, 200)

    def test_form_content_type_rejected(self):
        h = {"Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": self.srv.app.token}
        s, _, _ = self.req("POST", f"/api/tasks/{self.t['id']}/note", "note=x", h)
        self.assertEqual(s, 415)
        h = {"Content-Type": "text/plain", "X-CSRF-Token": self.srv.app.token}
        self.assertEqual(self.req("POST", f"/api/tasks/{self.t['id']}/note", '{"note":"x"}', h)[0], 415)

    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(self.req("GET", "/api/tasks", host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/", host="evil.example:80")[0], 403)
        self.assertEqual(self.req("GET", "/api/version", host=f"127.0.0.1:{self.port}")[0], 200)

    def test_keepalive_survives_rejected_post(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", f"/api/tasks/{self.t['id']}/note", body='{"note":"x"}',
                  headers={"Content-Type": "application/json"})
        r = c.getresponse(); r.read()
        self.assertEqual(r.status, 403)
        c.close()

    def test_csrf_token_endpoint(self):
        s, _, raw = self.req("GET", "/api/csrf")
        self.assertEqual(json.loads(raw)["token"], self.srv.app.token)
        self.assertEqual(self.req("GET", "/api/csrf", host="evil.example")[0], 403)


if __name__ == "__main__":
    unittest.main()
