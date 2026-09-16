"""Versioned, offline-only JSON-in/JSON-out A2 black-box seam.

python -m companion_mind.journal --store DIR [--replica DIR] [--fault F1]
One JSON operation on stdin; one JSON response on stdout. Failure exit=2,
hard crash exit=86. No database inspection or provider network is needed.
"""
import argparse
import json
import sys

from .codec import CONTRACT_COMMIT, CONTRACT_VERSION, SCHEMA_SHA256, JournalError, encode, fingerprint
from .replica import OfflineDrive
from .store import FAULTS, STORE_VERSION, Journal, StubScript


def execute(journal, request, replica_directory=None):
    operation = request["op"]
    if operation == "append":
        return journal.append(request["event"])
    if operation == "ingest":
        return journal.ingest(request["adapter"], request["event"])
    if operation == "correct":
        return journal.correct(request["event"])
    if operation == "turn":
        script = request.get("script", {})
        return journal.append_user_then_invoke_stub(
            request["user"], request["assistant_template"], attempt_id=request["attempt_id"],
            script=StubScript(tuple(script.get("frames", ["synthetic response"])), script.get("outcome", "complete")))
    if operation == "export":
        events = journal.export(order=request.get("order", "canonical"))
        return {"events": events, "fingerprint": fingerprint(events)}
    if operation == "recover":
        return journal.recovery_receipt
    if operation == "attempts":
        return journal.attempts()
    if operation == "replica_state":
        return journal.replica_state()
    if operation in {"drain", "readback"}:
        if replica_directory is None:
            raise JournalError("REPLICA_REQUIRED")
        remote = OfflineDrive(replica_directory, target=journal._target,
                              fail=bool(request.get("fail", False)), corrupt_read=bool(request.get("corrupt_read", False)))
        try:
            if operation == "drain":
                return journal.drain_replica(remote)
            return {"transport_evidence": remote.evidence_kind,
                    "events": [json.loads(body) for body in remote.snapshot()]}
        finally:
            remote.close()
    if operation == "info":
        return {"seam_version": "a019-offline/1", "store_version": STORE_VERSION,
                "contract_version": CONTRACT_VERSION, "contract_commit": CONTRACT_COMMIT,
                "schema_sha256": SCHEMA_SHA256, "offline_only": True,
                "provider_exactly_once": False, "recovery_complete": True}
    raise JournalError("UNSUPPORTED_OPERATION")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--replica")
    parser.add_argument("--replica-target", default="offline-drive")
    parser.add_argument("--fault", choices=sorted(FAULTS))
    args = parser.parse_args()
    try:
        payload = sys.stdin.buffer.read(2_097_153)
        if len(payload) > 2_097_152:
            raise JournalError("REQUEST_TOO_LARGE")
        request = json.loads(payload)
        with Journal(args.store, replica_target=args.replica_target, fault=args.fault) as journal:
            result = execute(journal, request, args.replica)
        print(encode({"ok": True, "result": result}))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, JournalError) else "REQUEST_FAILED"
        print(encode({"ok": False, "error": code}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
