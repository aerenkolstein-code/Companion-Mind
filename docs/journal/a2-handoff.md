# A019 sanctioned A2 black-box seam v1

This is an A1 implementation candidate interface, not authorization to execute
A2. Independent A2/Board gates remain separate under Issue 26.

Pin the exact candidate commit from the Draft PR and its A1 conformance receipt.
Use the published Canonical Event v1 schema/fixtures from that checkout. Do not
copy/modify the schema or inspect the Journal database as the A2 oracle.
Requires Python 3.11+ on a POSIX local filesystem; no credentials/network.

```sh
python -m pip install -e .
python -m companion_mind.journal --store /tmp/a019-local < operation.json
python -m companion_mind.journal --store /tmp/a019-local --replica /tmp/a019-remote < drain.json
```

Each invocation opens, verifies and recovers the same store before one operation,
then closes it. One UTF-8 JSON object on stdin, one JSON response on stdout:
`{"ok":true,"result":...}` (exit 0) or `{"ok":false,"error":"SAFE_CODE"}`
(exit 2). `--fault F1` through `F8` deliberately exits the subprocess with 86
without running cleanup. Never use it in the evaluator's own process. Reinvoke
the seam without the fault option on the **same** local/remote directories.

| Operation object | Result |
| --- | --- |
| `{"op":"info"}` | Version/pin/offline/recovery markers |
| `{"op":"append","event":EVENT}` | Durable append/duplicate receipt or safe conflict |
| `{"op":"ingest","adapter":"A018","event":EVENT}` | Same append seam with provenance check; also A019/A020 |
| `{"op":"correct","event":EVENT}` | Append-only correction with existing prior target |
| `{"op":"turn","user":USER,"assistant_template":TEMPLATE,"attempt_id":"attempt-1","script":{"frames":["visible\n"],"outcome":"complete"}}` | Durable USER/ASSISTANT receipts, invocation count and boundary trace |
| `{"op":"export","order":"canonical"}` | Events + normalized ordered fingerprint; order may also be journal |
| `{"op":"recover"}` | This open's recovery receipt; never invokes a provider |
| `{"op":"attempts"}` | Public attempt outcome/phase view |
| `{"op":"drain"}` | High-water, expected/verified, remote-ID/readback receipt |
| `{"op":"readback"}` | Offline remote event set, labeled `OFFLINE_DRIVE_STUB` |
| `{"op":"replica_state"}` | Pending/verified/error control receipts |

`EVENT`/`USER`/`TEMPLATE` above stand for the full published v1 mapping. The
template must be assistant/owned-client with the same session/turn/stable
persona/relationship as USER, a new event ID, a later free session sequence,
`content_payload={"text":""}`, no correction target and no reserved
`metadata.extensions.a019_attempt`. The caller supplies deterministic synthetic
timestamps. The template is a staging input, not canonical evidence until the
attempt terminalizes. Outcome options: complete, partial, failed. Empty output
terminalizes failed. Script sanitization is offline preflight, not live streaming.

F1-F5 apply to turn; F6 to append; F7-F8 to drain after a prior append. In F3 use
two safe frames to verify only the durable first visible frame is retained.
For F1 expect USER only and `NOT_SENT`; explicit identical turn submission may
resume. For F2 expect local failed/external UNKNOWN, then repeated identical turn
returns the receipt with zero calls. Never infer provider failure from local
status alone. For F4/F5 expect complete exactly once. All recovery repeats must
retain event set/order/status/payload/correction fingerprints.

The drain request accepts `fail:true` or `corrupt_read:true` on the offline
transport to demonstrate lag/error/mismatch. A subsequent explicit normal drain
may reconcile it. Compare the local set with readback only after completion is
`READBACK_VERIFIED` and only inside the declared high-water envelope. The fake
remote persists between processes; real Google Drive is not exercised.

The checked-in [synthetic manifest](../../examples/a019_e1_manifest.json) declares
two independent fresh runs, 120 turns each, F1-F8 and repeated restarts. A1's
fixture factory is `tools.a019_conformance.pair` / `turn_request`; an independent
A2 runner may construct its own source-conformant inputs without importing
Journal internals. Normalize only operational store UUIDs, recovery generations,
remote reserved IDs, runtime/version/commit receipts when comparing fresh runs;
never normalize away canonical event identity, order, content, status,
provenance, knowledge states or correction edges. Compare canonical exports with
the published order key and a deterministic JSON encoding. A1's normalized
encoding is UTF-8, sorted keys, compact separators, `ensure_ascii=False`, finite
JSON values, SHA-256.

Return independent A2 evidence through its separate work order. Neither this
document nor A1 tests can mark E1 GREEN, merge the PR or activate A029.
