"""OwnedHomeTestPort v1, one bounded JSON request on stdin; JSON result on stdout.

python -m companion_mind.owned_home.testport --store DIR [--fault POINT]

Request: {contract_version, scope:{universe_id,access_subject_id}, fixtures:[],
grants:[], op:turn|observe|resume|safe_export|wake|rebuild|info, ...}.
turn adds turn:{Turn fields} and optional resume:true. observe/resume add request_id;
resume reconstructs the original turn from A019, without client-held content.
wake adds candidate:{WakeCandidate fields}; rebuild adds source_id,version.
Fixtures/grants are trusted, explicit synthetic test setup, not UI input.
No production source/credential adapters or internal database operations exist.
Exit 0=receipt, 2=safe refusal, 86=declared subprocess crash; no auto-resume.
"""
import argparse
import json
import sys

from companion_mind.journal import JournalError
from .contracts import (VERSION, AuthorityFixture, Grant, HomeError, Scope, Turn,
                        WakeCandidate, encode, exact_keys, identifier)
from .runtime import FAULTS, OwnedRuntime
from .context import ContextTurn
from .router import SourceRevision


def validate_operation(operation):
    op = operation.get("op") if isinstance(operation, dict) else None
    if op in {"turn", "context_turn", "topic_switch"}:
        exact_keys(operation, ("op", "turn"), ("resume",))
        (Turn if op == "turn" else ContextTurn)(**operation["turn"])
        if type(operation.get("resume", False)) is not bool:
            raise HomeError("INVALID_RESUME")
    elif op == "wake":
        exact_keys(operation, ("op", "candidate"))
        WakeCandidate(**operation["candidate"])
    elif op in {"observe", "resume"}:
        exact_keys(operation, ("op", "request_id"))
        identifier(operation["request_id"])
    elif op == "session_state":
        exact_keys(operation, ("op", "session_id"))
        identifier(operation["session_id"])
    elif op == "rebuild":
        exact_keys(operation, ("op", "source_id", "version"))
    elif op in {"info", "safe_export"}:
        exact_keys(operation, ("op",))
    else:
        raise HomeError("OPERATION_NOT_IN_SLICE")


class OwnedHomeTestPort:
    version = VERSION

    def __init__(self, directory, *, scope, fixtures=(), grants=(), fault=None):
        self.__runtime = OwnedRuntime(directory, scope=scope, fixtures=fixtures, grants=grants, fault=fault)

    def close(self):
        self.__runtime.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def execute(self, operation):
        validate_operation(operation)
        op = operation.get("op")
        if op == "turn":
            exact_keys(operation, ("op", "turn"), ("resume",))
            return self.__runtime.submit(Turn(**operation["turn"]), resume=operation.get("resume", False))
        if op in {"context_turn", "topic_switch"}:
            return self.__runtime.submit(ContextTurn(**operation["turn"]), resume=operation.get("resume", False))
        if op == "session_state":
            return self.__runtime.session_state(operation["session_id"])
        if op == "observe":
            exact_keys(operation, ("op", "request_id"))
            return self.__runtime.observe(operation["request_id"])
        if op == "resume":
            return self.__runtime.resume(operation["request_id"])
        if op == "safe_export":
            exact_keys(operation, ("op",))
            return {"events": self.__runtime.safe_export()}
        if op == "wake":
            exact_keys(operation, ("op", "candidate"))
            return self.__runtime.wake(WakeCandidate(**operation["candidate"]))
        if op == "rebuild":
            exact_keys(operation, ("op", "source_id", "version"))
            return self.__runtime.rebuild(operation["source_id"], operation["version"])
        if op == "info":
            exact_keys(operation, ("op",))
            return {"contract_version": VERSION, "testport": "OwnedHomeTestPort v1",
                    "authority": "A019", "offline_only": True, "synthetic_only": True,
                    "live_provider_enabled": False, "external_connectors_enabled": False,
                    "automatic_resume": False, "FTS5": True, "index_relation": "SEPARATE_FROM_A019",
                    "context_version": "context-pack/2", "recent_turn_limit": 4, "evidence_limit": 5,
                    "fault_points": sorted(FAULTS), "supported_ops": ["turn", "observe", "resume", "safe_export", "wake", "rebuild", "info", "context_turn", "topic_switch", "session_state"]}
        raise HomeError("OPERATION_NOT_IN_SLICE")


def execute(directory, request, *, fault=None):
    if not isinstance(request, dict):
        raise HomeError("INVALID_SHAPE")
    required = ("contract_version", "scope", "op")
    optional = ("fixtures", "grants", "turn", "resume", "request_id", "candidate", "source_id", "version", "session_id")
    exact_keys(request, required, optional)
    if request["contract_version"] != VERSION:
        raise HomeError("CONTRACT_VERSION_MISMATCH")
    fixtures, grants = request.get("fixtures", []), request.get("grants", [])
    if not isinstance(fixtures, list) or not isinstance(grants, list) or len(fixtures) > 16 or len(grants) > 32:
        raise HomeError("FIXTURE_LIMIT")
    operation = {k: v for k, v in request.items() if k not in {"contract_version", "scope", "fixtures", "grants"}}
    # Validate untrusted operation bodies before opening any persistent store.
    validate_operation(operation)
    with OwnedHomeTestPort(directory, scope=Scope(**request["scope"]),
                           fixtures=[(SourceRevision if "revision" in f or "lifecycle" in f else AuthorityFixture)(**f) for f in fixtures],
                           grants=[Grant(**g) for g in grants], fault=fault) as port:
        return port.execute(operation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--fault", choices=sorted(FAULTS))
    args = parser.parse_args()
    try:
        payload = sys.stdin.buffer.read(1_048_577)
        if len(payload) > 1_048_576:
            raise HomeError("REQUEST_TOO_LARGE")
        request = json.loads(payload, parse_constant=lambda _: (_ for _ in ()).throw(HomeError("NONFINITE_JSON")))
        result = execute(args.store, request, fault=args.fault)
        print(encode({"ok": True, "result": result}))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, (HomeError, JournalError)) else "INVALID_REQUEST"
        print(encode({"ok": False, "error": code}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
