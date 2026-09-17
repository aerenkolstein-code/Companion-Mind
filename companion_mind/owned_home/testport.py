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
from .model_gateway import ModelProfile, ModelTurn, ProfileRef


def validate_operation(operation):
    op = operation.get("op") if isinstance(operation, dict) else None
    if op in {"model_turn", "model_preview", "model_validate_spec"}:
        required = ("op", "turn", "spec") if op == "model_validate_spec" else ("op", "turn")
        exact_keys(operation, required, ("resume", "expected_spec") if op == "model_turn" else ())
        ModelTurn(**operation["turn"])
        if type(operation.get("resume", False)) is not bool:
            raise HomeError("INVALID_RESUME")
        if op == "model_validate_spec" and not isinstance(operation["spec"], dict):
            raise HomeError("INVALID_MODEL_SPEC")
        if "expected_spec" in operation and not isinstance(operation["expected_spec"], dict):
            raise HomeError("INVALID_MODEL_SPEC")
    elif op == "model_registry":
        exact_keys(operation, ("op",))
    elif op == "model_lookup":
        exact_keys(operation, ("op", "profile_key", "profile_version"))
        ProfileRef(operation["profile_key"], operation["profile_version"])
    elif op in {"turn", "context_turn", "topic_switch"}:
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

    def __init__(self, directory, *, scope, fixtures=(), grants=(), fault=None, model_profiles=None):
        self.__runtime = OwnedRuntime(directory, scope=scope, fixtures=fixtures, grants=grants,
                                      fault=fault, model_profiles=model_profiles)

    def close(self):
        self.__runtime.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def execute(self, operation):
        validate_operation(operation)
        op = operation.get("op")
        if op == "model_turn":
            return self.__runtime.submit(ModelTurn(**operation["turn"]), resume=operation.get("resume", False),
                                         expected_spec=operation.get("expected_spec"))
        if op in {"model_preview", "model_validate_spec"}:
            return self.__runtime.model_preview(ModelTurn(**operation["turn"]), operation.get("spec"))
        if op == "model_registry":
            return self.__runtime.model_registry()
        if op == "model_lookup":
            return self.__runtime.model_registry({k: operation[k] for k in ("profile_key", "profile_version")})
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
                    "model_gateway_version": "synthetic-model-gateway/1", "model_invocation_owner": "A019",
                    "fault_points": sorted(FAULTS), "supported_ops": ["turn", "observe", "resume", "safe_export", "wake", "rebuild", "info", "context_turn", "topic_switch", "session_state", "model_registry", "model_lookup", "model_preview", "model_validate_spec", "model_turn"]}
        raise HomeError("OPERATION_NOT_IN_SLICE")


def execute(directory, request, *, fault=None):
    if not isinstance(request, dict):
        raise HomeError("INVALID_SHAPE")
    required = ("contract_version", "scope", "op")
    optional = ("fixtures", "grants", "turn", "resume", "request_id", "candidate", "source_id", "version", "session_id",
                "model_profiles", "profile_key", "profile_version", "spec", "expected_spec")
    exact_keys(request, required, optional)
    if request["contract_version"] != VERSION:
        raise HomeError("CONTRACT_VERSION_MISMATCH")
    fixtures, grants = request.get("fixtures", []), request.get("grants", [])
    if not isinstance(fixtures, list) or not isinstance(grants, list) or len(fixtures) > 16 or len(grants) > 32:
        raise HomeError("FIXTURE_LIMIT")
    model_profiles = request.get("model_profiles")
    if model_profiles is not None and (not isinstance(model_profiles, list) or len(model_profiles) > 16):
        raise HomeError("INVALID_PROFILE_REGISTRY")
    operation = {k: v for k, v in request.items() if k not in {"contract_version", "scope", "fixtures", "grants", "model_profiles"}}
    # Validate untrusted operation bodies before opening any persistent store.
    validate_operation(operation)
    with OwnedHomeTestPort(directory, scope=Scope(**request["scope"]),
                           fixtures=[(SourceRevision if "revision" in f or "lifecycle" in f else AuthorityFixture)(**f) for f in fixtures],
                           grants=[Grant(**g) for g in grants], fault=fault,
                           model_profiles=None if model_profiles is None else [ModelProfile(**p) for p in model_profiles]) as port:
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
