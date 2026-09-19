"""Loopback-only, unprivileged P2-S1 local Web shell.

python -m companion_mind.owned_home.shell --store DIR --port 8765
Open the printed loopback URL. Synthetic fixture/grant setup belongs to the
trusted process; browser ingress can only submit/recover its fixed owner scope.
No production provider, credentials, filesystem browsing or connector routes.
"""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys

from companion_mind.journal import JournalError
from .contracts import (VERSION, AuthorityFixture, Grant, HomeError, Scope, Turn,
                        encode, exact_keys)
from .runtime import OwnedRuntime
from .context import ContextTurn
from .model_gateway import ModelTurn
from .testport import OwnedHomeTestPort, validate_operation
from .human_control import OwnerFixture
from .tool_gateway import ActionRequest, ToolTarget
from .permission import SyntheticGrant
from .source_pack import READONLY_PROFILE, activate_bundle, ensure_demo_bundle
from .readonly_session import execute_readonly_request

SCOPE = Scope("synthetic-home", "synthetic-owner")
FIXTURE = AuthorityFixture("local-demo", "v1", SCOPE.universe_id, SCOPE.access_subject_id,
                           "The local synthetic archive contains one evidence item.")
GRANT = Grant(SCOPE.universe_id, SCOPE.access_subject_id, FIXTURE.source_id, FIXTURE.version)
TOOL_TARGET = ToolTarget("public-target", SCOPE.universe_id, SCOPE.access_subject_id)
TOOL_GRANT = SyntheticGrant("shell-tool-grant", SCOPE.universe_id, SCOPE.access_subject_id,
                            ("public-target",), ("read", "write"))
HUMAN_OWNER = OwnerFixture("synthetic-speaker", SCOPE.universe_id, SCOPE.access_subject_id)

READONLY_HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Companion-Mind · Read-only continuity</title>
<body><main><h1>Read-only continuity</h1><p>Public-safe synthetic demo.</p>
<form id="form" autocomplete="off"><textarea id="message" rows="5" cols="64" maxlength="8000" required></textarea>
<button id="submit">Ask</button><button id="resume" type="button" hidden>Resume</button></form>
<p id="status"></p><pre id="reply"></pre></main><script src="/readonly.js"></script></body></html>"""

READONLY_JS = """'use strict';
const $ = id => document.getElementById(id);
const key = 'owned-home-readonly-v1:' + location.origin;
let handle = null;
function persist(id) { handle = id ? {request_id:id} : null;
  if (handle) localStorage.setItem(key, JSON.stringify(handle)); else localStorage.removeItem(key); }
function render(r) { $('status').textContent = r.stop_reason || r.status || '';
  $('reply').textContent = r.visible_reply || ''; $('resume').hidden = r.status !== 'AWAIT_EXPLICIT_RESUME'; }
async function call(body) { const response = await fetch('/v1/readonly', {method:'POST',
  headers:{'Content-Type':'application/json','X-Owned-Home':'1'}, body:JSON.stringify(body)});
  const data = await response.json(); if (!data.ok) throw new Error(data.error); render(data.result); return data.result; }
$('form').addEventListener('submit', async event => { event.preventDefault();
  const text = $('message').value; const id = crypto.randomUUID(); persist(id); $('message').value = '';
  try { await call({op:'ro_turn',request_id:id,message:text}); } catch (error) { $('status').textContent = error.message; } });
$('resume').addEventListener('click', async () => { if (!handle) return;
  try { await call({op:'ro_resume',request_id:handle.request_id}); } catch (error) { $('status').textContent = error.message; } });
addEventListener('pagehide', () => { $('message').value = ''; });
try { const saved = JSON.parse(localStorage.getItem(key)); if (saved && typeof saved.request_id === 'string') {
  handle = {request_id:saved.request_id}; call({op:'ro_observe',request_id:saved.request_id}).catch(()=>{});
} else { localStorage.removeItem(key); } } catch (_) { localStorage.removeItem(key); }
"""

HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Companion-Mind · Local slice</title>
<body><main><h1>Companion-Mind</h1><p>Local synthetic workspace · <a href="/continuity">Multi-topic workspace</a> · <a href="/tools">Synthetic tools</a> · <a href="/human">Human decisions</a></p>
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


# The v1 page/script stays available for existing clients and recovery handles.
# The linked continuity page has its own strictly allowlisted control record.
CONTINUITY_HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Companion-Mind · Topics</title><body><main><h1>Companion-Mind</h1>
<p>Local synthetic workspace</p><p id="session"></p><p id="active-topic"></p>
<label for="topic">Topic</label><select id="topic"><option value="topic-a">Topic A</option>
<option value="topic-b">Topic B</option><option value="topic-c">Topic C</option></select>
<button id="switch" type="button">Switch topic</button>
<form id="form" autocomplete="off"><label for="message">Your message</label><br>
<textarea id="message" rows="5" cols="64" maxlength="8000" autocomplete="off" required></textarea><br>
<button id="submit">Send</button><button id="resume" type="button" hidden>Resume pending turn</button>
<button id="next" type="button" hidden>New turn</button></form>
<p id="status" role="status"></p><pre id="reply"></pre>
<a href="/">Single-turn workspace</a> · <a href="/models">Model workspace</a></main><script src="/continuity.js"></script></body></html>"""

CONTINUITY_JS = """'use strict';
const $ = id => document.getElementById(id);
const key = 'owned-home-v2:' + location.origin;
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
const topics = ['topic-a','topic-b','topic-c'];
let control, state = null, busy = false;
function persist() {
  // Exactly four non-secret control fields. Never serialize a server object,
  // DOM value, message, fixture, context, trace, or source body.
  localStorage.setItem(key, JSON.stringify({session_id:control.session_id,
    topic_id:control.topic_id,request_id:control.request_id,turn_no:control.turn_no}));
}
function controls() {
  const held = busy || (control.request_id !== null && state !== 'NOT_FOUND');
  $('submit').disabled = held; $('message').disabled = held;
  $('switch').disabled = busy || ['AWAIT_EXPLICIT_RESUME','UNKNOWN'].includes(state);
  $('topic').disabled = $('switch').disabled;
  $('resume').disabled = busy; $('next').disabled = busy;
  $('resume').hidden = !['AWAIT_EXPLICIT_RESUME','UNKNOWN'].includes(state);
  $('next').hidden = control.request_id === null || ['AWAIT_EXPLICIT_RESUME','UNKNOWN'].includes(state);
  $('session').textContent = 'Session: ' + control.session_id;
  $('active-topic').textContent = 'Active topic: ' + control.topic_id;
}
function render(r) {
  state = r.status;
  if (Number.isInteger(r.next_turn_no) && r.next_turn_no > control.turn_no) control.turn_no = r.next_turn_no;
  if (topics.includes(r.topic_id)) { control.topic_id = r.topic_id; $('topic').value = r.topic_id; }
  persist(); $('status').textContent = r.stop_reason || state;
  $('reply').textContent = r.visible_reply || ''; controls();
}
async function call(body) {
  const response = await fetch('/v1/turn', {method:'POST',headers:{'Content-Type':'application/json','X-Owned-Home':'1'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!data.ok) throw new Error(data.error);
  render(data.result); return data.result;
}
function clearTurn() {
  control.request_id = null; state = null; $('message').value = ''; $('reply').textContent = '';
  persist(); controls();
}
async function send(text, op) {
  if (busy) return;
  busy = true; controls();
  try {
    if (control.request_id) {
      const r = await call({contract_version:'owned-home/1',op:'observe',request_id:control.request_id});
      if (r.status !== 'NOT_FOUND') return;
    } else { control.request_id = crypto.randomUUID(); persist(); }
    const turn = {contract_version:'owned-home/1',request_id:control.request_id,
      session_id:control.session_id,turn_id:control.request_id,turn_no:control.turn_no,
      universe_id:'synthetic-home',access_subject_id:'synthetic-owner',
      source_id:'local-demo',source_version:'v1',topic_id:control.topic_id,premise_id:'task-v1',
      evidence_needs:[{source_id:'local-demo',route:'CURRENT'}],text:text,
      observed_at:new Date().toISOString(),budget_bytes:16384};
    await call({contract_version:'owned-home/1',op:op,turn:turn});
  } catch (e) { state = 'UNKNOWN'; $('status').textContent = e.message; }
  finally { $('message').value = ''; busy = false; controls(); }
}
$('form').addEventListener('submit', async e => {
  e.preventDefault(); const text = $('message').value; $('message').value = '';
  await send(text, 'context_turn');
});
$('switch').addEventListener('click', async () => {
  if (busy || ['AWAIT_EXPLICIT_RESUME','UNKNOWN'].includes(state)) return;
  const chosen = $('topic').value;
  if (!topics.includes(chosen)) return;
  clearTurn(); control.topic_id = chosen; persist(); controls();
  await send('Switch topic.', 'topic_switch');
});
$('next').addEventListener('click', () => {
  if (!busy && !['AWAIT_EXPLICIT_RESUME','UNKNOWN'].includes(state)) clearTurn();
});
$('resume').addEventListener('click', async () => {
  if (busy || !control.request_id || !['AWAIT_EXPLICIT_RESUME','UNKNOWN'].includes(state)) return;
  busy = true; controls();
  try { await call({contract_version:'owned-home/1',op:state === 'UNKNOWN' ? 'observe' : 'resume',request_id:control.request_id}); }
  catch (e) { state = 'UNKNOWN'; $('status').textContent = e.message; }
  finally { busy = false; controls(); }
});
addEventListener('pagehide', () => { $('message').value = ''; });
try {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(key)); } catch (_) {}
  const valid = saved && uuid(saved.session_id) && topics.includes(saved.topic_id) &&
    (saved.request_id === null || uuid(saved.request_id)) && Number.isInteger(saved.turn_no) && saved.turn_no >= 1 && saved.turn_no <= 1000000;
  control = valid ? {session_id:saved.session_id,topic_id:saved.topic_id,request_id:saved.request_id,turn_no:saved.turn_no} :
    {session_id:crypto.randomUUID(),topic_id:'topic-a',request_id:null,turn_no:1};
  persist(); $('topic').value = control.topic_id; controls();
  if (control.request_id) {
    busy = true; controls();
    call({contract_version:'owned-home/1',op:'observe',request_id:control.request_id})
      .catch(e => { state = 'UNKNOWN'; $('status').textContent = e.message; })
      .finally(() => { busy = false; controls(); });
  }
} catch (_) { busy = true; $('submit').disabled = true; $('switch').disabled = true; $('status').textContent = 'Recovery storage unavailable.'; }
"""


MODELS_HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Companion-Mind · Models</title><body><main><h1>Companion-Mind</h1>
<p>Local synthetic model workspace</p><p id="session"></p><p id="active-topic"></p>
<label for="topic">Topic</label><select id="topic"><option value="topic-a">Topic A</option>
<option value="topic-b">Topic B</option><option value="topic-c">Topic C</option></select>
<button id="switch" type="button">Choose topic</button><br>
<label for="model">Profile for next turn</label><select id="model">
<option value="synthetic-small">Small · v1</option><option value="synthetic-large">Large · v1</option>
<option value="synthetic-capable">Structured and tool capable · v1</option><option value="AUTO">Select automatically</option></select>
<button id="apply-model" type="button">Choose profile</button><p id="active-model"></p>
<form id="form" autocomplete="off"><label for="message">Your message</label><br>
<textarea id="message" rows="5" cols="64" maxlength="8000" autocomplete="off" required></textarea><br>
<button id="submit">Send</button><button id="resume" type="button" hidden>Check pending turn</button>
<button id="next" type="button" hidden>New turn</button></form>
<p id="status" role="status"></p><pre id="reply"></pre><pre id="model-info"></pre>
<a href="/continuity">Topic workspace</a></main><script src="/models.js"></script></body></html>"""

MODELS_JS = """'use strict';
const $ = id => document.getElementById(id);
const key = 'owned-home-v3:' + location.origin;
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
const topics = ['topic-a','topic-b','topic-c'];
const profiles = ['synthetic-small','synthetic-large','synthetic-capable','AUTO'];
let control, state = null, busy = false;
function persist() {
  // Six allowlisted control fields only. No bodies, traces or server objects.
  localStorage.setItem(key, JSON.stringify({session_id:control.session_id,
    topic_id:control.topic_id,request_id:control.request_id,turn_no:control.turn_no,
    profile_key:control.profile_key,profile_version:control.profile_version}));
}
function unresolved() { return ['AWAIT_EXPLICIT_RESUME','UNKNOWN','MODEL_UNCERTAIN'].includes(state); }
function controls() {
  $('submit').disabled = busy || (control.request_id !== null && state !== 'NOT_FOUND');
  $('message').disabled = $('submit').disabled;
  for (const id of ['switch','topic','apply-model','model']) $(id).disabled = busy || unresolved();
  $('resume').disabled = busy; $('next').disabled = busy;
  $('resume').hidden = !unresolved();
  $('resume').textContent = state === 'AWAIT_EXPLICIT_RESUME' ? 'Resume pending turn' : 'Check durable result';
  $('next').hidden = control.request_id === null || ['AWAIT_EXPLICIT_RESUME','UNKNOWN'].includes(state);
  $('next').textContent = state === 'MODEL_UNCERTAIN' ? 'Start a new turn (explicit decision)' : 'New turn';
  $('session').textContent = 'Session: ' + control.session_id;
  $('active-topic').textContent = 'Active topic: ' + control.topic_id;
}
function render(r) {
  state = r.model_result && ['UNKNOWN','TIMEOUT'].includes(r.model_result.outcome) ? 'MODEL_UNCERTAIN' : r.status;
  if (Number.isInteger(r.next_turn_no) && r.next_turn_no > control.turn_no) control.turn_no = r.next_turn_no;
  if (topics.includes(r.topic_id)) { control.topic_id = r.topic_id; $('topic').value = r.topic_id; }
  const model = r.model_trace && r.model_trace.selected_profile;
  $('active-model').textContent = model ? 'Active synthetic profile: ' + model.profile_key + ' / ' + model.profile_version : 'No model invoked';
  $('model-info').textContent = r.model_trace ? JSON.stringify({
    outcome:r.model_trace.terminal_model_outcome,budget:r.model_trace.budget_decision,
    selection:r.model_trace.selection_reason,retry:r.model_trace.retry_decision}) : '';
  persist(); $('status').textContent = r.stop_reason || state;
  $('reply').textContent = r.visible_reply || ''; controls();
}
async function call(body) {
  const response = await fetch('/v1/turn', {method:'POST',headers:{'Content-Type':'application/json','X-Owned-Home':'1'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!data.ok) throw new Error(data.error);
  render(data.result); return data.result;
}
function clearTurn() {
  control.request_id = null; state = null; $('message').value = ''; $('reply').textContent = ''; $('model-info').textContent = '';
  persist(); controls();
}
$('form').addEventListener('submit', async e => {
  e.preventDefault();
  if (busy || $('submit').disabled) return;
  const text = $('message').value; $('message').value = '';
  busy = true; controls();
  try {
    if (control.request_id) {
      const r = await call({contract_version:'owned-home/1',op:'observe',request_id:control.request_id});
      if (r.status !== 'NOT_FOUND') return;
    } else { control.request_id = crypto.randomUUID(); persist(); }
    const automatic = control.profile_key === 'AUTO';
    const turn = {contract_version:'owned-home/1',request_id:control.request_id,
      session_id:control.session_id,turn_id:control.request_id,turn_no:control.turn_no,
      universe_id:'synthetic-home',access_subject_id:'synthetic-owner',
      source_id:'local-demo',source_version:'v1',topic_id:control.topic_id,premise_id:'task-v1',
      evidence_needs:[{source_id:'local-demo',route:'CURRENT'}],text:text,
      observed_at:new Date().toISOString(),budget_bytes:16384,
      model_intent:{preferred_profile_key:automatic ? null : control.profile_key,
                    preferred_profile_version:automatic ? null : control.profile_version}};
    await call({contract_version:'owned-home/1',op:'model_turn',turn:turn});
  } catch (e) { state = 'UNKNOWN'; $('status').textContent = e.message; }
  finally { $('message').value = ''; busy = false; controls(); }
});
$('apply-model').addEventListener('click', () => {
  if (busy || unresolved() || !profiles.includes($('model').value)) return;
  control.profile_key = $('model').value; control.profile_version = 'v1'; persist();
  $('status').textContent = 'Profile selected for the next explicit turn.';
});
$('switch').addEventListener('click', () => {
  if (busy || unresolved() || !topics.includes($('topic').value)) return;
  clearTurn(); control.topic_id = $('topic').value; persist(); controls();
});
$('next').addEventListener('click', () => {
  if (!busy && !['AWAIT_EXPLICIT_RESUME','UNKNOWN'].includes(state)) clearTurn();
});
$('resume').addEventListener('click', async () => {
  if (busy || !control.request_id || !unresolved()) return;
  busy = true; controls();
  try { await call({contract_version:'owned-home/1',op:state === 'AWAIT_EXPLICIT_RESUME' ? 'resume' : 'observe',request_id:control.request_id}); }
  catch (e) { state = 'UNKNOWN'; $('status').textContent = e.message; }
  finally { busy = false; controls(); }
});
addEventListener('pagehide', () => { $('message').value = ''; });
try {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(key)); } catch (_) {}
  const valid = saved && uuid(saved.session_id) && topics.includes(saved.topic_id) &&
    (saved.request_id === null || uuid(saved.request_id)) && Number.isInteger(saved.turn_no) && saved.turn_no >= 1 && saved.turn_no <= 1000000 &&
    profiles.includes(saved.profile_key) && saved.profile_version === 'v1';
  control = valid ? {session_id:saved.session_id,topic_id:saved.topic_id,request_id:saved.request_id,
    turn_no:saved.turn_no,profile_key:saved.profile_key,profile_version:saved.profile_version} :
    {session_id:crypto.randomUUID(),topic_id:'topic-a',request_id:null,turn_no:1,profile_key:'synthetic-small',profile_version:'v1'};
  persist(); $('topic').value = control.topic_id; $('model').value = control.profile_key; controls();
  $('active-model').textContent = 'Profile intent: ' + control.profile_key + ' / ' + control.profile_version;
  if (control.request_id) {
    busy = true; controls();
    call({contract_version:'owned-home/1',op:'observe',request_id:control.request_id})
      .catch(e => { state = 'UNKNOWN'; $('status').textContent = e.message; })
      .finally(() => { busy = false; controls(); });
  }
} catch (_) { busy = true; for (const id of ['submit','switch','apply-model']) $(id).disabled = true; $('status').textContent = 'Recovery storage unavailable.'; }
"""


TOOLS_HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Companion-Mind · Synthetic tools</title><body><main>
<h1>Synthetic tool workspace</h1><p>Local demo · <a href="/models">Model workspace</a></p>
<label for="skill">Skill</label><select id="skill">
<option value="synthetic.compute">P0 · Add numbers</option>
<option value="synthetic.scoped_read">P1 · Read demo resource</option>
<option value="synthetic.reversible_write">P2 · Write demo number</option>
<option value="synthetic.consequential_send">P3 · Consequential action (held)</option>
<option value="synthetic.critical">P4 · Critical action (held)</option></select>
<button id="apply-skill" type="button">Select for next action</button>
<p id="active-skill"></p><form id="form" autocomplete="off">
<label for="message">Numbers: comma-separated for P0; one integer for P2</label><br>
<textarea id="message" rows="2" maxlength="256" autocomplete="off"></textarea><br>
<button id="submit">Run synthetic action</button>
<button id="resume" type="button" hidden>Check / explicitly resume</button>
<button id="next" type="button" hidden>New action</button></form>
<p id="status" role="status"></p><pre id="reply"></pre><pre id="tool-info"></pre>
</main><script src="/tools.js"></script></body></html>"""

TOOLS_JS = """'use strict';
const $ = id => document.getElementById(id);
const key = 'owned-home-v4:' + location.origin;
const skills = ['synthetic.compute','synthetic.scoped_read','synthetic.reversible_write','synthetic.consequential_send','synthetic.critical'];
const uuid = s => typeof s === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(s);
let control, busy = false, state = null, resolved = false;
function persist() { localStorage.setItem(key, JSON.stringify(control)); }
function controls() {
  const pending = control.action_id && !resolved && state !== 'NOT_FOUND';
  $('submit').disabled = busy || pending || resolved;
  $('apply-skill').disabled = busy || !!control.action_id;
  $('resume').hidden = !pending; $('resume').disabled = busy;
  $('next').hidden = !resolved; $('next').disabled = busy;
  $('active-skill').textContent = control.skill_id + ' / ' + control.skill_version;
}
function render(r) {
  state = r.status; resolved = r.terminal_count === 1;
  if (Number.isInteger(r.next_turn_no) && r.next_turn_no > control.turn_no) control.turn_no = r.next_turn_no;
  $('status').textContent = r.stop_reason || state;
  $('reply').textContent = r.visible_reply || '';
  $('tool-info').textContent = r.tool_trace ? JSON.stringify(r.tool_trace) : '';
  persist(); controls();
}
async function call(body) {
  const response = await fetch('/v1/tool', {method:'POST',headers:{'Content-Type':'application/json','X-Owned-Home':'1'},body:JSON.stringify(body)});
  const data = await response.json(); if (!data.ok) throw new Error(data.error);
  render(data.result); return data.result;
}
$('form').addEventListener('submit', async e => {
  e.preventDefault(); if (busy || $('submit').disabled) return;
  const text = $('message').value; $('message').value = '';
  busy = true; controls();
  try {
    if (control.action_id) {
      const observed = await call({contract_version:'owned-home/1',op:'tool_observe',action_id:control.action_id});
      if (observed.status !== 'NOT_FOUND') return;
    }
    let parameters = {};
    if (['synthetic.compute','synthetic.reversible_write'].includes(control.skill_id)) {
      if (!/^-?\\d+(\\s*,\\s*-?\\d+)*$/.test(text.trim())) throw new Error('Enter integers only.');
      const values = text.split(',').map(Number);
      if (values.some(v => !Number.isInteger(v) || Math.abs(v) > 1000000)) throw new Error('Number outside demo range.');
      if (control.skill_id === 'synthetic.compute') parameters = {values:values};
      else { if (values.length !== 1) throw new Error('Enter one number.'); parameters = {value:values[0]}; }
    }
    if (control.skill_id === 'synthetic.critical') parameters = {operation:'critical'};
    if (!control.action_id) { control.action_id = crypto.randomUUID(); persist(); }
    const action = {action_id:control.action_id,idempotency_key:control.action_id,
      task_id:'tool-task-' + control.session_id,request_id:control.action_id,session_id:control.session_id,
      turn_id:control.action_id,turn_no:control.turn_no,universe_id:'synthetic-home',access_subject_id:'synthetic-owner',
      skill_id:control.skill_id,skill_version:control.skill_version,target_id:control.target_id,
      parameters:parameters,grant_id:control.skill_id === 'synthetic.compute' ? null : 'shell-tool-grant',
      observed_at:new Date().toISOString()};
    await call({contract_version:'owned-home/1',op:'tool_execute',action:action});
  } catch (e) { state = 'TRANSPORT_UNCERTAIN'; $('status').textContent = e.message; }
  finally { $('message').value = ''; busy = false; controls(); }
});
$('apply-skill').addEventListener('click', () => {
  if (busy || control.action_id || !skills.includes($('skill').value)) return;
  control.skill_id = $('skill').value; persist(); controls();
});
$('next').addEventListener('click', () => {
  if (busy || !resolved) return;
  control.action_id = null; resolved = false; state = null;
  $('message').value = ''; $('reply').textContent = ''; $('tool-info').textContent = '';
  persist(); controls();
});
$('resume').addEventListener('click', async () => {
  if (busy || !control.action_id || resolved) return;
  const op = ['AWAIT_EXPLICIT_RESUME','AWAIT_EXPLICIT_RECONCILE'].includes(state) ? 'tool_resume' : 'tool_observe';
  busy = true; controls();
  try { await call({contract_version:'owned-home/1',op:op,action_id:control.action_id}); }
  catch (e) { state = 'TRANSPORT_UNCERTAIN'; $('status').textContent = e.message; }
  finally { busy = false; controls(); }
});
addEventListener('pagehide', () => { $('message').value = ''; });
try {
  let saved = null; try { saved = JSON.parse(localStorage.getItem(key)); } catch (_) {}
  const valid = saved && uuid(saved.session_id) && (saved.action_id === null || uuid(saved.action_id)) &&
    Number.isInteger(saved.turn_no) && saved.turn_no >= 1 && saved.turn_no <= 1000000 &&
    skills.includes(saved.skill_id) && saved.skill_version === 'v1' && saved.target_id === 'public-target';
  control = valid ? {session_id:saved.session_id,action_id:saved.action_id,turn_no:saved.turn_no,
    skill_id:saved.skill_id,skill_version:saved.skill_version,target_id:saved.target_id} :
    {session_id:crypto.randomUUID(),action_id:null,turn_no:1,skill_id:'synthetic.compute',skill_version:'v1',target_id:'public-target'};
  persist(); $('skill').value = control.skill_id; controls();
  if (control.action_id) {
    busy = true; controls();
    call({contract_version:'owned-home/1',op:'tool_observe',action_id:control.action_id})
      .catch(e => { state = 'TRANSPORT_UNCERTAIN'; $('status').textContent = e.message; })
      .finally(() => { busy = false; controls(); });
  }
} catch (_) { busy = true; $('submit').disabled = true; $('apply-skill').disabled = true; $('status').textContent = 'Recovery storage unavailable.'; }
"""


HUMAN_HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Local human decision</title>
<body><main><h1>Human decision</h1><p><a href="/">Home</a> · Public synthetic task · one local step</p>
<button id="create" type="button">Create decision request</button>
<form id="form" autocomplete="off"><label for="message">Response: CONTINUE, HOLD, CANCEL or UNSURE</label>
<input id="message" maxlength="64" autocomplete="off"><button id="submit">Save response</button></form>
<button id="resume" type="button" hidden>Resume one step</button>
<button id="cancel" type="button">Cancel request</button><button id="next" type="button" hidden>New task</button>
<p id="status" role="status"></p><pre id="reply"></pre></main><script src="/human.js"></script></body></html>"""

HUMAN_JS = """'use strict';
const $ = id => document.getElementById(id), key = 'owned-home-v5:' + location.origin;
let handle = null, current = null, busy = false, state = null;
function persist(h) {
  handle = {id:h.id,created_at:h.created_at,expires_at:h.expires_at};
  localStorage.setItem(key, JSON.stringify(handle));
}
function controls() {
  $('create').disabled = busy || (handle !== null && state !== 'NOT_FOUND');
  $('submit').disabled = busy || state !== 'WAITING'; $('message').disabled = $('submit').disabled;
  $('resume').hidden = !['RESPONSE_DURABLE','UNKNOWN'].includes(state);
  $('resume').disabled = busy;
  $('resume').textContent = state === 'UNKNOWN' ? 'Check request' : 'Resume one step';
  $('cancel').disabled = busy || !current || !current.human_request || ['STOP','UNKNOWN'].includes(state);
  $('next').hidden = !['STOP','NOT_FOUND'].includes(state); $('next').disabled = busy;
}
function render(r) {
  current = r; state = r.status; $('status').textContent = r.stop_reason || r.status;
  $('reply').textContent = r.continuation ? r.continuation.result : ''; controls();
}
async function call(op, fields={}) {
  const response = await fetch('/v1/human', {method:'POST',headers:{'Content-Type':'application/json','X-Owned-Home':'1'},
    body:JSON.stringify({contract_version:'owned-home/1',op,...fields})});
  const data = await response.json(); if (!data.ok) throw new Error(data.error);
  render(data.result); return data.result;
}
async function action(fn) {
  if (busy) return; busy = true; controls();
  try { await fn(); } catch (error) { current = null; state = 'UNKNOWN'; $('status').textContent = error.message; }
  finally { $('message').value = ''; busy = false; controls(); }
}
function identity() {
  const id = handle.id;
  return {human_request_id:id,request_id:'req-'+id,trace_id:'trace-'+id,goal_id:'goal-'+id,task_id:'task-'+id,
    session_id:id,turn_id:id,turn_no:1,universe_id:'synthetic-home',access_subject_id:'synthetic-owner',owner_id:'synthetic-speaker'};
}
$('create').addEventListener('click', () => action(async () => {
  if (handle) {
    const r = await call('human_observe',{human_request_id:handle.id});
    if (r.status !== 'NOT_FOUND') return;
  } else {
    const now = new Date(); persist({id:crypto.randomUUID(),created_at:now.toISOString(),expires_at:new Date(now.getTime()+3600000).toISOString()});
  }
  await call('human_request',{human_request:{...identity(),created_at:handle.created_at,expires_at:handle.expires_at}});
}));
$('form').addEventListener('submit', e => {
  e.preventDefault(); if (!current || state !== 'WAITING') return;
  const text = $('message').value, fp = current.human_request.request_fingerprint;
  return action(() => call('human_respond',{human_response:{...identity(),request_fingerprint:fp,response_id:'response-'+handle.id,text}}));
});
$('resume').addEventListener('click', () => action(async () => {
  if (!handle) return;
  if (state === 'UNKNOWN' || !current) { await call('human_observe',{human_request_id:handle.id}); return; }
  await call('human_resume',{human_request_id:handle.id,request_fingerprint:current.human_request.request_fingerprint});
}));
$('cancel').addEventListener('click', () => action(async () => {
  if (current && current.human_request) await call('human_cancel',{human_request_id:handle.id,request_fingerprint:current.human_request.request_fingerprint});
}));
$('next').addEventListener('click', () => {
  if (busy || !['STOP','NOT_FOUND'].includes(state)) return;
  localStorage.removeItem(key); handle = null; current = null; state = null;
  $('message').value = ''; $('reply').textContent = ''; $('status').textContent = ''; controls();
});
addEventListener('pagehide', () => { current = null; $('message').value = ''; });
try {
  let saved = null; try { saved = JSON.parse(localStorage.getItem(key)); } catch (_) {}
  const iso = s => typeof s === 'string' && /^\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d\\.\\d{3}Z$/.test(s) && Number.isFinite(Date.parse(s));
  if (saved && typeof saved.id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(saved.id)
      && iso(saved.created_at) && iso(saved.expires_at)) {
    persist(saved); action(() => call('human_observe',{human_request_id:handle.id}));
  } else { localStorage.removeItem(key); controls(); }
} catch (_) { busy = true; controls(); $('status').textContent = 'Recovery storage unavailable.'; }
"""


def make_server(directory, *, host="127.0.0.1", port=0):
    if host != "127.0.0.1":
        raise HomeError("LOOPBACK_ONLY")
    if type(port) is not int or not 0 <= port <= 65535:
        raise HomeError("INVALID_PORT")
    directory = Path(directory)
    readonly_state = None

    def readonly_setup():
        nonlocal readonly_state
        if readonly_state is None:
            readonly_now = datetime.now(timezone.utc).isoformat()
            readonly_input, readonly_grant, readonly_manifest = ensure_demo_bundle(
                directory / "readonly-demo-input", readonly_now)
            activate_bundle(readonly_input, directory / "readonly", readonly_grant, readonly_now)
            readonly_state = (readonly_input, readonly_grant, readonly_manifest)
        return readonly_state

    def readonly_request(operation):
        readonly_input, readonly_grant, _ = readonly_setup()
        request = {
            "contract_version": VERSION, "profile_version": READONLY_PROFILE,
            "scope": SCOPE.projection(), "readonly_bundle_root": str(readonly_input),
            "readonly_grants": [readonly_grant],
            "readonly_now": datetime.now(timezone.utc).isoformat(),
            **operation,
        }
        return execute_readonly_request(directory, request)

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
            if self.path == "/continuity":
                return self._send(200, CONTINUITY_HTML, "text/html")
            if self.path == "/continuity.js":
                return self._send(200, CONTINUITY_JS, "application/javascript")
            if self.path == "/models":
                return self._send(200, MODELS_HTML, "text/html")
            if self.path == "/models.js":
                return self._send(200, MODELS_JS, "application/javascript")
            if self.path == "/tools":
                return self._send(200, TOOLS_HTML, "text/html")
            if self.path == "/tools.js":
                return self._send(200, TOOLS_JS, "application/javascript")
            if self.path == "/human":
                return self._send(200, HUMAN_HTML, "text/html")
            if self.path == "/human.js":
                return self._send(200, HUMAN_JS, "application/javascript")
            if self.path == "/readonly":
                return self._send(200, READONLY_HTML, "text/html")
            if self.path == "/readonly.js":
                return self._send(200, READONLY_JS, "application/javascript")
            self._send(404, encode({"ok": False, "error": "ROUTE_NOT_FOUND"}))

        def do_POST(self):
            if not self._origin() or self.headers.get("X-Owned-Home") != "1":
                return self._send(403, encode({"ok": False, "error": "ORIGIN_DENIED"}))
            if self.path not in ("/v1/turn", "/v1/tool", "/v1/human", "/v1/readonly"):
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
                if self.path == "/v1/readonly":
                    exact_keys(body, ("op",), ("request_id", "message", "source_id", "selector"))
                    op = body["op"]
                    if op == "ro_turn":
                        exact_keys(body, ("op", "request_id", "message"))
                        _, _, readonly_manifest = readonly_setup()
                        identifier = body["request_id"]
                        if not isinstance(identifier, str) or len(identifier) > 80:
                            raise HomeError("INVALID_ID")
                        state = readonly_request({"op": "ro_session_state",
                                                  "task_id": readonly_manifest["task_id"],
                                                  "session_id": "shell-readonly-session"})
                        managed = {"op": "ro_turn", "turn": {
                            "task_id": readonly_manifest["task_id"],
                            "session_id": "shell-readonly-session",
                            "request_id": identifier, "turn_id": identifier,
                            "turn_no": state["next_turn_no"],
                            "package_id": readonly_manifest["package_id"],
                            "package_version": readonly_manifest["package_version"],
                            "manifest_digest": readonly_manifest["manifest_digest"],
                            "question": body["message"], "universe_id": SCOPE.universe_id,
                            "access_subject_id": SCOPE.access_subject_id,
                            "evidence_needs": [{"source_id": "current", "selector": "agenda"}],
                            "budget_bytes": 4096,
                        }}
                    elif op in {"ro_observe", "ro_resume"}:
                        exact_keys(body, ("op", "request_id"))
                        managed = {"op": op, "request_id": body["request_id"]}
                    elif op == "ro_info":
                        exact_keys(body, ("op",))
                        managed = {"op": "ro_info"}
                    elif op == "ro_safe_export":
                        exact_keys(body, ("op",))
                        managed = {"op": "ro_safe_export"}
                    elif op == "ro_source_view":
                        exact_keys(body, ("op", "source_id", "selector"))
                        managed = {"op": op, "package_id": readonly_manifest["package_id"],
                                   "package_version": readonly_manifest["package_version"],
                                   "manifest_digest": readonly_manifest["manifest_digest"],
                                   "source_id": body["source_id"], "selector": body["selector"]}
                    else:
                        raise HomeError("OPERATION_NOT_IN_SLICE")
                    return self._send(200, encode({"ok": True, "result": readonly_request(managed)}))
                if self.path == "/v1/human":
                    if op not in {"human_request", "human_respond", "human_observe", "human_resume", "human_cancel"}:
                        raise HomeError("OPERATION_NOT_IN_SLICE")
                    if body.get("contract_version") != VERSION:
                        raise HomeError("CONTRACT_VERSION_MISMATCH")
                    operation = {k: v for k, v in body.items() if k != "contract_version"}
                    validate_operation(operation)
                    with OwnedHomeTestPort(directory, scope=SCOPE, human_owners=[HUMAN_OWNER],
                                           human_now=datetime.now(timezone.utc).isoformat()) as port:
                        result = port.execute(operation)
                    return self._send(200, encode({"ok": True, "result": result}))
                if self.path == "/v1/tool":
                    if op == "tool_execute":
                        exact_keys(body, ("contract_version", "op", "action"))
                    elif op in {"tool_observe", "tool_resume"}:
                        exact_keys(body, ("contract_version", "op", "action_id"))
                    else:
                        raise HomeError("OPERATION_NOT_IN_SLICE")
                    if body["contract_version"] != VERSION:
                        raise HomeError("CONTRACT_VERSION_MISMATCH")
                    validate_operation({k: v for k, v in body.items() if k != "contract_version"})
                    with OwnedRuntime(directory, scope=SCOPE, tool_targets=[TOOL_TARGET], tool_grants=[TOOL_GRANT]) as runtime:
                        result = (runtime.tool_execute(ActionRequest(**body["action"])) if op == "tool_execute" else
                                  runtime.tool_observe(body["action_id"], resume=op == "tool_resume"))
                    return self._send(200, encode({"ok": True, "result": result}))
                if op in {"turn", "context_turn", "topic_switch", "model_turn"}:
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
                    result = (runtime.submit((Turn if op == "turn" else ModelTurn if op == "model_turn" else ContextTurn)(**body["turn"]), resume=body.get("resume", False))
                              if op in {"turn", "context_turn", "topic_switch", "model_turn"} else runtime.resume(body["request_id"])
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
