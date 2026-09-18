# ADR-C1-002 — Staging medium

Decision: **local SQLite recovery buffer**, for the later sidecar selected in
ADR-C1-001. No staging database/engine is implemented in S0.

Choose an application-local directory outside browser sync and user-synced cloud
folders. The future store is a bounded delivery buffer, not a canonical ledger.
SQLite transactions support atomic updates of identity, sanitized payload and
delivery state without requiring a parallel append-only RAW authority. A file
spool would require another crash-atomic indexing/recovery protocol; IndexedDB
would keep recovery coupled to the browser and still need the A019 handoff.

Required record: stable capture_id, source_event_key, normalized payload
fingerprint, profile version/fingerprint, sanitized payload, pending/delivery
state and exact A019 receipt reference. Persist only after scrub. Reopen must
reconcile pending captures and repeat the identical A019 ingest identity.
No receipt means no A019_COMMITTED. Identity/fingerprint conflicts stop; no
last-write-wins. Only confirmed A019 delivery permits governed compaction.

**Staging is NOT Authority.** It must not export itself as Canonical RAW, accept
Current/Persona/Relationship/Canon updates or write Google Drive. A019 remains
the sole canonical persistence/replica owner. Buffer receipt references do not
create a second truth source. No browser cloud sync, credential copies or
unsanitized durable duplicate are permitted.

Chrome distinguishes local and sync storage in its
[storage API](https://developer.chrome.com/docs/extensions/reference/api/storage).
That distinction supports the explicit no-sync requirement; it does not prove
any proposed SQLite path is outside cloud backup. Future S2 must verify the
actual location, permissions, fsync/crash/reopen behavior and retention policy
on each supported OS. Windows portability remains separately unverified.
