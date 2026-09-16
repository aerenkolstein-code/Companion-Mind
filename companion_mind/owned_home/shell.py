"""Loopback-only, unprivileged P2-S1 local Web shell.

python -m companion_mind.owned_home.shell --store DIR --port 8765
Open the printed loopback URL. Synthetic fixture/grant setup belongs to the
trusted process; browser ingress can only submit/recover its fixed owner scope.
No production provider, credentials, filesystem browsing or connector routes.
"""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys

from companion_mind.journal import JournalError
from .contracts import (VERSION, AuthorityFixture, Grant, HomeError, Scope, Turn,
                        encode, exact_keys)
from .runtime import OwnedRuntime
from .testport import validate_operation

SCOPE = Scope("synthetic-home", "synthetic-owner")
FIXTURE = AuthorityFixture("local-demo", "v1", SCOPE.universe_id, SCOPE.access_subject_id,
                           "The local synthetic archive contains one evidence item.")
GRANT = Grant(SCOPE.universe_id, SCOPE.access_subject_id, FIXTURE.source_id, FIXTURE.version)

HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Companion-Mind · Local slice</title>
<body><main><h1>Companion-Mind</h1><p>Local synthetic workspace</p>
<form id="form" autocomplete="off"><label for="message">Your message</label><br>
<textarea id="message" rows="5" cols="64" maxlength="8000" autocomplete="off" required></textarea><br>
<button id="submit">Send</button><button id="resume" type="button" hidden>Resume pending turn</button>
<button id="next" type="button" hidden>New turn</button></form>
<p id="status" role="status"></p><pre id="reply"></pre></main><script src="/app.js"></script></body></html>"""

JS = """'use strict';
const $ = id => document.getElementById(id);
const key = 'owned-home-v1:' + location.origin;
let pending = null, draft = null, state = null, busy = false;
function saveHandle(id) {
  // Only locally generated UUID identity crosses the persistence boundary.
  // Never spread a turn/server response or serialize DOM/message content here.
  const handle = {request_id:id};
  localStorage.setItem(key, JSON.stringify(handle));
  pending = handle;
}
function controls() {
  $('submit').disabled = busy || (pending !== null && state !== 'NOT_FOUND');
  $('message').disabled = $('submit').disabled;
  $('resume').disabled = busy; $('next').disabled = busy;
  $('resume').hidden = !['AWAIT_EXPLICIT_RESUME','UNKNOWN'].includes(state);
  $('resume').textContent = state === 'UNKNOWN' ? 'Check pending turn' : 'Resume pending turn';
  $('next').hidden = pending === null || ['AWAIT_EXPLICIT_RESUME','UNKNOWN'].includes(state);
}
function render(r) {
  state = r.status;
  $('status').textContent = state === 'NOT_FOUND'
    ? 'No durable turn found. Enter your message to retry.' : (r.stop_reason || state);
  $('reply').textContent = r.visible_reply || '';
  controls();
}
async function call(body) {
  const response = await fetch('/v1/turn', {method:'POST',
    headers:{'Content-Type':'application/json','X-Owned-Home':'1'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!data.ok) throw new Error(data.error);
  render(data.result); return data.result;
}
function report(error) { $('status').textContent = error.message; }
$('form').addEventListener('submit', async e => {
  e.preventDefault(); if (busy) return;
  busy = true; controls();
  try {
    // Resolve uncertainty before a retry. Observation never invokes cognition.
    if (pending) {
      const current = await call({contract_version:'owned-home/1',op:'observe',request_id:pending.request_id});
      if (current.status !== 'NOT_FOUND') { draft = null; return; }
    } else { saveHandle(crypto.randomUUID()); }
    if (!draft) {
      const id = pending.request_id;
      draft = {contract_version:'owned-home/1',request_id:id,session_id:id,
        turn_id:id,turn_no:1,universe_id:'synthetic-home',access_subject_id:'synthetic-owner',
        source_id:'local-demo',source_version:'v1',text:$('message').value,
        observed_at:new Date().toISOString(),budget_bytes:16384};
      $('message').value = '';
    }
    await call({contract_version:'owned-home/1',op:'turn',turn:draft});
    draft = null;
  } catch (error) {
    state = 'UNKNOWN'; report(error);
  } finally { draft = null; busy = false; controls(); }
});
$('resume').addEventListener('click', async () => {
  if (busy || !pending || !['AWAIT_EXPLICIT_RESUME','UNKNOWN'].includes(state)) return;
  const op = state === 'UNKNOWN' ? 'observe' : 'resume';
  busy = true; controls();
  try { await call({contract_version:'owned-home/1',op:op,request_id:pending.request_id}); }
  catch (error) { state = 'UNKNOWN'; report(error); }
  finally { busy = false; controls(); }
});
$('next').addEventListener('click', () => {
  if (busy) return;
  localStorage.removeItem(key); pending = null; draft = null; state = null;
  $('message').value = ''; $('reply').textContent = ''; $('status').textContent = ''; controls();
});
addEventListener('pagehide', () => { draft = null; $('message').value = ''; });
try {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(key)); } catch (_) {}
  if (saved && typeof saved.request_id === 'string' &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(saved.request_id)) {
    // Replace legacy full-turn storage with the allowlisted identity only.
    saveHandle(saved.request_id);
    busy = true; controls();
    call({contract_version:'owned-home/1',op:'observe',request_id:pending.request_id})
      .catch(error => { state = 'UNKNOWN'; report(error); }).finally(() => { busy = false; controls(); });
  } else { localStorage.removeItem(key); controls(); }
} catch (_) {
  busy = true; controls(); $('status').textContent = 'Recovery storage unavailable.';
}
"""


def make_server(directory, *, host="127.0.0.1", port=0):
    if host != "127.0.0.1":
        raise HomeError("LOOPBACK_ONLY")
    if type(port) is not int or not 0 <= port <= 65535:
        raise HomeError("INVALID_PORT")
    directory = Path(directory)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Request/user content is not a second access-log RAW.

        def _origin(self):
            authority = "127.0.0.1:" + str(self.server.server_port)
            if self.headers.get_all("Host") != [authority]:
                return False
            origins = self.headers.get_all("Origin")
            return (origins is None or origins == ["http://" + authority]) and self.headers.get("Sec-Fetch-Site") != "cross-site"

        def _send(self, status, body, content_type="application/json"):
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self._origin():
                return self._send(403, encode({"ok": False, "error": "ORIGIN_DENIED"}))
            if self.path == "/":
                return self._send(200, HTML, "text/html")
            if self.path == "/app.js":
                return self._send(200, JS, "application/javascript")
            self._send(404, encode({"ok": False, "error": "ROUTE_NOT_FOUND"}))

        def do_POST(self):
            if not self._origin() or self.headers.get("X-Owned-Home") != "1":
                return self._send(403, encode({"ok": False, "error": "ORIGIN_DENIED"}))
            if self.path != "/v1/turn":
                return self._send(404, encode({"ok": False, "error": "ROUTE_NOT_FOUND"}))
            try:
                lengths = self.headers.get_all("Content-Length") or []
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding") is not None:
                    raise HomeError("INVALID_LENGTH")
                length = int(lengths[0])
                if not 0 < length <= 65536 or self.headers.get("Content-Type") != "application/json":
                    raise HomeError("INVALID_BODY")
                self.connection.settimeout(3)
                body = json.loads(self.rfile.read(length))
                op = body.get("op")
                if op == "turn":
                    exact_keys(body, ("contract_version", "op", "turn"), ("resume",))
                elif op in {"observe", "resume"}:
                    exact_keys(body, ("contract_version", "op", "request_id"))
                else:
                    raise HomeError("OPERATION_NOT_IN_SLICE")
                if body["contract_version"] != VERSION:
                    raise HomeError("CONTRACT_VERSION_MISMATCH")
                # Reject content/identity before even opening persistent stores.
                validate_operation({k: v for k, v in body.items() if k != "contract_version"})
                with OwnedRuntime(directory, scope=SCOPE, fixtures=[FIXTURE], grants=[GRANT]) as runtime:
                    result = (runtime.submit(Turn(**body["turn"]), resume=body.get("resume", False))
                              if op == "turn" else runtime.resume(body["request_id"])
                              if op == "resume" else runtime.observe(body["request_id"]))
                self._send(200, encode({"ok": True, "result": result}))
            except Exception as exc:
                code = str(exc) if isinstance(exc, (HomeError, JournalError)) else "INVALID_REQUEST"
                self._send(400, encode({"ok": False, "error": code}))

    return HTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with make_server(args.store, port=args.port) as server:
        print("http://127.0.0.1:" + str(server.server_port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
