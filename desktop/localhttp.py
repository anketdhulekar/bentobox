"""The local HTTP server, on 127.0.0.1 only: hook events, the agent list for the CLI, and a nudge from the CLI.

Only programs on this Mac talk to it: the hook script and bentobox.py. A browser always sends an Origin header with a
request a web page makes, including `Origin: null` from a sandboxed frame or a file, so any request with an Origin is
refused: no website can read the agents or send events. The Host check stops DNS rebinding.
"""
import http.server
import json
import socketserver

MAX_BODY = 8192


def make_http(zoo, port, on_tap, where_of, status=None, reload=None):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "bentobox"

        def log_message(self, *args):
            pass

        def _caller_ok(self):
            return self.headers.get("Host") in self.server.allowed_hosts and self.headers.get("Origin") is None

        def _reply(self, code, body=b"", ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _length(self):
            """The body's length, or None if it's missing a valid one, negative or too big."""
            raw = self.headers.get("Content-Length")
            try:
                n = int(raw) if raw is not None else 0
            except ValueError:
                return None
            return n if 0 <= n <= MAX_BODY else None

        def _json_body(self, n):
            """A JSON object sent as application/json, or None."""
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return None
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return None
            return body if isinstance(body, dict) else None

        def do_GET(self):
            if not self._caller_ok():
                return self._reply(403)
            if self.path == "/state":
                msg = zoo.public(where_of)
                if status:
                    msg.update(status())
                return self._reply(200, json.dumps(msg).encode())
            self._reply(404)

        def do_POST(self):
            if not self._caller_ok():
                return self._reply(403)
            n = self._length()
            if n is None:
                return self._reply(400)
            if self.path == "/reload":
                # It takes nothing from the request: the app only reads again what the CLI saved. So a key or a lock
                # code never travels here.
                if reload:
                    reload()
                return self._reply(204)
            if self.path not in ("/hook", "/focus"):
                return self._reply(404)
            body = self._json_body(n)
            if body is None:
                return self._reply(400)
            if self.path == "/hook":
                zoo.on_hook(body)
                return self._reply(204)
            if isinstance(body.get("id"), str):
                ok = on_tap(body["id"], "local")
                return self._reply(200 if ok else 404, json.dumps({"ok": bool(ok)}).encode())
            self._reply(400)

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def server_bind(self):  # skips HTTPServer's host-name lookup, which can take 5 s on macOS
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "127.0.0.1", self.server_address[1]
            self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}

    return Server(("127.0.0.1", port), Handler)
