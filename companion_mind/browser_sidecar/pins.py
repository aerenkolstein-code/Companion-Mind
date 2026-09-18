"""Frozen S0 authority references; filesystem reads only, no contract fork."""

import hashlib
from pathlib import Path

from .normalize import CaptureError

ARCHITECTURE_ID = "1U8IRUWSq6AI3kTgdKbBmq0757ChxQe2DrMBAN_XatCU"
PLAN_ID = "1DwJmFrZH60dmf4XlHezmlAnBMJ4s9ZHkmX0Js97Zmis"
WORK_ORDER_ID = "ENG-C1-A018-S0-01"
BASE_SHA = "40389094886cd1f8570919459f38dc22083c0e37"
BASE_TREE = "3a0b5b5120c15504db10f41e11a0dc5997a779ee"
CONTRACT_VERSION = "canonical_event/v1"
CONTRACT_COMMIT = "63ac8d7de8eb35915cc291b0f7ea67c33b366922"
CONTRACT_HASHES = {
    "schemas/canonical_event_v1.schema.json": "37c8c6df4433b9bd070be6e8b07405b9a67e15dd5c93ed54c04251f4d36c9b1e",
    "companion_mind/contracts/canonical_event_v1.py": "016163c05eb08ffed33910047ec57a1d5772aede9157a5079187cc1781e48508",
}
ADAPTER = "A018"
PROVENANCE = ("browser_sidecar", "observed")


def verify_pins(repo_root, observed_base):
    """Caller supplies a fresh remote base; this offline function does not query git."""
    if observed_base != BASE_SHA:
        raise CaptureError("BASELINE_MOVED")
    for name, expected in CONTRACT_HASHES.items():
        try:
            actual = hashlib.sha256((Path(repo_root) / name).read_bytes()).hexdigest()
        except OSError:
            raise CaptureError("CONTRACT_UNAVAILABLE") from None
        if actual != expected:
            raise CaptureError("CONTRACT_CHANGE_REQUIRED")
    return {"architecture": ARCHITECTURE_ID, "plan": PLAN_ID, "authority_version": "0.1",
            "work_order": WORK_ORDER_ID, "base": BASE_SHA, "base_tree": BASE_TREE,
            "contract_version": CONTRACT_VERSION, "contract_commit": CONTRACT_COMMIT,
            "contract_hashes": dict(CONTRACT_HASHES), "adapter": ADAPTER,
            "source_kind": PROVENANCE[0], "observation_type": PROVENANCE[1]}
