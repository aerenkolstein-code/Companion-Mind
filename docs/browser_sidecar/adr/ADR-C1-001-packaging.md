# ADR-C1-001 — Packaging

Decision: **SELECT_B — browser extension + localhost sidecar** for later stages.
This freezes direction, not an installed package, transport or permission grant.

| Criterion | A: extension only | B: extension + localhost sidecar |
|---|---|---|
| A019 integration | Existing Python Journal cannot execute directly in extension JS; a new implementation or external port would be required | Can reuse existing Python ingest seam in a local process |
| Crash/restart | Must recover across browser/service-worker termination | Local recovery buffer can outlive an extension worker; reconnection still needs testing |
| Permission surface | Smaller native footprint, but still needs a governed canonical delivery route | Adds a separately approved local boundary; exact origin/extension, bounds and authentication required |
| Worker lifecycle | Volatile worker state cannot be treated as durable | Worker remains disposable; durable delivery state stays local |
| Windows/Linux | Browser-facing code may be portable; A019 connection remains unresolved | Linux A019 reference is usable; its POSIX dependencies do not establish Windows compatibility |
| Testability | Browser harness needed for actual lifecycle | Pure parser tests now; local fake transport and fault injection later |
| Secret boundary | Scrub before browser persistence | Scrub before either process persists; no unsanitized durable transit copy |
| Rollout complexity | Simpler package but unresolved canonical seam | Two components, uninstall/recovery/origin checks and independent acceptance needed |

Selection B avoids forking the frozen A019 contract into a second JS Journal.
It does **not** select a general listening HTTP server, install a native host,
add an extension permission or run a local service in S0. Transport choice and
security checks belong to subsequent authorized stages. Windows native A019
support is NOT_EVALUABLE here; any needed A019 changes require a separate order.

Chrome documents that service workers may terminate and global variables are
not durable; this motivates restart-safe state, not a live compatibility claim.
See [service-worker lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle).
Chrome's [native messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)
is a possible later local-process transport with an explicit host registration
and extension allowlist. It is a comparison reference only, not a chosen or
authorized implementation. These conclusions combine official API constraints
with the inspected repository's Python/POSIX seam.

Rejected: A as the first implementation direction while its canonical A019
delivery route is unresolved. Revisit through Plan/Architecture if B cannot fit
the existing permission ceiling. No S1–S8 work is authorized by this ADR.
