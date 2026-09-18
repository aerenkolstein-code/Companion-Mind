# ADR-C1-004 — P01–P15 proof routes

Decision: freeze the companion `proof-test-mapping.json` with all fifteen
Architecture obligations. Each row identifies current owner, executable S0
test method names, positive and negative checks, current evaluability, later
test IDs and remaining live dependencies. G0-23 checks complete unique coverage
and resolves every S0 method against the actual unittest class.

Static guards are evidence only for their named scope. For example, S0 proves
that no parser path calls A019 or falsely marks a commit; it does not prove
live A019 delivery, cross-process staging or crash recovery. The offline seam
test uses an unchanged published fixture in a temporary Journal and is not a
new C1 mapper. Later IDs are planned executable routes owned by their stated
stages, not tests falsely reported as already run. None permits S1–S8 before G0
acceptance and separate authorization.

G0-01…27 are developer offline checks. Their success and Draft PR CI are a
READY_FOR_G0_ACCEPTANCE return package; independent G0 acceptance remains a
separate verification/Board action. Technical completion and W1 remain false.
