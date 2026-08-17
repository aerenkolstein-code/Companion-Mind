# C2 read-only long-conversation recovery prototype

Issue: `ENG-C2-RECOVERY-01` / GitHub #10.

This prototype recovers a ChatGPT conversation **from data already resident in the current browser tab's React Query cache**, then reconciles the local bulk export against a separately captured renderable-turn ledger. It does not use an unofficial bulk API, copy credentials, or mutate QueryClient state.

## Boundary

The browser adapter is read-only:

- no `fetch` / XHR bulk download;
- no QueryClient mutation methods;
- no cookies, Authorization headers, CSRF material, or session credentials;
- no upload of recovered bodies;
- fail closed when the React Fiber / QueryClient / exact conversation query cannot be identified confidently.

Recovered bodies are private local artifacts. **Never commit them to this repository or CI artifacts.** Repository fixtures are synthetic only.

## 1. Browser-side local export

Open the target ChatGPT conversation in the browser and make sure at least one turn is materialized. In DevTools Console, load/paste `tools/chatgpt_recovery_exporter.js`, then run:

```js
const { receipt } = await CM_C2_RECOVERY.run({ download: true });
console.table(receipt);
```

A JSON file is downloaded locally through a Blob URL. The console receipt contains counts and a SHA-256 only; it does not print message bodies.

The adapter:

1. finds a React Fiber handle from a materialized turn;
2. walks Fiber/context ancestry to identify a QueryClient by public method shape, not a minified class name;
3. chooses the successful exact-conversation query containing `mapping` + `current_node`;
4. follows `current_node -> parent -> ... -> root` and reverses to chronological order;
5. preserves root/no-message, user, assistant, tool, structured and non-string payloads;
6. scans object keys for credential/header-shaped surfaces and fails closed if one appears;
7. computes a deterministic SHA-256 over canonical JSON before local download.

## 2. Verify the local artifact

```bash
python -m companion_mind.chatgpt_recovery verify /path/to/chatgpt-c2-recovery-....json
```

Expected output is a compact receipt such as:

```json
{"active_path_size":3719,"credential_surface_count":0,"mapping_size":3775,"schema_version":"cm-c2-recovery/v0.1","sha256":"..."}
```

Counts above are an example of the previously observed live page, not a hard-coded acceptance predicate.

## 3. Reconcile against the renderable-turn ledger

The ledger is a JSON array. Use stable identity whenever available:

```json
[
  {
    "renderable_index": 0,
    "expected_role": "user",
    "known_turn_id_or_dom_id": "stable-id-from-ui-ledger"
  }
]
```

Run:

```bash
python -m companion_mind.chatgpt_recovery reconcile \
  recovery.json \
  renderable-ledger.json \
  --out reconciliation.json
```

The command also writes `reconciliation.gaps.json` unless `--gaps-out` is provided.

Disposition classes:

- `MATCHED`
- `MISSING_FROM_BULK`
- `EXTRA_GRAPH_NODE`
- `AMBIGUOUS_MAPPING`

The reconciler deliberately does **not** fall back to text similarity when stable identity is absent. Missing/ambiguous entries become the bounded gap ledger for targeted sidebar random-access backfill.

## 4. Synthetic public-safe example

```bash
python -m companion_mind.chatgpt_recovery build \
  examples/c2_recovery/synthetic_conversation_payload.json \
  /tmp/c2-synthetic-export.json

python -m companion_mind.chatgpt_recovery reconcile \
  /tmp/c2-synthetic-export.json \
  examples/c2_recovery/synthetic_renderable_ledger.json \
  --out /tmp/c2-synthetic-reconciliation.json
```

The third synthetic ledger entry intentionally does not exist in bulk and therefore produces a gap.

## Acceptance sequence for #10

1. offline/synthetic tests GREEN;
2. one read-only live local export on the current ultra-long conversation;
3. artifact parses and checksum verifies;
4. reconcile against the 3,529-position ledger;
5. report unresolved gap count;
6. STOP and return to Board / Plan Office / Verification.

Formal ingestion into canonical RAW/L0/Drive is explicitly out of scope for this issue.
