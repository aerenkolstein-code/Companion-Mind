# Connector Alpha Windows Manual Read Contract

Connector Alpha is a manually operated Windows-only path for one named,
non-sensitive Google Doc. It is not a browser-cookie integration, directory
browser, synchronizer, write capability, production service, or background
agent.

The operator supplies a non-secret binding JSON containing the dedicated test
application, named Google subject, Windows SID, one file ID, expiry, and three
explicit boundary confirmations. Unknown, mismatched, expired, or non-test
bindings are refused before a credential is dereferenced.

`authorize` launches the system browser for a desktop PKCE flow. The callback
is accepted only on the local loopback port with the original state, the exact
`drive.file` scope, and the exact single Picker file ID. The authorization code
and access token are never written to terminal output, receipts, logs, config,
or repository files. A successful token exchange is written only to the
current-user, session-persistent Windows Credential Manager target derived from
the full binding digest. There is no environment, plaintext file, cookie, or
connector credential fallback.

`read` performs one fixed six-GET transaction: identity, Drive M0, Docs D0,
body, Docs D1, Drive M1. It has no caller-configurable URL, method, endpoint,
redirect, retry, refresh, list, search, or write operation. Each request has
fixed Google hosts and paths, HTTPS, an eight-second connection timeout, a
twelve-second response deadline, per-response two MiB cap, cumulative four MiB
cap, and a forty-five-second read deadline. The body is released only when the
principal, file, MIME, Drive version, Docs revision, and final local gate agree.
The evidence receipt contains counters and status only; structured content is
written solely to the operator's explicit `--content-out` local path.

`revoke` closes the local session gate and attempts deletion of the dedicated
Credential Manager target even if a credential is unavailable. When a token was
already acquired for cleanup it makes one fixed Google revoke request. A remote
failure cannot reopen the local session, and the receipt distinguishes remote
confirmation from unknown remote revocation.

The legacy Owned Home synthetic runtime imports Unix `fcntl` and is not claimed
Windows-compatible by this connector. Connector Alpha's package CLI is the
actual Windows entrypoint; no claim is made for the legacy shell.
