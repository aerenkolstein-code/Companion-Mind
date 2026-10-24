# C2 - Experimental ChatGPT Web RAW-only capture

Archived engineering snapshot: 2026-10-24. This is a local-only experimental
browser instrumentation tool under the C2 long-conversation continuity workstream.
It is NOT an OpenAI API, a production cloud integration, or a change to the
Companion-Mind canonical runtime, Journal, Current, Memory, or Persona authority.

## Design

- P2 PRIMARY: listen to existing Chrome DevTools Protocol Network events for
  candidate messages; parse role, UUID, text, and finality.
- P1 RECONCILIATION: when a network candidate is missing, compare two stable
  visible DOM snapshots and require a known trailing message ID as anchor.
  Unanchored or conflicting history remains HOLD.
- P0 SINGLE WRITER: append only approved candidates to local RAW JSON, under
  an exclusive lock, optimistic baseline check, temporary file, and readback.
  Markdown is only a derived local view.
- NON-AUTHORITATIVE PENDING: network+DOM corroborated but unanchored messages
  may be preserved separately; PENDING is not eligible for L0.
- NO DOWNSTREAM FAN-OUT: no L0 compiler, Journal, cloud archive, notification,
  or scheduling is invoked by this collector.

ChatGPT Web internals may change without compatibility notice. An open tab
does not prove full history, cross-device coverage, or no network loss.

## Verification (Node.js 24+; no external packages)

    node --check raw-auto.mjs
    node test-offline.mjs
    node test-pending.mjs
    node test-conflict.mjs

The tests use synthetic messages only. Checks cover message completion,
duplicate IDs, history-prefix protection, stable DOM suffix recovery, atomic
append, and non-authoritative pending constraints.

## Bounded manual usage

Use only an existing Chrome with local CDP on port 9333. The collector never
launches a browser or sends a ChatGPT message.

    node raw-auto.mjs --once --duration-sec=15 --target=CONVERSATION_UUID --stdout

The following is an experimental local writer, NOT a recommendation for
unattended production deployment:

    node raw-auto.mjs --write --target=CONVERSATION_UUID --stdout

Back up your files first. Do not operate two RAW writers against one directory.
The standalone collector uses its source directory for local RAW storage.
Git ignores sessions, pending evidence, logs, and checkpoint directories.

The public-safe mirror omits the machine-specific Windows launcher, original
private trial adapters, private chat/session IDs, absolute machine paths,
RAW message bodies, private L0 checkpoints, and private receipts.

## Boundaries

The bounded single-turn real-chat automatic canary passed. A scoped local
historical overlap chain was also recovered. Neither demonstration proves
unattended reliability, a full cloud archive, cross-device continuity, or
the absence of silently lost messages.

Potential FINALITY_UNKNOWN_HOLD and DOM_NETWORK_CONFLICT_HOLD must never
be silently upgraded to DONE. The local startup guard remained disabled,
and the local original did not enable P3 L0 or Journal integrations.

This branch is a publication archive only; it is not a merge to main.
See MILESTONE.md for further evidence boundaries.

## Conflict auto-reconciliation patch (2026-10-24)

When a completed P2 network text candidate disagrees with DOM for the same
message ID, the collector enters a bounded P1 reconciliation path. It requires
the old RAW tail anchor, two separate stable/final DOM observation rounds at
least 9 seconds apart, unchanged fingerprint/order, and a valid P0-only append.
A successful atomic readback produces RAW_CONFLICT_RECONCILED and
RAW_APPEND_VERIFIED events. Source-specific hashes are retained for audit.

Network-internal contradictory final candidates, incomplete DOM, missing tail,
archived revisions, ambiguous order, or missing corroboration remain HOLD.
The program does not overwrite existing RAW or infer canonical cloud history.

The 21 new synthetic conflict tests passed. This patch has been installed in
one local, running single-target collector, but a subsequent real newly
observed conflict automatically resolved by this patch has not yet been
verified. This publication is therefore a code and regression milestone,
not full live conflict-resolution acceptance.