# Connector Alpha local qualification

Work order: CA-01, Windows manual single-document read connector.
Reviewed: 2026-09-21. Code under test:
`b368feb4e8da67faaf0e6c6bf6c8159cd73c24e0`.

The bounded implementation and the executed local checks pass. **The work order
and Connector Alpha stage remain open pending live validation.** This report is
sanitized for the code repository; real identities, client configuration,
document IDs/content and credentials are maintained separately on the host.

## Scope and acceptance

CA-01 implements a manually triggered Windows package CLI for one explicitly
bound, non-sensitive Google Doc: dedicated app/identity enrollment, bounded
read-only requests, structured local output, separate evidence, and credential
cleanup. It prohibits cloud business writes, scans, background synchronization,
credential fallback, production deployment and remote content inference.

The implementation owner supplies runnable code and simulated evidence; the A1
lead independently checks the implementation and records acceptance. Full stage
acceptance additionally requires live authorization, the approved single-document
read, revocation, old-grant rejection, and honest qualification limits. Preparation
or simulated success alone does not meet those criteria.

## Executed checks

- `python -m unittest discover -s tests -p 'test_connector_alpha_*.py' -v`:
  **42 tests passed**, with fake Google responses, no Google network calls and
  in-memory credential fixtures. Loopback tests use an actual localhost server.
- `python tools/connector_alpha_native_probe.py`: **passed** on Windows build
  10.0.26200, using generated synthetic values and dedicated disposable slots.
  Actual SID/session/desktop checks, native Credential Manager write/read/delete,
  cross-process consumed-grant denial, cleanup access and wrong-SID rejection
  passed. Both generated token and ledger slots were removed and absence checked.
- Reviewed fixes: explicit revoke reports failure through its exit code; a gate
  failure before the first read persists closure; changed binding renewal has a
  documented, tested clean new-installation procedure. See
  [binding renewal](connector_alpha_binding_renewal.md).

## Unverified and excluded

Physical Windows lock/unlock, real OAuth consent, Google document reading,
provider revocation and propagation remain **NOT RUN**. The live path waits for
explicit scope confirmation and user consent. A browser login or local storage
of a client configuration is not live-provider qualification.

The legacy Unix shell is not claimed Windows-compatible. Windows user/admin
trust, point-in-time desktop checks and supported-document-text coverage remain
explicit limitations. No full legacy regression rerun or production deployment
is claimed. The next stage must not start based on this local-only report.
