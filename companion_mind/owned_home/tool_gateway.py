"""Exact, public-safe synthetic tool contracts and an isolated offline adapter.

Skill fixtures cannot redefine tier/operation semantics. Arguments are bounded
integers or fixed enums, never URLs, headers, credential bytes or free-form code.
The adapter's numeric resources simulate external state, not CM Authorities.
"""
from dataclasses import asdict, dataclass, field

from .action_control import AtomicState
from .contracts import HomeError, Scope, Turn, encode, exact_keys, fingerprint, identifier, timestamp

SKILLS = {
    "synthetic.compute": ("P0_PURE", "NONE", "compute", {"values": "1..32 integers"}),
    "synthetic.scoped_read": ("P1_SCOPED_READ", "SCOPED_READ", "read", {}),
    "synthetic.reversible_write": ("P2_REVERSIBLE_WRITE", "REVERSIBLE_WRITE", "write", {"value": "integer"}),
    "synthetic.consequential_send": ("P3_CONSEQUENTIAL_WRITE", "CONSEQUENTIAL_SEND", "send", {}),
    "synthetic.critical": ("P4_CRITICAL", "CRITICAL", "critical", {"operation": "fixed critical enum"}),
}
CRITICAL = ("critical", "break_glass", "credential_mutation", "grant_mutation", "security_reconfiguration", "destructive_delete")
OUTCOMES = ("SUCCESS", "FAILED", "PARTIAL", "UNKNOWN")
CONNECTOR = "synthetic-tools/v1"


def bounded_integer(value):
    if type(value) is not int or not -1_000_000 <= value <= 1_000_000:
        raise HomeError("INVALID_SYNTHETIC_NUMBER")


@dataclass(frozen=True)
class SkillContract:
    skill_id: str
    skill_version: str = "v1"
    permission_tier: str | None = None
    side_effect_class: str | None = None
    connector_class: str = CONNECTOR
    retry_budget: int = 0
    loop_budget: int = 0
    synthetic: bool = True
    public_safe: bool = True

    def __post_init__(self):
        identifier(self.skill_id)
        identifier(self.skill_version)
        if self.skill_id not in SKILLS:
            raise HomeError("SKILL_NOT_IN_SYNTHETIC_ALLOWLIST")
        tier, effect, _, _ = SKILLS[self.skill_id]
        if self.permission_tier not in (None, tier) or self.side_effect_class not in (None, effect):
            raise HomeError("SKILL_TIER_REDEFINITION")
        if (self.connector_class != CONNECTOR or type(self.retry_budget) is not int or self.retry_budget != 0
                or type(self.loop_budget) is not int or self.loop_budget != 0
                or self.synthetic is not True or self.public_safe is not True):
            raise HomeError("UNSAFE_SKILL_CONTRACT")
        object.__setattr__(self, "permission_tier", tier)
        object.__setattr__(self, "side_effect_class", effect)

    @property
    def ref(self):
        return {"skill_id": self.skill_id, "skill_version": self.skill_version}

    def projection(self):
        operation, schema = SKILLS[self.skill_id][2:]
        schema = dict(schema)
        result = {**asdict(self), "intent_class": operation,
                  "preconditions": ["EXACT_CONTRACT", "PUBLIC_SYNTHETIC_INPUT", "RUNTIME_PERMISSION"],
                  "input_schema": schema, "input_schema_fingerprint": fingerprint(schema),
                  "output_schema_fingerprint": fingerprint({"receipt": "tool-execution/1", "outcome": OUTCOMES}),
                  "required_evidence_classes": [] if operation == "compute" else ["PUBLIC_SYNTHETIC_RESOURCE"],
                  "required_authority_classes": [], "allowed_tools": [CONNECTOR + ":" + operation],
                  "ordered_sop": ["RESOLVE", "FREEZE_INTENT", "PERMISSION", "DURABLE_DISPATCH", "EXECUTE", "READBACK", "A019_EVIDENCE"],
                  "stop_policy": "FAIL_CLOSED_NO_BLIND_RETRY", "trace_hook": "tool-trace/1", "eval_hook": "TS4",
                  "authority": False}
        return {**result, "skill_fingerprint": fingerprint(result)}


class SkillRegistry:
    def __init__(self, fixtures=None):
        items = tuple(SkillContract(k) for k in SKILLS) if fixtures is None else tuple(fixtures)
        if len(items) > 16 or any(type(s) is not SkillContract for s in items):
            raise HomeError("INVALID_SKILL_REGISTRY")
        if len({(s.skill_id, s.skill_version) for s in items}) != len(items):
            raise HomeError("DUPLICATE_SKILL_IDENTITY")
        self.__items = tuple(sorted(items, key=lambda s: (s.skill_id, s.skill_version)))

    def lookup(self, skill_id, skill_version):
        identifier(skill_id)
        identifier(skill_version)
        return next((s for s in self.__items if (s.skill_id, s.skill_version) == (skill_id, skill_version)), None)

    def projection(self):
        items = [s.projection() for s in self.__items]
        return {"registry_version": "synthetic-skill-registry/1", "skills": items,
                "registry_fingerprint": fingerprint(items), "authority": False, "executions": 0}


@dataclass(frozen=True)
class ToolTarget:
    target_id: str
    universe_id: str
    access_subject_id: str
    initial_value: int = 0
    version: str = "v1"
    synthetic: bool = True
    public_safe: bool = True

    def __post_init__(self):
        for v in (self.target_id, self.universe_id, self.access_subject_id, self.version):
            identifier(v)
        bounded_integer(self.initial_value)
        if self.synthetic is not True or self.public_safe is not True:
            raise HomeError("PUBLIC_SYNTHETIC_TARGET_REQUIRED")

    def ref(self):
        return {"target_id": self.target_id, "version": self.version,
                "universe_id": self.universe_id, "access_subject_id": self.access_subject_id,
                "fixture_fingerprint": fingerprint(asdict(self)), "class": "PUBLIC_SYNTHETIC_RESOURCE"}


@dataclass(frozen=True)
class ActionRequest:
    action_id: str
    task_id: str
    request_id: str
    session_id: str
    turn_id: str
    turn_no: int
    universe_id: str
    access_subject_id: str
    skill_id: str
    skill_version: str
    target_id: str
    idempotency_key: str
    parameters: dict = field(default_factory=dict)
    grant_id: str | None = None
    observed_at: str = "2026-09-18T00:00:00+00:00"
    declared_tier: str | None = None
    declared_side_effect: str | None = None
    reversible: bool = True
    expected_readback: str = "AUTO"
    script: str = "SUCCESS"
    readback_mode: str = "VERIFY"
    synthetic: bool = True
    public_safe: bool = True

    def __post_init__(self):
        for v in (self.action_id, self.task_id, self.request_id, self.session_id, self.turn_id,
                  self.universe_id, self.access_subject_id, self.skill_id, self.skill_version,
                  self.target_id, self.idempotency_key):
            identifier(v)
        if self.grant_id is not None:
            identifier(self.grant_id)
        timestamp(self.observed_at)
        if type(self.turn_no) is not int or not 1 <= self.turn_no <= 1_000_000:
            raise HomeError("INVALID_TURN_NUMBER")
        if self.synthetic is not True or self.public_safe is not True:
            raise HomeError("PUBLIC_SYNTHETIC_ACTION_REQUIRED")
        if type(self.reversible) is not bool or self.expected_readback not in ("AUTO", "VALUE_FINGERPRINT", "NONE"):
            raise HomeError("INVALID_READBACK_CONTRACT")
        if self.script not in OUTCOMES or self.readback_mode not in ("VERIFY", "MISMATCH", "UNAVAILABLE"):
            raise HomeError("INVALID_TOOL_SCRIPT")
        for value in (self.declared_tier, self.declared_side_effect):
            if value is not None:
                identifier(value)
        # Validate the public input schema before any store opens, even if the
        # exact contract version will subsequently be unresolved.
        if self.skill_id == "synthetic.compute":
            exact_keys(self.parameters, ("values",))
            values = self.parameters["values"]
            if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 32:
                raise HomeError("INVALID_SYNTHETIC_VALUES")
            for value in values:
                bounded_integer(value)
            object.__setattr__(self, "parameters", {"values": tuple(values)})
        elif self.skill_id == "synthetic.reversible_write":
            exact_keys(self.parameters, ("value",))
            bounded_integer(self.parameters["value"])
            object.__setattr__(self, "parameters", dict(self.parameters))
        elif self.skill_id == "synthetic.critical":
            exact_keys(self.parameters, ("operation",))
            if self.parameters["operation"] not in CRITICAL:
                raise HomeError("INVALID_CRITICAL_OPERATION")
            object.__setattr__(self, "parameters", dict(self.parameters))
        else:
            exact_keys(self.parameters, ())

    @property
    def scope(self):
        return Scope(self.universe_id, self.access_subject_id)

    def turn(self):
        return Turn(self.request_id, self.session_id, self.turn_id, self.turn_no,
                    self.universe_id, self.access_subject_id, self.target_id, "v1",
                    "Public synthetic tool intent.", self.observed_at)

    @property
    def identity(self):
        return {**self.turn().identity, "task_id": self.task_id, "action_id": self.action_id}

    def projection(self):
        return asdict(self)


def action_intent(action, skill, target):
    profile = skill.projection() if skill else None
    tier = skill.permission_tier if skill else None
    required_readback = "VALUE_FINGERPRINT" if tier == "P2_REVERSIBLE_WRITE" else "NONE"
    readback = required_readback if action.expected_readback == "AUTO" else action.expected_readback
    result = {"intent_version": "tool-action-intent/1", "action_id": action.action_id,
              "identity": action.identity, "scope": action.scope.projection(),
              "skill_ref": {"skill_id": action.skill_id, "skill_version": action.skill_version},
              "skill_fingerprint": profile["skill_fingerprint"] if profile else None,
              "parameters_fingerprint": fingerprint(action.parameters), "target_id": action.target_id,
              "target_ref": target.ref() if target else None,
              "permission_tier": tier, "side_effect_class": skill.side_effect_class if skill else None,
              "declared_tier": action.declared_tier, "declared_side_effect": action.declared_side_effect,
              "idempotency_key": action.idempotency_key, "grant_id": action.grant_id,
              "reversible": action.reversible, "expected_receipt_class": "SYNTHETIC_RECEIPT_V1",
              "expected_readback_class": readback,
              "expected_value_fingerprint": fingerprint(action.parameters["value"]) if tier == "P2_REVERSIBLE_WRITE" else None,
              "request_fingerprint": fingerprint(action.projection()), "authority": False}
    return {**result, "intent_fingerprint": fingerprint(result)}


def exact_binding(supplied, current):
    return isinstance(supplied, dict) and encode(supplied) == encode(current)


class SyntheticToolAdapter:
    """Numeric target fixture state only; never CM Current/Canon/Persona data."""

    def __init__(self, directory):
        self.state = AtomicState(directory, "synthetic-tool-resources/1", {"resources": {}})
        if not isinstance(self.state.data.get("resources"), dict):
            self.close()
            raise HomeError("SYNTHETIC_RESOURCE_FORMAT")

    def close(self):
        self.state.close()

    @staticmethod
    def key(scope, target_id):
        return fingerprint({**scope, "target_id": target_id})

    def _resource(self, target):
        key = self.key({"universe_id": target.universe_id, "access_subject_id": target.access_subject_id}, target.target_id)
        resources = self.state.data["resources"]
        if key not in resources:
            resources[key] = {"value": target.initial_value, "reads": 0, "writes": 0,
                              "fixture_fingerprint": target.ref()["fixture_fingerprint"]}
        if resources[key]["fixture_fingerprint"] != target.ref()["fixture_fingerprint"]:
            raise HomeError("SYNTHETIC_TARGET_FIXTURE_DRIFT")
        return resources[key]

    def execute(self, action, skill, target, decision):
        if (not decision["execution_allowed"] or decision["decision"] != "ALLOW" or
                skill.permission_tier not in ("P0_PURE", "P1_SCOPED_READ", "P2_REVERSIBLE_WRITE")):
            raise HomeError("TOOL_EXECUTION_NOT_ALLOWED")
        operation = SKILLS[skill.skill_id][2]
        value, reads, writes = None, 0, 0
        if action.script != "FAILED":
            if operation == "compute":
                value = sum(action.parameters["values"])
            elif operation in ("read", "write"):
                resource = self._resource(target)
                if operation == "read":
                    resource["reads"] += 1
                    reads = 1
                else:
                    resource["value"] = action.parameters["value"]
                    resource["writes"] += 1
                    writes = 1
                value = resource["value"]
                self.state.save()
        return {"reported_outcome": action.script,
                "external_receipt_ref": "synthetic-receipt-" + fingerprint({"action": action.projection(), "result": value}),
                "result_fingerprint": fingerprint(value),
                "pure_result": value if operation == "compute" else None,
                "synthetic_reads": reads, "synthetic_writes": writes}

    def readback(self, action, intent):
        key = self.key(intent["scope"], intent["target_id"])
        resource = self.state.data["resources"].get(key)
        if action.readback_mode == "UNAVAILABLE" or resource is None:
            return {"status": "UNAVAILABLE", "observed_fingerprint": None}
        value = resource["value"] + (action.readback_mode == "MISMATCH")
        observed = fingerprint(value)
        return {"status": "VERIFIED" if observed == intent["expected_value_fingerprint"] else "MISMATCH",
                "observed_fingerprint": observed}

    def probe(self, intent):
        resource = self.state.data["resources"].get(self.key(intent["scope"], intent["target_id"]))
        return {"synthetic_resource_only": True, "authority": False,
                "reads": resource["reads"] if resource else 0, "writes": resource["writes"] if resource else 0,
                "value_fingerprint": fingerprint(resource["value"]) if resource else None}


def execution_receipt(intent, decision, observation=None, readback=None):
    observed = observation is not None
    outcome = observation["reported_outcome"] if observed else "UNKNOWN"
    readback = readback or {"status": "NOT_REQUIRED" if intent["permission_tier"] != "P2_REVERSIBLE_WRITE" else "NOT_VERIFIED",
                            "observed_fingerprint": None}
    error = None if outcome == "SUCCESS" else "SYNTHETIC_" + outcome
    if intent["permission_tier"] == "P2_REVERSIBLE_WRITE" and outcome == "SUCCESS" and readback["status"] != "VERIFIED":
        outcome, error = "UNKNOWN", "P2_READBACK_NOT_VERIFIED"
    result = {"receipt_version": "tool-execution/1", "action_id": intent["action_id"],
              "idempotency_key": intent["idempotency_key"], "action_fingerprint": intent["intent_fingerprint"],
              "skill_ref": intent["skill_ref"], "skill_fingerprint": intent["skill_fingerprint"],
              "permission_decision_id": decision["decision_id"], "permission_fingerprint": decision["decision_fingerprint"],
              "connector_tool_version": CONNECTOR, "started_marker": "DISPATCH_INTENT_DURABLE",
              "completed_marker": "RECEIPT_OBSERVED" if observed else "RECOVERY_WITHOUT_RECEIPT",
              "clock_domain": "SYNTHETIC_LOGICAL", "outcome": outcome, "safe_error_class": error,
              "external_receipt_ref": observation["external_receipt_ref"] if observed else None,
              "result_fingerprint": observation["result_fingerprint"] if observed else None,
              "pure_result": observation["pure_result"] if observed else None, "readback": readback,
              "execution_count": 1 if observed else "UNKNOWN", "execution_upper_bound": 1,
              "synthetic_reads": observation["synthetic_reads"] if observed else "UNKNOWN",
              "synthetic_writes": observation["synthetic_writes"] if observed else "UNKNOWN",
              "automatic_redispatches": 0, "retry_decision": "NO_AUTOMATIC_REDISPATCH",
              "authority_mutation_count": 0, "real_external_side_effects": 0, "real_credential_reads": 0, "spend": 0}
    return {**result, "receipt_fingerprint": fingerprint(result)}
