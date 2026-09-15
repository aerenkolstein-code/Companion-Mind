"""A1 internal conformance runner and synthetic fixture helpers; not A2 verdict."""
from __future__ import annotations

import argparse
from copy import deepcopy
import io
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from companion_mind.journal.codec import CONTRACT_COMMIT, SCHEMA_SHA256, fingerprint


def pair(number=0, *, session="synthetic-a019"):
    user = json.loads((ROOT / "tests/fixtures/canonical_event_v1/01_owned_client_user_text.json").read_text())
    user.update(event_id=f"{session}-user-{number}", session_id=session, turn_id=f"turn-{number}",
                sequence_no=number * 2, message_id=f"message-user-{number}", content_payload={"text": f"synthetic input {number}"})
    assistant = deepcopy(user)
    assistant.update(event_id=f"{session}-assistant-{number}", actor_role="assistant", sequence_no=number * 2 + 1,
                     message_id=f"message-assistant-{number}", content_payload={"text": ""},
                     provider="offline-stub", model="deterministic-v1")
    return user, assistant


def turn_request(number=0, **kwargs):
    user, assistant = pair(number, **kwargs)
    return {"op": "turn", "user": user, "assistant_template": assistant,
            "attempt_id": f"attempt-{number}", "script": {"frames": ["visible one\n", "visible two\n"], "outcome": "complete"}}


def seam(directory, request, *, fault=None, replica=None):
    command = [sys.executable, "-m", "companion_mind.journal", "--store", str(directory)]
    if replica is not None:
        command += ["--replica", str(replica)]
    if fault is not None:
        command += ["--fault", fault]
    return subprocess.run(command, input=json.dumps(request), text=True, capture_output=True, cwd=ROOT, timeout=20)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_journal.py")
    from test_journal import RECEIPTS
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    receipt = {"work_order": "ENG-A019-P1-01", "candidate_commit": head,
               "working_tree_dirty": dirty, "contract_commit": CONTRACT_COMMIT, "schema_sha256": SCHEMA_SHA256,
               "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
               "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
               "a1_internal_conformance": "PASS" if result.wasSuccessful() else "FAIL",
               "e1": "NOT_RUN_REQUIRES_INDEPENDENT_A2_AND_BOARD", "provider_live_calls": 0,
               "drive_evidence": "OFFLINE_DRIVE_STUB", "receipts": RECEIPTS,
               "normalized_result_fingerprint": fingerprint(RECEIPTS)}
    Path(args.output).write_text(json.dumps(receipt, indent=2) + "\n")
    sys.stderr.write(stream.getvalue())
    print(json.dumps(receipt, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
