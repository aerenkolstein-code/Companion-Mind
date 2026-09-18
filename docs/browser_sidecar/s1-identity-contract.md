# C1 S1: incremental identity, normalize and dedupe

Work order: `WO-ENG-C1-A018-S1-01 v0.1`. Approved execution receipt:
`AUTH-C1-S1-EXEC-20260918-T0020`. The approved S1 plan and its original
constraints remain the design authority. This file documents the implementation;
it does not grant independent G1 acceptance, merge, later stages or live access.

## Surface and ownership

`companion_mind.browser_sidecar.identity.IdentityReducer` is a sequential,
in-memory, source-local reducer over the unchanged S0 `Capture` and `Attachment`
classes. The single supported profile is the existing frozen `ChatGPT-Web-v0.1`.
No storage, browser transport, clock, controller, network, provider, A019 ingest,
Drive writer or new dependency is introduced. S0 parser/normalizer, fixtures,
profile, pins, ADRs and historical receipts are unchanged.

The public methods are `observe(captures)`,
`reconcile(temporary, stable, mapping)` and `snapshot()`. Both observation
arguments must be concrete lists or tuples of exact S0 `Capture` values, not
serialized objects, generators, subclasses or objects with additional fields.
The methods do not parse HTML. Tests may call the unchanged S0 parser first.
The caller remains responsible for supplying authorized synthetic/public-safe
sources: `Capture` has no credential or source-authorization capability.

## Admission and identities

Admission checks exact field/nested attachment types, safe opaque labels,
profile ID/version/fingerprint, valid role and source position, terminal/control
consistency and the existing conservative scrub boundary. Supplied `verified`
flags and serialized hashes are not trusted inputs. The inherited source key
and normalized fingerprint are calculated from validated fields; role, order,
attachments, profile and terminal/control/redaction fields are not removed.
Accepted values and nested attachments are detached from caller-owned objects.
A caller changing its original object's dictionary cannot change accepted state.

The conversation identity is `(profile_id, scope_alias, conversation_kind,
conversation_id)`. A member additionally has `message_id`; a position index
additionally has `source_position`. Titles, node object addresses, arrival times
and text similarity never establish identity. Different scopes remain isolated;
unsupported profiles fail closed rather than being merged. Equal text under
different message IDs remains distinct when source positions are valid.

Same source/logical identity and same fingerprint is idempotent. A changed
fingerprint under the same source/logical identity raises `CaptureError("CONFLICT")`.
Different members claiming the same logical position raise
`CaptureError("AMBIGUOUS_IDENTITY")`. No last-write-wins or record overwrite is
used to resolve evidence conflicts.

## Atomic observation and HOLD

Each call validates all values, builds candidate state, checks all logical
members/positions, then publishes state once. Failure leaves the previous
observations, aliases and mapping evidence unchanged. No partial result is
returned before completion. Inputs are never rewritten.

`STREAMING` and `STABILIZING` are nonterminal. A batch containing either is held
**as a whole**, including otherwise valid terminal items: `Decision.status` is
`HOLD`, accepted state is unchanged, and streaming bodies are not retained.
Callers must replay the relevant observations later. This conservative policy
avoids half-applied batches; it is not a liveness or scheduling guarantee.
A conflicting terminal item still causes rejection rather than being hidden by
another item's HOLD. A later parser-marked terminal candidate is admitted once.
No real stability clock or live termination claim is made. Complete, partial and
failed semantics continue to follow the S0 parser's evidence convention.

## Explicit temporary-to-stable reconciliation

The mapping has exactly `scope_alias`, `temporary_id`, and `stable_id`. Endpoints
are typed: temporary observations must belong to the named temporary conversation,
and stable observations to the named stable conversation, in the supported
profile/scope. Stable-to-stable/reversed endpoints and fan-out are rejected.
Because aliases go only from a temporary node to a stable node, typed alias
cycles cannot be installed. Every mapping is explicit, never inferred from text.

A candidate union of prior and supplied observations is formed. Every affected
existing or supplied temporary message must have an actual stable counterpart
in that union. The existing S0 `reconcile_conversation` proof is checked for
**every** such message, not only one sample. Missing counterparts are ambiguity,
not permission to drop unmatched members. Stable-only observations remain
independent members. Multiple temporary conversations may point to one stable
conversation only when each complete mapping is proved and the resulting union
is conflict-free.

On success logical keys, positions and duplicate membership are reduced together.
Original observed captures/source keys remain intact and old/new capture IDs
plus matching fingerprints are retained as mapping evidence. Temp-first,
stable-first and replay on both sides produce the same final state for the same
valid inputs. New observations can resolve through an already proved conversation
alias; no unobserved stable Capture is fabricated. A failing proof or resulting
position/fingerprint collision leaves all prior state intact. Nonterminal mapping
input holds the whole mapping, installing no alias.

## Results and error boundary

`Decision` has `status` (`APPLIED`, `IDEMPOTENT`, or `HOLD`) and `member_count`.
All decisions and snapshots set `authority=false`, `canonical_commit=false`,
`ingest_state="NOT_ATTEMPTED"`. A successful decision is an in-memory update,
not an A019 commit or a durability receipt.

`snapshot()` returns detached, deterministic members, positions, original
observations, aliases, and mapping evidence. Returned collections may be changed
by callers without altering the reducer. Diagnostics use constant reason codes:
`INVALID_CAPTURE`, `PROFILE_DRIFT`, `AMBIGUOUS_ROLE`, `AMBIGUOUS_IDENTITY`,
`AMBIGUOUS_TERMINAL`, `UNSAFE_CAPTURE`, `CONFLICT`. Unsafe input values, raw DOM,
signed URLs and secret canaries are not included in error messages.
Scrubbing remains the declared S0 pattern boundary, not a universal DLP claim.

## Tests, protection and handoff

`tests/test_browser_sidecar_s1.py` exercises all twelve G1 families, including
immutable F08-F13, whole-set mapping, atomic failures, nonterminal HOLD, detached
inputs/snapshots, declared secret rejection and 100 combined replay cycles.
`s1-proof-test-mapping.json` links real test IDs and inherited P01-P15 obligations;
it is not a test-results file. `s1-baseline.json` binds the exact base and six-path
scope. The sole existing-file amendment adds only the exact new S1 test path in
`permitted(path)`. Removing those two inserted lines must recover the original
preflight Git blob. The original protected-tree hash and all other rules remain.

Full G0/A019/Owned Home/C2/CLI and existing Test CI checks remain required.
Their temporary SQLite/files/loopback harnesses are inherited regression
infrastructure, not new S1 persistence or transport. Report local and remote
checks separately, with exact SHA, counts, logs and gaps. No missing or old
result may be substituted for a new result. Developer evidence ends at the G1
review handoff; independent G1 acceptance and guarded merge/post-merge remain
separate. No S2 recovery, S3 mapping, S4 controller, G6/live, W1, durable
exactly-once or real concurrent-tab guarantee is claimed.
