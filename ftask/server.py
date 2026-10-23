"""Web UI + JSON API (http.server, standard library only).

Security model (it listens on localhost, there is no login):
  * binds 127.0.0.1 by default
  * Host header must be a loopback name (or the bind host / --allow-host): blocks DNS rebinding
  * every POST needs: JSON content-type, a CSRF token header, and a same-origin Origin
    (when the browser sends one) / Sec-Fetch-Site
  * strict CSP, no CORS headers, no external resources
  * file uploads are POSTs too: same Host / Origin / CSRF-token checks, body must be application/octet-stream
    (a custom header + non-simple content type can never be sent cross-origin without a CORS preflight, which
    this server never approves); the body is streamed to disk, never held in memory
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import files as fs
from . import store as st
from .hooks import HUMAN

UI_PATH = Path(__file__).with_name("ui.html")
MAX_BODY = 1_000_000
DEFAULT_MAX_UPLOAD = 10 * 1024 ** 3  # 10 GiB; override with FTASK_MAX_UPLOAD_BYTES or `ftask serve --max-upload`
DRAIN_LIMIT = 64 * 1024 * 1024
LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}
ACTOR = HUMAN


class App:
    def __init__(self, db_path, host="127.0.0.1", allow_hosts=(), max_upload=None):
        self.store = st.Store(db_path)
        if max_upload is None:
            try:
                max_upload = int(os.environ.get("FTASK_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD))
            except ValueError:
                max_upload = DEFAULT_MAX_UPLOAD
        self.max_upload = max_upload
        self.token = secrets.token_urlsafe(32)
        self.ui = UI_PATH.read_text(encoding="utf-8")
        self.wildcard = host in ("0.0.0.0", "::", "")
        self.allowed = {h.lower() for h in LOOPBACK} | {h.lower() for h in allow_hosts}
        if host and not self.wildcard:
            self.allowed.add(host.lower().strip("[]"))


def _host_name(hostport: str) -> str:
    h = hostport.strip().lower()
    if h.startswith("["):  # [::1]:4747
        return h.split("]")[0].lstrip("[")
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


class _CountedReader:
    """Wraps rfile so the handler always knows how much of the request body is still unread."""

    def __init__(self, handler):
        self.h = handler

    def read(self, n):
        data = self.h.rfile.read(n)
        self.h._unread = max(0, self.h._unread - len(data))
        return data


class Handler(BaseHTTPRequestHandler):
    server_version = "ftask"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 120  # seconds of silence on the socket before a stalled client is dropped
    _unread = 0

    # ---- plumbing ------------------------------------------------------

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        if getattr(self.server, "quiet", False):
            return
        if self.path.startswith("/api/version") and self.command == "GET":
            return  # the 5s poll would drown the journal
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body: bytes, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status, msg, code=None):
        if self.command == "POST":
            self._drain()
            self.close_connection = True  # request body may be unread
        try:
            self._json(status, {"error": msg, "code": code or "error"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True  # the client already went away

    def _drain(self):
        """Swallow a modest unread request body so the browser can still read our error reply."""
        n = self._unread
        if 0 < n <= DRAIN_LIMIT:
            try:
                while n > 0:
                    chunk = self.rfile.read(min(n, fs.CHUNK))
                    if not chunk:
                        break
                    n -= len(chunk)
            except OSError:
                pass
        self._unread = 0

    def _host_ok(self) -> bool:
        if self.app.wildcard:
            return True
        host = self.headers.get("Host", "")
        return bool(host) and _host_name(host) in self.app.allowed

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return False
            if parsed.netloc.lower() != self.headers.get("Host", "").lower():
                return False
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs not in ("same-origin", "none"):
            return False
        return True

    # ---- routing -------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            if not self._host_ok():
                return self._error(403, "bad Host header", "host")
            u = urlsplit(self.path)
            path = u.path
            query = parse_qs(u.query)
            if method == "POST":
                try:
                    self._unread = max(0, int(self.headers.get("Content-Length", "0")))
                except ValueError:
                    self._unread = 0
                if not self._origin_ok():
                    return self._error(403, "cross-origin request refused", "origin")
                upload = re.fullmatch(r"/api/tasks/([^/]+)/upload", path)
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != ("application/octet-stream" if upload else "application/json"):
                    return self._error(415, "Content-Type must be " + (
                        "application/octet-stream" if upload else "application/json"), "content-type")
                token = self.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(token.encode(), self.app.token.encode()):
                    return self._error(403, "missing or invalid CSRF token", "csrf")
                if upload:
                    return self._upload(upload.group(1), query)
                return self._post(path)
            return self._get(path, query)
        except st.FtaskError as e:
            status = {st.ValidationError: 400, st.NotFound: 404, st.Ambiguous: 409, st.DatabaseBusy: 503,
                      st.StorageError: 507, st.TooLarge: 413}.get(type(e), 400)
            self._error(status, str(e), type(e).__name__)
        except sqlite3.OperationalError as e:
            self._error(503, f"database error: {e}", "db")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            traceback.print_exc()
            try:
                self._error(500, "internal error", "internal")
            except Exception:
                pass

    def _get(self, path, query):
        s = self.app.store
        if path in ("/", "/index.html"):
            nonce = secrets.token_urlsafe(16)
            html = self.app.ui.replace("__CSRF__", self.app.token).replace("__NONCE__", nonce)
            csp = (
                "default-src 'none'; script-src 'nonce-%s'; style-src 'nonce-%s'; connect-src 'self'; "
                "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % (nonce, nonce)
            )
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": csp})
        if path == "/api/version":
            return self._json(200, {"version": s.version()})
        if path == "/api/csrf":
            return self._json(200, {"token": self.app.token})
        if path == "/api/tasks":
            return self._json(200, s.snapshot())
        if path == "/api/events":
            since = query.get("since", [None])[0]
            after = query.get("after", [None])[0]
            return self._json(200, {"events": s.events(
                since=st.parse_since(since) if since else None,
                after_seq=int(after) if after else None, limit=500)})
        m = re.fullmatch(r"/api/tasks/([^/]+)", path)
        if m:
            t = s.get(m.group(1))
            t["events"] = s.task_events(t["id"])
            return self._json(200, t)
        if path == "/api/config":
            try:
                free = fs.free_space(fs.files_root(s.path))
            except OSError:
                free = None
            return self._json(200, {"max_upload": self.app.max_upload, "free_bytes": free,
                                    "reserve_bytes": fs.SPACE_RESERVE})
        m = re.fullmatch(r"/api/files/(\d+)/download", path)
        if m:
            return self._download(int(m.group(1)))
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._error(404, "not found", "NotFound")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise st.ValidationError("Content-Length required")
        if n > MAX_BODY:
            raise st.ValidationError("request body too large")
        raw = self.rfile.read(n)
        self._unread = 0
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise st.ValidationError("body is not valid JSON")
        if not isinstance(data, dict):
            raise st.ValidationError("body must be a JSON object")
        return data

    def _post(self, path):
        m = re.fullmatch(r"/api/files/(\d+)/remove", path)
        if m:
            self._body()
            f = self.app.store.remove_file(int(m.group(1)), actor=ACTOR, only_direction=HUMAN)
            return self._json(200, {"removed": f, "version": self.app.store.version()})
        m = re.fullmatch(r"/api/tasks/([^/]+)/(step|note|status)", path)
        if not m:
            self._body()  # drain
            return self._error(404, "not found", "NotFound")
        ref, action = m.groups()
        data = self._body()
        s = self.app.store
        if action == "step":
            r = s.set_step(ref, data.get("index"), data.get("done"), actor=ACTOR)
        elif action == "note":
            note = data.get("note")
            if not isinstance(note, str):
                raise st.ValidationError("note must be a string")
            r = s.set_note(ref, note, actor=ACTOR)
        else:
            status = data.get("status")
            heard = s.listeners(ref, "done" if status == "done" else status) if status in ("done", "cancelled") else None
            r = s.set_status(ref, status, actor=ACTOR, waiting_on=data.get("waiting_on"))
            if heard is not None:
                # only claim the agent was told when something is actually listening for this event
                r["notified"] = bool(r["changed"] and heard["any"])
                r["listeners"] = heard
        r["version"] = s.version()
        self._json(200, r)

    def _upload(self, ref, query):
        s = self.app.store
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or not raw_len.isdigit():
            raise st.ValidationError("Content-Length required (chunked uploads are not supported)")
        n = int(raw_len)
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise st.ValidationError("chunked uploads are not supported")
        name = (query.get("name", [""])[0] or "").strip()
        if not name:
            raise st.ValidationError("name is required")
        step = query.get("step", [None])[0]
        if step not in (None, ""):
            if not step.lstrip("-").isdigit():
                raise st.ValidationError("step must be a number")
            step = int(step)
        else:
            step = None
        label = query.get("label", [""])[0]
        r = s.ingest(ref, _CountedReader(self), name=name, size=n, step_index=step, label=label,
                     client_mime=query.get("type", [None])[0], by=ACTOR, direction=HUMAN,
                     max_size=self.app.max_upload)
        self._unread = 0
        self._json(200, {"file": r["file"], "version": s.version()})

    def _download(self, fid):
        d, f, size = self.app.store.open_file(fid)
        with f:
            self.send_response(200)
            self.send_header("Content-Type", d["mime"])
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", fs.content_disposition(d["name"]))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
            self.end_headers()
            if self.command == "HEAD":
                return
            left = size
            while left > 0:
                chunk = f.read(min(fs.CHUNK, left))
                if not chunk:
                    self.close_connection = True  # file shrank under us: cut the connection, do not pad
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def make_server(db_path, host="127.0.0.1", port=4747, allow_hosts=(), max_upload=None) -> Server:
    if ":" in host:
        Server.address_family = __import__("socket").AF_INET6
    srv = Server((host, port), Handler)
    srv.app = App(db_path, host, allow_hosts, max_upload)  # type: ignore[attr-defined]
    return srv


def serve(db_path, host="127.0.0.1", port=4747, allow_hosts=(), max_upload=None, on_ready=None) -> int:
    try:
        srv = make_server(db_path, host, port, allow_hosts, max_upload)
    except OSError as e:
        print(f"ftask: cannot listen on {host}:{port}: {e}", file=sys.stderr)
        return 1
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"ftask: WARNING listening on {host}; there is no login, anyone who can reach it can edit tasks",
              file=sys.stderr)
    print(f"ftask listening on http://{host}:{srv.server_address[1]}  db={db_path}", flush=True)
    if on_ready:
        on_ready(f"http://{host if ':' not in host else '[' + host + ']'}:{srv.server_address[1]}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
