# S0 developer validation and approved scope amendment

The user explicitly approved the focused repository-scope amendment after the
first NOT_READY return. The amendment is implemented. Current full-repository
regression, exact head/tree and GitHub Actions results are bound in the
[Draft PR #34 return package](https://github.com/aerenkolstein-code/Companion-Mind/pull/34).
Independent G0 acceptance remains separate; no merge, S1 or live permission
follows from this implementation.

## S0 evidence

`s0-offline-receipt.json` records all 27 executable G0 checks passing after the
amendment, including 100 parse/replay cycles per snapshot, zero observed
synthetic sentinel leakage, zero duplicate amplification and the forbidden-source
scan. These results are limited to the declared static synthetic contract.
The 20 fixtures and VendorProfile retain their original fingerprints.

The 43 pre-existing readonly files remain byte-identical to `s0-baseline.json`.
The only additional existing-file edit is the explicitly approved preflight
predicate in `tests/test_owned_home_slice1_conformance.py`. No C1 parser,
normalizer, fixture, profile, A019, C2, Owned Home runtime, dependency or workflow
behavior changed as part of this amendment.

Local validation uses Python 3.12.14 and Node 24.19.0. The unchanged workflow
uses Python 3.11 and Node 20. A019 offline conformance, both C2 Node suites and
installed CLI demo/replay/mitigation validation passed locally after the amendment.
The post-amendment full-repository run passed **157/157 tests** in 111.997 seconds,
including the original four failing checks. The PR binds this result to the
published tree and records the separate exact-head CI run.

## Historical blocker

Before the amendment, the full-repository suite ran 157 tests with 4 failures
locally and in CI. The root failure was
`test_owned_home_slice1_conformance.Slice1Conformance.test_ts1_12_repository_preflight_and_repeatability`.
It accepted only the earlier A029 P2-S4 mutable paths and reconstructed HEAD
after removing only those paths. The new C1 additions changed the reconstructed
tree while the expected historical tree remained
`2d0ec0d73a703a1abb4024abda4f1aa6f4c43cfd`.

TS2-14, TS3-14 and TS4-14 invoked that same check and failed transitively.
The original failed head was `7d5306601fb68420ad582b3321e82f3803ea0cf8`;
[run 35355190236](https://github.com/aerenkolstein-code/Companion-Mind/actions/runs/35355190236)
preserves the historical CI evidence. These failures are not presented as the
current post-amendment result.

## Approved correction

The existing test's `permitted()` predicate now recognizes only these
additional work-order surfaces:

- `companion_mind/browser_sidecar/`
- `docs/browser_sidecar/`
- `tests/fixtures/c1_chatgpt_web_v0_1/`
- `tests/test_browser_sidecar_s0.py`

The exact historical protected-tree hash, the reconstruction algorithm, existing
allowed A029 files, dependency checks and all behavioral assertions are unchanged.
An AST comparison confirms all executable test code outside `permitted()` is
identical to the original PR head. Unrelated repository paths remain outside the
allowlist. No tests or workflow steps are skipped or disabled.

This amendment extends one existing test's recognized scope only. It does not
extend C1 beyond S0 or grant independent G0 acceptance. After full regression
and exact-head CI pass, the developer may return READY_FOR_G0_ACCEPTANCE and
stop at that handoff.
