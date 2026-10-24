# C2 RAW-only collector - bounded engineering milestone

Date: 2026-10-24.

## Scoped evidence

| Test or state | Outcome |
|---|---|
| P0/P1/P2 synthetic offline tests | 21 / 21 PASS |
| Non-authoritative pending tests | 7 / 7 PASS |
| One real temporary-chat automatic capture | PASS (user + assistant turn) |
| Local bounded history backfill | Prefix preserved; continuous overlap chain |
| Versioned ChatGPT internal network schema | NOT AVAILABLE |
| Long-term unattended coverage | UNKNOWN |
| Cross-device, closed-tab, sleep and restart coverage | UNKNOWN |
| In-progress or conflicting latest reply | HOLD |
| L0 / Journal / cloud downstream fan-out | NOT ENABLED |
| Independent acceptance | NOT CLAIMED |

This branch records only an engineering milestone. It is not Current, Journal,
Memory, L0, or Persona authority. No private RAW, personal content, credentials,
production absolute paths, or internal archival URLs are in this publication.

Code is a public-safe derivative rather than a byte-identical copy of the
local Windows deployment. Synthetic tests were rerun on the derivative.
Publication does not install or activate anything. It does not change the
accepted status of the previously published read-only C2 prototype.

## Remaining risk

The network parser is dependent on unsupported web implementation details.
DOM can disagree with earlier network candidates, and neither presence of a
live listener nor a successful one-turn canary implies full chronology.
Those cases stay HOLD, with pending evidence kept non-authoritative.

Do not run this together with any other RAW writer to the same sessions
directory. Any long-term multi-chat unattended operation or P3 fan-out
requires separately scoped validation and authorization.

## 2026-10-24 addendum: conflict correction

A scoped same-message P2/DOM conflict reconciliation module was added. It
requires a known history anchor and two separated stable, final DOM rounds.
The candidate is written solely by P0; each successful resolution emits
explicit status/evidence and leaves older RAW messages unchanged.

- New synthetic conflict regression: 21 / 21 PASS.
- Existing offline / PENDING regressions: 21 / 21 and 7 / 7 PASS.
- Combined synthetic regression: 49 / 49 PASS.
- Local production installation and read-only post-install observation: PASS.
- Real new conflict resolved after this patch: NOT YET OBSERVED.
- L0, Journal, boot guard and main branch: unchanged by this addendum.

The released code is an archival snapshot only. No claim of complete cloud
history, independent acceptance, or new live conflict repair is made.