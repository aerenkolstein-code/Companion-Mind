# Connector Alpha local qualification

Work order: CA-01, Windows manual single-document read connector.
Reviewed: 2026-09-22. Code under test:
`36147c7` (bound Desktop-client token exchange follow-up).

The bounded implementation, synthetic checks, and one approved live validation
pass. This report is sanitized for the code repository; it contains no real
identity, client configuration, document ID/content, credential, raw receipt,
or content fingerprint.

## Scope and acceptance

CA-01 implements a manually triggered Windows package CLI for one explicitly
bound, non-sensitive Google Doc: dedicated app/identity enrollment, bounded
read-only requests, structured local output, separate evidence, and credential
cleanup. It prohibits cloud business writes, scans, background synchronization,
credential fallback, production deployment and remote content inference.

The implementation owner supplies runnable code and synthetic evidence; the A1
lead independently checks it and records acceptance. The approved validation
also completed live authorization, the bounded single-document read, revocation,
and old-grant rejection. This is a limited qualification of that approved
dedicated test path, not an authorization for production or another live run.

## Executed checks

- `python -m unittest discover -s tests -p 'test_connector_alpha_*.py' -v`:
  **72 tests passed**, with fake Google responses, no Google network calls and
  in-memory credential fixtures. Loopback tests use an actual localhost server.
- `python tools/connector_alpha_native_probe.py`: **passed** on Windows build
  10.0.26200, using generated synthetic values and dedicated disposable slots.
  Actual SID/session/desktop checks, native Credential Manager write/read/delete,
  cross-process consumed-grant denial, cleanup access and wrong-SID rejection
  passed. Both generated token and ledger slots were removed and absence checked.
  This is native synthetic evidence, not live-provider evidence.
- Reviewed fixes: explicit revoke reports failure through its exit code; a gate
  failure before the first read persists closure; changed binding renewal has a
  documented, tested clean new-installation procedure. See
  [binding renewal](connector_alpha_binding_renewal.md).
- Reader compatibility: Google only returns Docs `revisionId` to users with edit
  access. The reader path retains read-only access and requires identical complete
  D0/body/D1 response fingerprints plus matching Drive version metadata within
  the same six-GET and response budgets. Evidence labels the absent revision
  `NOT_RETURNED` and atomicity `UNKNOWN`. Drift and inconsistent revision presence
  are rejected. [Google Docs API reference](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents).
- One approved live, manual validation completed its bounded path: callback
  acceptance, exactly one token exchange and one identity GET, then a successful
  six-GET document transaction. The reader returned supported text; Drive version
  was stable; Docs reader revision was `NOT_RETURNED`; and the three reader
  response fingerprints agreed. The resulting observation is `AS_OF` with
  atomicity `UNKNOWN`, not a snapshot guarantee.
- Cleanup after that live read confirmed local closure, remote revocation, and
  dedicated token deletion. An independent subsequent attempt on the same grant
  was denied as `GRANT_NOT_ACTIVE`, made zero Google reads, and produced no
  content output. Native verification also found the dedicated token slot absent
  and lifecycle ledger closed.

## Unverified and excluded

Physical Windows lock/unlock remains **NOT RUN**. The live validation did not
exercise a lock or unlock during consent or reading, so point-in-time desktop
gating is not a continuous lock-monitoring claim. Provider propagation beyond
the observed revocation result was not independently measured.

The legacy Unix shell is not claimed Windows-compatible, and this work does not
claim a full Windows port of prior runtimes. Windows user/admin trust,
point-in-time desktop checks, and supported-document-text coverage remain
explicit limitations. No full legacy regression rerun, production permission,
or production deployment is claimed.
