"""P2-S3 pure synthetic model contracts; no I/O, credentials or attempt store.

The versioned estimator counts exact UTF-8 bytes of canonical JSON. These are
synthetic units, not vendor tokens. Runtime selects; A019 alone invokes the
offline script and persists its attempts. A result is projected only from that
durable terminal evidence, never from a planned outcome alone.
"""
from dataclasses import asdict, dataclass, field

from .contracts import HomeError, encode, exact_keys, fingerprint, identifier
from .context import ContextTurn

CAPABILITIES = ("files", "media", "structured_output", "tool_calls")
SCRIPTS = ("COMPLETE", "PARTIAL", "FAILED", "TIMEOUT", "UNKNOWN")
ESTIMATOR = {"id": "synthetic-utf8-json", "version": "v1", "vendor_tokenizer_parity": False}


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise HomeError("INVALID_MODEL_LIMIT")


def capabilities(values):
    if not isinstance(values, (list, tuple)) or len(values) > len(CAPABILITIES):
        raise HomeError("INVALID_CAPABILITIES")
    if any(not isinstance(v, str) or v not in CAPABILITIES for v in values) or len(set(values)) != len(values):
        raise HomeError("INVALID_CAPABILITIES")
    return tuple(sorted(values))


@dataclass(frozen=True, order=True)
class ProfileRef:
    profile_key: str
    profile_version: str

    def __post_init__(self):
        identifier(self.profile_key)
        identifier(self.profile_version)

    def projection(self):
        return asdict(self)


@dataclass(frozen=True)
class ModelProfile:
    provider_key: str
    model_key: str
    profile_key: str
    profile_version: str
    context_capacity: int
    output_reserve: int
    envelope_reserve: int = 128
    estimator_id: str = "synthetic-utf8-json"
    estimator_version: str = "v1"
    structured_output: bool = False
    tool_calls: bool = False
    media: bool = False
    files: bool = False
    availability: str = "AVAILABLE"
    health: str = "HEALTHY"
    cost_rank: int = 1
    latency_ms: int = 1
    synthetic: bool = True
    public_safe: bool = True

    def __post_init__(self):
        for value in (self.provider_key, self.model_key, self.profile_key, self.profile_version):
            identifier(value)
        if self.synthetic is not True or self.public_safe is not True:
            raise HomeError("PUBLIC_SYNTHETIC_PROFILE_REQUIRED")
        if (self.estimator_id, self.estimator_version) != (ESTIMATOR["id"], ESTIMATOR["version"]):
            raise HomeError("UNSUPPORTED_SYNTHETIC_ESTIMATOR")
        integer(self.context_capacity, 128, 65536)
        integer(self.output_reserve, 64, 32768)
        integer(self.envelope_reserve, 0, 32768)
        integer(self.cost_rank, 0, 1_000_000)
        integer(self.latency_ms, 0, 1_000_000)
        if self.usable_input_capacity < 64:
            raise HomeError("PROFILE_CAPACITY_EXHAUSTED")
        if any(type(getattr(self, name)) is not bool for name in CAPABILITIES):
            raise HomeError("INVALID_CAPABILITIES")
        if self.availability not in ("AVAILABLE", "UNAVAILABLE") or self.health not in ("HEALTHY", "UNHEALTHY"):
            raise HomeError("INVALID_MODEL_HEALTH")

    @property
    def ref(self):
        return ProfileRef(self.profile_key, self.profile_version)

    @property
    def usable_input_capacity(self):
        return self.context_capacity - self.output_reserve - self.envelope_reserve

    @property
    def digest(self):
        return fingerprint(asdict(self))

    def projection(self):
        return {**asdict(self), "profile_fingerprint": self.digest,
                "usable_input_capacity": self.usable_input_capacity, "unit": "SYNTHETIC_UTF8_BYTES",
                "vendor_tokenizer_parity": False}


def default_profiles():
    return (ModelProfile("synthetic-a", "small-model", "synthetic-small", "v1", 4096, 256),
            ModelProfile("synthetic-b", "large-model", "synthetic-large", "v1", 32768, 2048,
                         structured_output=True, media=True, files=True, cost_rank=3, latency_ms=3),
            ModelProfile("synthetic-c", "capable-model", "synthetic-capable", "v1", 16384, 1024,
                         structured_output=True, tool_calls=True, media=True, files=True,
                         cost_rank=2, latency_ms=2))


@dataclass(frozen=True)
class ModelIntent:
    preferred_profile_key: str | None = "synthetic-small"
    preferred_profile_version: str | None = "v1"
    allowed_profiles: tuple = field(default_factory=lambda: tuple(p.ref.projection() for p in default_profiles()))
    required_capabilities: tuple = ()
    allowed_capabilities: tuple = CAPABILITIES
    minimum_input_capacity: int = 64
    output_budget: int = 128
    timeout_ms: int = 1000
    response_format: str = "text"
    reasoning_tier: str = "NONE"
    audit_tier: str = "SAFE_METADATA"
    fallback_policy: str = "STOP"
    max_cost_rank: int = 1_000_000
    max_latency_ms: int = 1_000_000

    def __post_init__(self):
        if (self.preferred_profile_key is None) != (self.preferred_profile_version is None):
            raise HomeError("EXACT_PROFILE_VERSION_REQUIRED")
        if self.preferred_profile_key is not None:
            ProfileRef(self.preferred_profile_key, self.preferred_profile_version)
        if not isinstance(self.allowed_profiles, (list, tuple)) or not 1 <= len(self.allowed_profiles) <= 16:
            raise HomeError("INVALID_PROFILE_ALLOWLIST")
        refs = tuple(ProfileRef(**r) if isinstance(r, dict) else r for r in self.allowed_profiles)
        if any(type(r) is not ProfileRef for r in refs) or len(set(refs)) != len(refs):
            raise HomeError("INVALID_PROFILE_ALLOWLIST")
        object.__setattr__(self, "allowed_profiles", tuple(sorted(refs)))
        object.__setattr__(self, "required_capabilities", capabilities(self.required_capabilities))
        object.__setattr__(self, "allowed_capabilities", capabilities(self.allowed_capabilities))
        if self.response_format not in ("text", "json") or self.reasoning_tier != "NONE" or self.audit_tier != "SAFE_METADATA":
            raise HomeError("UNSUPPORTED_MODEL_FORMAT_OR_TIER")
        if not set(self.required).issubset(self.allowed_capabilities):
            raise HomeError("CAPABILITY_NOT_ALLOWED")
        if self.fallback_policy not in ("STOP", "PRE_CALL_COMPATIBLE"):
            raise HomeError("INVALID_FALLBACK_POLICY")
        integer(self.minimum_input_capacity, 64, 65536)
        integer(self.output_budget, 64, 32768)
        integer(self.timeout_ms, 1, 1_000_000)
        integer(self.max_cost_rank, 0, 1_000_000)
        integer(self.max_latency_ms, 0, 1_000_000)

    @property
    def required(self):
        return tuple(sorted(set(self.required_capabilities) | ({"structured_output"} if self.response_format == "json" else set())))

    def projection(self):
        value = asdict(self)
        value["allowed_profiles"] = [r.projection() for r in self.allowed_profiles]
        return value


@dataclass(frozen=True)
class ModelTurn(ContextTurn):
    model_intent: dict = field(default_factory=dict)
    model_script: str = "COMPLETE"

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.model_intent, dict):
            raise HomeError("INVALID_MODEL_INTENT")
        intent = ModelIntent(**self.model_intent)
        object.__setattr__(self, "model_intent", intent.projection())
        if self.model_script not in SCRIPTS:
            raise HomeError("UNKNOWN_SYNTHETIC_SCRIPT")

    @property
    def intent(self):
        return ModelIntent(**self.model_intent)

    @property
    def identity(self):
        # A topic's task remains stable across explicit model/turn changes.
        # Request/turn/event/attempt identity still follows the unchanged v1 seam.
        return {**super().identity, "task_id": "model-task-" + fingerprint({
            **self.scope.projection(), "session_id": self.session_id,
            "topic_id": self.topic_id, "premise_id": self.premise_id})[:32]}


class CapabilityRegistry:
    def __init__(self, profiles=None):
        profiles = tuple(default_profiles() if profiles is None else profiles)
        if len(profiles) > 16 or any(type(p) is not ModelProfile for p in profiles):
            raise HomeError("INVALID_PROFILE_REGISTRY")
        if len({p.ref for p in profiles}) != len(profiles):
            raise HomeError("DUPLICATE_PROFILE_IDENTITY")
        self.__profiles = tuple(sorted(profiles, key=lambda p: p.ref))

    def lookup(self, ref):
        return next((p for p in self.__profiles if p.ref == ref), None)

    def projection(self):
        values = [p.projection() for p in self.__profiles]
        return {"registry_version": "synthetic-model-registry/1", "profiles": values,
                "registry_fingerprint": fingerprint(values), "authority": False,
                "durable_ledger": False, "estimator": dict(ESTIMATOR)}

    def select(self, intent, requested_budget):
        integer(requested_budget, 64, 65536)
        candidates, compatible = [], []
        for ref in intent.allowed_profiles:
            p = self.lookup(ref)
            checks = {"exact_version": p is not None,
                      "available": bool(p and p.availability == "AVAILABLE" and p.health == "HEALTHY"),
                      "capabilities": {name: bool(p and getattr(p, name)) for name in intent.required},
                      "capacity": bool(p and min(requested_budget, p.usable_input_capacity) >= intent.minimum_input_capacity),
                      "output_budget": bool(p and p.output_reserve >= intent.output_budget),
                      "cost": bool(p and p.cost_rank <= intent.max_cost_rank),
                      "latency": bool(p and p.latency_ms <= intent.max_latency_ms)}
            ok = all(v for k, v in checks.items() if k != "capabilities") and all(checks["capabilities"].values())
            candidates.append({**ref.projection(), "profile_fingerprint": p.digest if p else None,
                               "checks": checks, "compatible": ok})
            if ok:
                compatible.append(p)
        compatible.sort(key=lambda p: (p.cost_rank, p.latency_ms, p.profile_key, p.profile_version))
        preferred = ProfileRef(intent.preferred_profile_key, intent.preferred_profile_version) if intent.preferred_profile_key is not None else None
        chosen = next((p for p in compatible if p.ref == preferred), None)
        reason, fallback = "EXACT_PREFERRED", None
        if preferred and (preferred not in intent.allowed_profiles or self.lookup(preferred) is None):
            chosen, reason = None, "EXACT_PROFILE_UNRESOLVED_OR_NOT_ALLOWED"
        elif chosen is None:
            if preferred is None or intent.fallback_policy == "PRE_CALL_COMPATIBLE":
                chosen = compatible[0] if compatible else None
                reason = "DETERMINISTIC_SELECTION" if preferred is None else "PRE_CALL_COMPATIBLE_FALLBACK"
                fallback = "PREFERRED_INCOMPATIBLE_OR_UNAVAILABLE" if preferred else None
            else:
                reason = "PREFERRED_INCOMPATIBLE_FALLBACK_FORBIDDEN"
        decision = {"candidates": candidates, "preferred": preferred.projection() if preferred else None,
                    "selected": chosen.ref.projection() if chosen else None,
                    "selected_profile_fingerprint": chosen.digest if chosen else None,
                    "selection_reason": reason if chosen or preferred else "NO_COMPATIBLE_PROFILE",
                    "fallback_reason": fallback, "fallback_policy": intent.fallback_policy,
                    "required_capabilities": list(intent.required), "allowed_capabilities": list(intent.allowed_capabilities),
                    "stop_reason": None if chosen else "NO_COMPATIBLE_PROFILE", "provider_invocations": 0,
                    "requested_budget": requested_budget,
                    "effective_input_budget": min(requested_budget, chosen.usable_input_capacity) if chosen else 0,
                    "usable_input_capacity": chosen.usable_input_capacity if chosen else 0,
                    "output_reserve": chosen.output_reserve if chosen else 0,
                    "envelope_reserve": chosen.envelope_reserve if chosen else 0,
                    "context_capacity": chosen.context_capacity if chosen else 0,
                    "registry_fingerprint": self.projection()["registry_fingerprint"],
                    "estimator": dict(ESTIMATOR), "estimator_fingerprint": fingerprint(ESTIMATOR)}
        decision["selection_fingerprint"] = fingerprint(decision)
        return chosen, decision


@dataclass(frozen=True)
class ModelCallSpec:
    identity: tuple
    profile_key: str
    profile_version: str
    profile_fingerprint: str
    estimator_fingerprint: str
    context_fingerprint: str
    input_fingerprint: str
    required_capabilities: tuple
    allowed_capabilities: tuple
    input_budget: int
    output_budget: int
    timeout_ms: int
    response_format: str
    reasoning_tier: str
    audit_tier: str
    adapter_ref: str
    script_ref: str
    request_fingerprint: str

    def projection(self):
        value = asdict(self)
        value["identity"] = dict(self.identity)
        value["required_capabilities"] = list(self.required_capabilities)
        value["allowed_capabilities"] = list(self.allowed_capabilities)
        return {**value, "spec_fingerprint": fingerprint(value)}


def call_spec(turn, profile, context):
    intent = turn.intent
    identity = {**turn.identity, **turn.scope.projection(), "topic_id": turn.topic_id}
    return ModelCallSpec(tuple(sorted(identity.items())), profile.profile_key, profile.profile_version,
                         profile.digest, fingerprint(ESTIMATOR), context["context_fingerprint"],
                         context["input_fingerprint"], intent.required, intent.allowed_capabilities,
                         context["budget"]["limit"], intent.output_budget, intent.timeout_ms,
                         intent.response_format, intent.reasoning_tier, intent.audit_tier,
                         "a019-deterministic-offline-stub/v1", turn.model_script,
                         fingerprint(turn.projection())).projection()


def spec_matches(supplied, expected):
    # Compare every bound field, not a caller-supplied digest or profile label.
    return isinstance(supplied, dict) and encode(supplied) == encode(expected)


def script_frames(spec):
    outcome = spec["script_ref"]
    text = (encode({"synthetic": True, "outcome": outcome}) if spec["response_format"] == "json" else
            "Synthetic model result: " + outcome + ".")
    if len(text.encode("utf-8")) > spec["output_budget"]:
        raise HomeError("SYNTHETIC_OUTPUT_BUDGET_EXCEEDED")
    # Two frames provide a real interrupted-stream recovery case. No invocation
    # occurs here; this only prepares the immutable A019 offline script.
    return (text[:10], text[10:]), {"COMPLETE": "complete", "FAILED": "failed"}.get(outcome, "partial")


@dataclass(frozen=True)
class ModelCallResult:
    outcome: str
    terminal_evidence: str
    profile_ref: tuple
    attempt_id: str
    spec_fingerprint: str
    context_fingerprint: str
    candidate_fingerprint: str
    candidate_bytes: int
    input_units: int
    output_units: int
    latency_ms: int | None
    safe_error_class: str | None
    invocation_count: int | str
    invocation_upper_bound: int
    external_outcome: str

    def projection(self):
        value = asdict(self)
        value["profile_ref"] = dict(self.profile_ref)
        value.update(synthetic=True, real_provider_invocations=0, spend=0,
                     retry_decision="NO_AUTOMATIC_RETRY", automatic_retries=0,
                     usage_unit="SYNTHETIC_UTF8_BYTES", hidden_reasoning=None,
                     candidate_content="VISIBLE_REPLY_ONLY", tool_action_intents=[])
        return {**value, "result_fingerprint": fingerprint(value)}


def result_from_terminal(spec, terminal, input_units, latency_ms):
    evidence = terminal["metadata"]["extensions"].get("a019_attempt")
    if not evidence:
        return None
    observed = evidence["terminal_evidence"] == "OBSERVED"
    outcome = spec["script_ref"] if observed else "UNKNOWN"
    candidate = terminal["content_payload"].get("text", "")
    size = len(candidate.encode("utf-8"))
    return ModelCallResult(outcome, evidence["terminal_evidence"],
                           tuple((k, spec[k]) for k in ("profile_key", "profile_version")),
                           spec["identity"]["attempt_id"], spec["spec_fingerprint"],
                           spec["context_fingerprint"], fingerprint(candidate), size,
                           input_units, size, latency_ms if observed else None,
                           None if outcome == "COMPLETE" else "SYNTHETIC_" + outcome,
                           1 if observed else "UNKNOWN", 1,
                           evidence["external_outcome"]).projection()
