# P3-S2 offline Docs read guard v1

Authority: WO-A1-A029-P3S2-DOCS-READ-GUARD-PROTOTYPE-01 v0.1, Issue #38.
Profile: `p3s2-docs-read-guard/1`.
Parent: Issue #37 R1 documentary preflight. Provider capability may include write;
this execution profile still denies every write before credential dereference.

This is an offline, public-safe synthetic prototype. It has no Google HTTP
transport, OAuth, real credentials, model invocation or business write API.
The `SyntheticBroker` simulates availability, locking and opaque handle lifetime;
it does not qualify an OS-native SecretStore. No runtime TestPort, shell route,
shared policy, dependency or CI workflow is changed.

## Trusted setup and untrusted entry

Trusted setup constructs `Binding`, `SyntheticBroker`, `OfflineDocsFixture`,
`ReadGuard`, its clock and its private control directory. Fixture resources and
any race hooks are synthetic test inputs. `execute(request)` is the untrusted
entry: it validates exact fields and fails closed before any adapter probe.
Python reflection, code replacement, a malicious host administrator and arbitrary
filesystem rollback are outside this in-process prototype's trust boundary.
The exact fixture types are enforced; callers cannot inject a transport subclass.

`Binding` includes provider/app/client/project/access_subject/universe/resource_id/
MIME/purpose/credential_domain/grant_generation/policy_version/not_before/expires_at.
All are explicit. Provider must be `synthetic-google-docs`; MIME must be
`application/vnd.google-apps.document`; profile must equal the version above.
Missing, UNKNOWN, invalid, mismatched, old, future or expired bindings are denied.
This proves a concrete *synthetic* G01 binding; no real principal/app grant is
inferred from the account used to read or archive governance documents.

A request contains `action`, the full `binding`, integer `epoch`, opaque `handle`,
and optional `require_fresh` / `resume_handle`. No URL, HTTP method, arbitrary
path, field mask, transport, callback, permission decision or override is accepted.
The convenience `request()` creates a fixture request; it does not bypass checks.

## C01–C08 implementation mapping

All implementation symbols below are in
`companion_mind/owned_home/docs_read_guard.py`.

| Contract | Code | Mechanical proof |
| --- | --- | --- |
| C01 identity | `Binding`, `ReadGuard._validate`, `_live` | RG-T01–07, T20 |
| C02 exact READ surface | `_FixtureRead`, `_exchange`, `OfflineDocsFixture._read` | RG-T01–04, T10, T19 |
| C03 write DENY | `WRITES`, `_validate`; default action DENY before `_lease` | RG-T09–11 |
| C04 resource/subject isolation | full binding equality; resource-bound broker lease; response identity check | RG-T02–06, T10, T15 |
| C05 secret boundary | `OpaqueHandle`, `SyntheticBroker`, `_validate`, `_check_response`; sanitized errors | RG-T10, T12–13, T17 |
| C06 revoke/generation | `revoke`, `install_generation`, `_save_lifecycle`, `_live`, delivery lock | RG-T06–08, T18, T20 |
| C07 freshness/UNKNOWN | `_check_response`, explicit AS_OF cache path | RG-T14–18 |
| C08 receipt | `_receipt`, `execute`; no business Authority mutation path | RG-T01, T08–10, T12, T15–20 |

## READ and write enforcement

Only the `read` action dispatches. The server constructs exactly three fixture
exchanges: metadata-before, body, metadata-after, all for the bound resource.
There is no list/search/children/changes/follow-link/redirect path. Even another
resource present in the provider-granted fixture is denied by this capability.
The metadata schema is fixed to id, MIME, version, modified_at and trashed; the
body schema is fixed to id, MIME, version, revision, coverage and text.
Unexpected response fields do not create a new API surface.

edit/append/create/copy/move/delete/share/permission/permission_mutation/
batchUpdate/documents.batchUpdate/drive.write/write/synthetic.reversible_write
are denied explicitly. Every other action is denied by default. A request cannot
relabel its tier or present a generic P2 ALLOW decision. The existing generic P2
policy remains untouched and continues to allow its own valid synthetic writes;
the RG-T11 cross-profile test proves it cannot authorize this profile's writes.

## Credential and egress boundary

The broker holds one explicitly synthetic canary in memory. Handles contain no
credential bytes and are authorized by object identity plus binding digest.
Unknown or copied handles cannot be dereferenced; pickle/copy/deepcopy exports
are denied. Read leases bind the resource, binding digest and revoke epoch and
are removed after the request. Public requests cannot call lifecycle methods.

Unavailable, locked or non-boolean availability states fail closed, including
the historical cache path. There is no environment/file/plaintext fallback.
The canary test places synthetic secret text in hostile input, provider output
and errors, then scans output/context/trace/journal projections, stdout, stderr,
logging and control files. Raw provider errors are reduced to fixed reason codes.
Recognized credential patterns in body text are also rejected. This is not a
claim to detect every unknown, unlabelled secret in arbitrary real documents.

## Revocation, renewal and restart

The existing `AtomicState` supplies exclusive process ownership, fsync, atomic
replace and directory fsync. The prototype adds a separate control snapshot with
only format, binding digest, grant generation, epoch and revoked flag. No body,
credential, prompt, source text or business Authority is persisted there.

`revoke()` sets revoked, increases epoch and durably commits before returning or
releasing any response. It then invalidates broker handles/leases and the cache.
A failed/unknown remote result cannot reopen access; the argument is only a
synthetic label and causes no remote call. Dispatch and final result publication
are linearized against lifecycle changes with a reentrant lock. The test hook
models pending completion outside the dispatch lock. Revocation after the body
or immediately before final delivery discards the old result, with no text or
resume handle returned. Data delivered before a later revoke cannot be retracted.

`install_generation()` is an explicit trusted regrant, never a request action.
It permits only a strictly higher generation and validity change, preserving
identity; it durably advances the epoch before issuing a new opaque handle.
Old requests and handles cannot cross it. Restart accepts only the exact durable
binding/generation. Cache/resume handles are not persisted and cannot silently
become fresh success after restart. Crash fixtures kill a child after durable
revoke or generation replacement and verify recovery in a new process.

Missing state in an existing directory, malformed state, mismatched binding and
concurrent ownership fail closed. A persistence failure blocks the current
guard and invalidates handles; it is not acknowledged as a durable transition.
Disk failure recovery and host-level snapshot rollback require separate
qualification. Deleting the entire trusted state directory is not a supported
automatic reset/recovery action. OS-native credential persistence, external
provider revoke propagation and detection latency remain UNKNOWN / NOT RUN.

## Freshness and results

SUCCESS requires matching metadata-before/body/metadata-after versions and
identity, a nonempty reader revision, valid observation timestamps, non-trashed
state, FULL coverage and the same live binding/epoch at delivery. It returns
`freshness=AS_OF`, `observed_at`, `as_of`, version, revision and content digest.
It never claims CURRENT. The synthetic fixture supplies an explicit body version;
this does not establish atomicity across real Google APIs.

| Condition | Result |
| --- | --- |
| Metadata/body version mismatch | STALE / VERSION_CHANGED |
| Missing version or reader revision | UNKNOWN; no privilege escalation |
| Missing/extra fields, incomplete coverage, invalid content | UNKNOWN; no body delivery |
| Wrong returned resource/MIME or trashed resource | DENY |
| 403 | DENY / ACCESS_DENIED_403 |
| 404 | UNKNOWN / NOT_FOUND_OR_UNREADABLE_404; no nonexistence claim |
| Timeout or unclassified provider error | UNKNOWN; no automatic retry |
| Secret canary in provider data | BLOCKED / SECRET_OUTPUT_BLOCKED |
| Old epoch/generation/expired during request | DISCARDED; no text/resume handle |
| Resume when a fresh read is required | UNKNOWN / FRESH_READ_REQUIRED |
| Explicit historical resume with a valid capability | AS_OF / CACHED_AS_OF, original observation time |

No result changes Current, Agenda, Persona, Relationship or Canon. A new fresh
read must be an explicit new request. There is no account switching, scope
upgrade, automatic retry or fallback to official Web to manufacture success.

## Reproduce

From the repository root with Python 3.11+ (no new dependency):

```bash
python tests/test_owned_home_docs_read_guard.py --receipt
python -m unittest discover -s tests -v
python tools/a019_conformance.py --output /tmp/a019-read-guard-regression.json
node tests/test_chatgpt_recovery_verify.mjs
node tests/test_chatgpt_recovery_reconcile.mjs
```

Run the installed CLI demo/replay/validate-mitigation checks from the existing
`.github/workflows/test.yml` as well. Existing PR CI automatically discovers the
new unittest file on Python 3.11. Its checkout may be a shallow merge commit;
protected-tree verification does not depend on parent history being available.
The standalone `--receipt` JSON records actual HEAD/tree, cleanliness, all 20
case results and network guard observations. Do not label a dirty run as evidence
for a later commit. Run focused and full regression again on the committed head.

The focused suite intercepts socket creation/DNS/connections, urllib open and
HTTP(S) connections; any attempt is a test failure, including attempts swallowed
by error handling. Crash children install the same guard. The wider legacy suite
also tests its separately authorized loopback interfaces; that is not Google
Live or a prototype transport. No real credential material is used.

## RG test mapping

All tests are in `tests/test_owned_home_docs_read_guard.py`.

| ID | Test suffix / primary assertion |
| --- | --- |
| RG-T01 | exact_authorized_read: bound read, three server-built exchanges |
| RG-T02 | wrong_id: zero credential/adapter activity |
| RG-T03 | same_name_different_id: display name cannot authorize |
| RG-T04 | provider_granted_neighbor: present fixture resource still denied |
| RG-T05 | all_identity_dimensions: 30 mismatched/unknown/null bindings |
| RG-T06 | generation_and_expiry: type, old generation, exact expiry, not-before |
| RG-T07 | revoke_before_dispatch: durable epoch, remote failure cannot reopen |
| RG-T08 | revoke_inflight_and_before_delivery: two real thread races |
| RG-T09 | all_writes_before_credential_and_dispatch: complete denied action set |
| RG-T10 | unknown_action_and_endpoint_escape: URL/method/path/transport injection, forged handle |
| RG-T11 | generic_p2_allow_does_not_bleed: genuine legacy ALLOW does not grant this profile |
| RG-T12 | secret_canary_all_egress: input/output/error/export/file/log protections |
| RG-T13 | store_unavailable_locked_no_fallback: no env/file read; cache also blocked |
| RG-T14 | stable_metadata_body_metadata_asof: evidence-complete synthetic snapshot |
| RG-T15 | mismatch_and_wrong_response: body/metadata/identity/coverage/time negatives |
| RG-T16 | missing_reader_signals_unknown: absent version/revision/field preserved |
| RG-T17 | safe_error_classes: 403/404/timeout, no blind retries |
| RG-T18 | cache_never_fresh_and_revoke_invalidates: no freshness laundering |
| RG-T19 | authorities_and_network_unchanged: canaries, immutable business files, exact scope |
| RG-T20 | crash_restart_lifecycle: process death, durable epochs/generation, damaged-state denial |

## Repository boundary and acceptance ceiling

Base SHA: `14f0e9cf00413a8ac3ad902b3b9c5641616970a1`.
Base tree: `529eab1585e2598a6da4c846a56e14c743f515c0`.
Exactly five paths are in this implementation surface: the module, this document,
its new test, and the two existing scope tests
`tests/test_owned_home_p3s1_conformance.py` /
`tests/test_owned_home_slice1_conformance.py`.
Those existing tests receive only three exact new-path allowances; their protected
tree values and assertions are retained. RG-T19 independently reconstructs the
tree outside these five paths and compares it with
`346296033d24dbd5f521b120f4c0a1479cd54ded`.

PASS means offline prototype conformance only, ready for independent exact-head
review under Issue #38. It is not independent acceptance, merge authorization,
real G01 grant/identity proof, a qualified OS SecretStore, real provider freshness
or revocation proof, final P3D-009 selection, Plan ACCEPT, OAuth or Live. Preserve
those real-world UNKNOWNs and stop for the authorized review/decision chain.
