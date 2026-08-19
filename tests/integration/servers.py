"""Local stand-ins for the two external services billwatch talks to.

* LegiScanServer — an HTTP server that answers LegiScan Pull API requests from a
  fixture directory (same layout FixtureClient uses), validating the API key,
  and optionally failing the first N requests to exercise retry logic.
* SmtpServer     — a tiny threaded SMTP server (EHLO/AUTH/MAIL/RCPT/DATA/QUIT)
  that records delivered messages so tests can inspect envelope + content.

Both bind to 127.0.0.1 on an ephemeral port.
"""

from __future__ import annotations

import base64
import json
import socketserver
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from billwatch.legiscan import search_slug

# --------------------------------------------------------------------------- #
# LegiScan
# --------------------------------------------------------------------------- #


@dataclass
class LegiScanServer:
    fixture_dir: Path
    api_key: str = "test-key"
    fail_first: int = 0  # respond 503 to this many requests before behaving
    requests: list[dict] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def _payload_for(self, params: dict[str, str]) -> tuple[int, dict]:
        op = params.get("op", "")
        state = params.get("state", "MD")
        d = self.fixture_dir
        if op == "getSessionList":
            path = d / f"sessions_{state}.json"
        elif op == "getMasterListRaw":
            path = d / f"masterlist_{state}.json"
        elif op == "getBill":
            path = d / f"bill_{params.get('id')}.json"
        elif op == "getSearchRaw":
            path = d / f"search_{state}_{search_slug(params.get('query', ''))}.json"
            if not path.is_file():
                return 200, {"status": "OK", "searchresult": {"summary": {}, "results": []}}
        else:
            return 200, {"status": "ERROR", "alert": {"message": f"Unknown operation {op}"}}
        if not path.is_file():
            return 200, {"status": "ERROR", "alert": {"message": "Unknown bill id"}}
        return 200, json.loads(path.read_text(encoding="utf-8"))

    def start(self) -> LegiScanServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_GET(self):
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                outer.requests.append(q)
                if outer.fail_first > 0:
                    outer.fail_first -= 1
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"upstream unavailable")
                    return
                if q.get("key") != outer.api_key:
                    body = {"status": "ERROR", "alert": {"message": "Invalid API key"}}
                    code = 200
                else:
                    code, body = outer._payload_for(q)
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.05), daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()


# --------------------------------------------------------------------------- #
# SMTP
# --------------------------------------------------------------------------- #


@dataclass
class DeliveredMessage:
    mail_from: str
    rcpt_to: list[str]
    data: bytes
    auth: tuple[str, str] | None


class SmtpServer:
    def __init__(self, *, require_auth: bool = False, reject_rcpt: set[str] | None = None):
        self.messages: list[DeliveredMessage] = []
        self.require_auth = require_auth
        self.reject_rcpt = reject_rcpt or set()
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    def start(self) -> SmtpServer:
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def _send(self, line: str) -> None:
                self.wfile.write((line + "\r\n").encode())

            def handle(self):
                self._send("220 billwatch-test-smtp ESMTP")
                mail_from, rcpts, auth = "", [], None
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode(errors="replace").rstrip("\r\n")
                    verb, _, arg = line.partition(" ")
                    verb = verb.upper()
                    if verb in ("EHLO", "HELO"):
                        self._send("250-billwatch-test-smtp")
                        self._send("250 AUTH PLAIN LOGIN")
                    elif verb == "AUTH":
                        mech, _, initial = arg.partition(" ")
                        if mech.upper() == "PLAIN":
                            if not initial:
                                self._send("334 ")
                                initial = self.rfile.readline().decode().strip()
                            parts = base64.b64decode(initial).split(b"\0")
                            auth = (parts[-2].decode(), parts[-1].decode())
                        elif mech.upper() == "LOGIN":
                            self._send("334 VXNlcm5hbWU6")
                            u = base64.b64decode(self.rfile.readline().strip()).decode()
                            self._send("334 UGFzc3dvcmQ6")
                            p = base64.b64decode(self.rfile.readline().strip()).decode()
                            auth = (u, p)
                        self._send("235 2.7.0 Authentication successful")
                    elif verb == "MAIL":
                        if outer.require_auth and auth is None:
                            self._send("530 5.7.0 Authentication required")
                            continue
                        mail_from = arg.split(":", 1)[1].strip().strip("<>")
                        rcpts = []
                        self._send("250 OK")
                    elif verb == "RCPT":
                        addr = arg.split(":", 1)[1].strip().strip("<>")
                        if addr in outer.reject_rcpt:
                            self._send("550 5.1.1 No such user")
                        else:
                            rcpts.append(addr)
                            self._send("250 OK")
                    elif verb == "DATA":
                        self._send("354 End data with <CR><LF>.<CR><LF>")
                        buf = []
                        while True:
                            l2 = self.rfile.readline()
                            if not l2 or l2 in (b".\r\n", b".\n"):
                                break
                            buf.append(l2[1:] if l2.startswith(b"..") else l2)
                        outer.messages.append(
                            DeliveredMessage(mail_from, list(rcpts), b"".join(buf), auth)
                        )
                        self._send("250 OK queued")
                    elif verb == "QUIT":
                        self._send("221 Bye")
                        return
                    elif verb in ("RSET", "NOOP"):
                        self._send("250 OK")
                    else:
                        self._send("502 Command not implemented")

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.05), daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()
