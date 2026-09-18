# ADR-C1-003 — Terminal stabilization

Decision: explicit terminal observable **plus** mutation stability; candidate
**T_stable_ms = 500**, derived only from synthetic calibration CAL-S0-01.

Observable in this fixture contract: assistant `data-c1-state` and
`data-c1-terminal` agree on complete/partial/failed, streaming is absent, and
`data-c1-stable-ms` meets the candidate interval. A missing or contradictory
observable fails closed; silence alone is not complete. A below-threshold
observation is STABILIZING with no terminal candidate. Streaming is control
state only. UNKNOWN never becomes a canonical terminal status. A failed marker
with usable text is AMBIGUOUS_TERMINAL. Future correction of an already-ingested
partial event must use the published contract, not overwrite it.

Calibration is recorded in the F01–F20 manifest, not measured on a real vendor:

| Trace | Mutations (ms) | First terminal observable | Post-terminal gaps |
|---|---|---|---|
| Synthetic A | 0, 200, 550, 1000 | 200 | 350, 450 |
| Synthetic B | 0, 100, 350, 750 | 100 | 250, 400 |

Algorithm: max observed post-terminal mutation gap (450) + frozen safety margin
(50) = 500 ms. G0-22 executes it and rejects nonmonotonic calibration; F03 checks
streaming and the 499/500 ms boundary. The candidate avoids early finalization
between the specified synthetic mutations only. It is not a universal upper
bound on future changes, latency or vendor behavior.

S0 does not implement timers, observers or a controller. Fixture-supplied elapsed
time is not a browser measurement. S4 must implement/reset the mutation timer
and qualify a 50-turn synthetic controller suite; S6/S7 require separately
authorized real vendor calibration and 20-turn acceptance. Any profile change
requires a versioned fixture/fingerprint update. No live observation was used.
