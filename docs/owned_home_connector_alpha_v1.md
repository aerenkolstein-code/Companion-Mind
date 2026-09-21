# Connector Alpha Windows Manual Read Contract

Connector Alpha is a manually operated Windows-only path for one named,
non-sensitive Google Doc. It is not a browser-cookie integration, directory
browser, synchronizer, write capability, production service, or background
agent.

The operator supplies a non-secret binding JSON containing the dedicated test
application, named Google subject, Windows SID, one file ID, expiry, and three
explicit boundary confirmations. Unknown, mismatched, expired, or non-test
bindings are refused before a credential is dereferenced.

`authorize` binds its local loopback listener before it launches the system browser for a desktop PKCE flow. The callback
is accepted only on the local loopback port with the original state, the exact
`drive.file` scope, and the exact single Picker file ID. The authorization code
and access token are never written to terminal output, receipts, logs, config,
or repository files. A successful token exchange is written only to the
current-user Credential Manager target derived from
the full binding digest. There is no environment, plaintext file, cookie, or
connector credential fallback.

The first successful authorization also uses that same token for a fixed
identity check. It verifies the configured test email and records the observed
Google permission ID in the explicit non-secret binding file. A binding that has
not completed this enrollment cannot read.

`read` performs one fixed six-GET transaction: identity, Drive M0, full Docs
D0, full Docs body, full Docs D1, Drive M1. It has no caller-configurable URL, method, endpoint,
redirect, retry, refresh, list, search, or write operation. Each request has
fixed Google hosts and paths, HTTPS, an eight-second connection timeout, a
twelve-second response deadline, per-response two MiB cap, cumulative four MiB
cap, and a forty-five-second read deadline. The body is released only when the
principal, file, MIME, Drive version, Docs revision when reader-visible, and final local gate agree.
Google Docs exposes `revisionId` only to editors. For the approved reader-only
test account, a missing revision is handled honestly: D0, body, and D1 must
have matching safe fingerprints while Drive M0/M1 versions match. That result
is an `AS_OF` observation over this read window, not a claim of provider
atomicity or of a current snapshot.
The evidence receipt contains counters and status only; structured content is
written solely to the operator's explicit `--content-out` local path.

The operator flow is one manual `authorize`, one manual `read --content-out
RESULT.json`, then automatic cleanup. Read delivery is a separate final gate:
the content file is written only after the final grant/lifecycle check. Whether
the read succeeds or fails, the command closes local authorization, attempts
the one remote revoke, and deletes the dedicated credential; the receipt reports
each cleanup state independently. A new read requires a new authorization.

`revoke` closes the local session gate and attempts deletion of the dedicated
Credential Manager target even if a credential is unavailable. When a token was
already acquired for cleanup it makes one fixed Google revoke request. A remote
failure cannot reopen the local session, and the receipt distinguishes remote
confirmation from unknown remote revocation.

The legacy Owned Home synthetic runtime imports Unix `fcntl` and is not claimed
Windows-compatible by this connector. Connector Alpha's package CLI is the
actual Windows entrypoint; no claim is made for the legacy shell.

## Current qualification evidence and limits

The native synthetic canary (`tools/connector_alpha_native_probe.py`) has
passed current-SID, active-console, Default input-desktop, session Credential
Manager write/read/delete, one-use read lifecycle, fresh-process denial while a
read is in progress, cleanup-only token access after closure, and wrong-SID
rejection. It uses only generated test slots and reports zero Google calls.

The physical Windows lock-screen check is **NOT RUN**. No real OAuth consent,
Google document read, provider revocation, or real account/application/file
binding has been run. The automated CLI and transport/session tests use fake
Google responses and in-memory stores; they are not live-provider evidence.

## Manual operator sequence

Create a non-secret binding JSON with the dedicated client and test file. The
initial enrollment value is explicitly `null`; it is not a substitute ID:

```json
{"installation":"owned-home-win","client_id":"123-example.apps.googleusercontent.com","project_id":"dedicated-test-project","app_name":"Connector Alpha","subject_email":"approved-test@example.invalid","subject_permission_id":null,"owner":"owned-home","universe":"owned-home","purpose":"p3s2-single-read","file_id":"approved-single-file-id","windows_sid":"S-1-5-...","expires_at":"2026-12-31T23:59:59+00:00","dedicated_test_app":true,"non_sensitive_test_file":true,"revoke_project_grant_authorized":true}
```

From the repository root on the designated Windows host, use only manual
commands. The first command opens the system browser after the loopback listener
has bound; it enrolls the observed permission ID into the same non-secret JSON.

```powershell
python -m companion_mind.connector_alpha.cli --config .\connector-alpha-binding.json authorize
python -m companion_mind.connector_alpha.cli --config .\connector-alpha-binding.json --content-out .\connector-alpha-result.json read
python -m companion_mind.connector_alpha.cli --config .\connector-alpha-binding.json revoke
```

The access token is session-only in Credential Manager. The lifecycle ledger is
non-secret but persistent in a separate Credential Manager slot so a crash or
second process cannot resume a consumed read grant. The `read` command already
performs cleanup; `revoke` is the manual recovery command when a prior command
did not finish cleanup. Do not copy authorization codes, callback URLs, tokens,
or browser cookies into the terminal or config file.
