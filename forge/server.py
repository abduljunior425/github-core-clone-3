"""The HTTP server: routing, auth, and response framing.

One ThreadingHTTPServer, HTTP/1.1 with keep-alive, a Content-Length on
every reply. Routing is by path prefix:

- GET  /healthz                     -> 200 {"ok": true}, no auth
- GET  /info/refs                   -> git advertisement (no auth)
- POST /<owner>/<repo>.git/git-upload-pack   -> upload-pack v2/v0
- POST /<owner>/<repo>.git/git-receive-pack -> receive-pack v0
- /api/v3/...                       -> REST (Bearer token required)
- POST /api/graphql                 -> GraphQL (Bearer token required)
- POST /mcp, DELETE /mcp           -> MCP (Bearer token required on POST)

Any non-empty Bearer token is accepted as the viewer; a missing token on
a protected route answers 401 with a JSON body. Unknown routes answer a
JSON 404, never HTML. Every reply carries a non-empty X-GitHub-Request-Id.
"""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import gitwire, graphql, mcp, rest
from .store import ForgeStore

_JSON = "application/json"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Forge/1.0.0"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, *args):  # keep the trial log clean
        pass

    def setup(self):
        super().setup()
        # Kill Nagle on the accepted socket: without TCP_NODELAY the
        # headers (one small write) and the body (a second small write)
        # stall behind the client's delayed ACK (~20 ms per keep-alive
        # request). The benchmark measures exactly that pattern.
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def _send(self, status: int, content_type: str, body: bytes,
              extra_headers=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-GitHub-Request-Id",
                             self.server.store.next_request_id())
            for key, value in (extra_headers or {}).items():
                if value is not None:
                    self.send_header(key, value)
            # One single write for headers + body: end_headers() would
            # flush the buffered headers as their own small segment, and
            # the body would follow as a second one. Send the whole reply
            # at once instead, then flush explicitly.
            head = b"".join(self._headers_buffer)
            self._headers_buffer = []
            payload = body if (self.command != "HEAD" and body) else b""
            self.wfile.write(head + b"\r\n" + payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, status: int, payload, extra_headers=None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self._send(status, _JSON, body, extra_headers)

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError:
            return b""
        if size <= 0:
            return b""
        return self.rfile.read(size)

    def _bearer(self):
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and auth[7:].strip():
            return auth[7:].strip()
        return None

    def _require_auth(self) -> bool:
        """True when the request carries a Bearer token; else a 401."""
        if self._bearer() is None:
            self._send_json(401, {"message": "Requires authentication"})
            return False
        return True

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_PUT(self):
        self._route()

    def do_PATCH(self):
        self._route()

    def do_DELETE(self):
        self._route()

    def _route(self):
        try:
            self._dispatch()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as err:  # a handler failure is a 500, never a crash
            try:
                self._send_json(500, {"message": "Internal error: %s" % err})
            except Exception:
                pass

    def _dispatch(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        method = self.command
        store = self.server.store
        host = self.headers.get("Host") or "127.0.0.1"
        # Read the body up front, before any routing decision: a reply sent
        # with the body still unread (e.g. a 401) would leave those bytes
        # in the stream, and the client's next request on the keep-alive
        # connection would be parsed from the middle of them.
        raw_body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else b""

        if path == "/healthz" and method == "GET":
            self._send_json(200, {"ok": True})
            return

        # -- git smart HTTP: /<owner>/<repo>.git/<service> -----------------
        if path.endswith(".git/info/refs") and method == "GET":
            owner, repo = _git_repo(path[:-len("/info/refs")])
            service = query.get("service", [None])[0]
            headers = {"git-protocol": self.headers.get("Git-Protocol") or ""}
            status, ctype, body = gitwire.handle_info_refs(
                store, owner, repo, service, headers)
            self._send(status, ctype, body)
            return
        if path.endswith(".git/git-upload-pack") and method == "POST":
            owner, repo = _git_repo(path[:-len("/git-upload-pack")])
            proto = (self.headers.get("Git-Protocol") or "").strip()
            if proto == "version=2":
                status, ctype, out = gitwire.handle_upload_pack_v2(
                    store, owner, repo, raw_body)
            else:
                status, ctype, out = gitwire.handle_upload_pack_v0(
                    store, owner, repo, raw_body)
            self._send(status, ctype, out)
            return
        if path.endswith(".git/git-receive-pack") and method == "POST":
            owner, repo = _git_repo(path[:-len("/git-receive-pack")])
            status, ctype, out = gitwire.handle_receive_pack(
                store, owner, repo, raw_body)
            self._send(status, ctype, out)
            return

        # -- REST v3 -------------------------------------------------------
        if path == "/api/v3" or path.startswith("/api/v3/"):
            if not self._require_auth():
                return
            segments = [s for s in path.split("/") if s][2:]
            if method in ("POST", "PUT", "PATCH"):
                if raw_body:
                    try:
                        body = json.loads(raw_body.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        self._send_json(400, {"message": "Invalid JSON body"})
                        return
                else:
                    body = None
            else:
                body = None
            status, payload, extra = rest.handle_rest(
                store, method, segments, query, body, host, self.path)
            self._send_json(status, payload, extra)
            return

        # -- GraphQL -------------------------------------------------------
        if path == "/api/graphql" and method == "POST":
            if not self._require_auth():
                return
            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else None
            except (ValueError, UnicodeDecodeError):
                self._send_json(400, {"errors": [{"message": "Invalid JSON body"}]})
                return
            status, payload = graphql.handle_graphql(store, body)
            self._send_json(status, payload)
            return

        # -- MCP -----------------------------------------------------------
        if path == "/mcp":
            if method == "DELETE":
                self._send_json(200, {})
                return
            if method == "POST":
                if not self._require_auth():
                    return
                status, payload = mcp.handle_mcp(store, raw_body)
                self._send_json(status, payload)
                return
            self._send_json(404, {"message": "Not Found"})
            return

        self._send_json(404, {"message": "Not Found"})


def _git_repo(prefix: str):
    """'/<owner>/<repo>.git' -> (owner, repo)."""
    parts = [s for s in prefix.split("/") if s]
    if len(parts) != 2 or not parts[1].endswith(".git"):
        return "", ""
    return parts[0], parts[1][:-len(".git")]


def make_server(store: ForgeStore, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.store = store
    server.daemon_threads = True
    server.request_queue_size = 128
    return server


def serve(store: ForgeStore, host: str, port: int):
    server = make_server(store, host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
